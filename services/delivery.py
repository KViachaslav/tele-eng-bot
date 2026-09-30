"""Отправка слов пользователю.

Используется планировщиком (слова по расписанию) и командой ``/word``.
Здесь же определяется, какая клавиатура прикладывается к сообщению: в режиме
«сначала слово, потом перевод» — кнопка «Показать», в режиме «всё сразу» —
сразу «Знаю» / «Не знаю».
"""
from __future__ import annotations

from collections.abc import Collection

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import User, UserSettings, utcnow
from keyboards.inline import answer_keyboard, show_word_keyboard
from services.message_builder import build_refresh_card, build_word_card
from services.word_selector import (
    SelectedWord,
    on_demand_exclusions,
    select_main_word,
    select_refresh_word,
)


class UserUnreachable(RuntimeError):
    """Telegram не позволил отправить сообщение (бот заблокирован и т.п.)."""


def reveal_keyboard(user: User, word_id: int, delivery_id: int) -> InlineKeyboardMarkup:
    """Клавиатура сообщения со словом под выбранный режим показа."""
    if user.reveal_mode == config.REVEAL_MODE_ON_BUTTON:
        return show_word_keyboard(word_id, delivery_id)
    return answer_keyboard(word_id, delivery_id)


async def send_card(
    bot: Bot,
    user: User,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Отправляет карточку слова в личный чат.

    :raises UserUnreachable: если пользователь заблокировал бота — вызывающий
        код должен поставить рассылку на паузу.
    """
    try:
        await bot.send_message(
            user.telegram_id,
            text,
            parse_mode=config.PARSE_MODE,
            reply_markup=reply_markup,
        )
    except TelegramForbiddenError as error:
        logger.warning("Пользователь {} недоступен: {}", user.telegram_id, error)
        raise UserUnreachable(str(error)) from error
    except TelegramAPIError:
        logger.exception("Не удалось отправить слово пользователю {}", user.telegram_id)
        raise


async def _deliver(
    bot: Bot,
    session: AsyncSession,
    user: User,
    selected: SelectedWord,
) -> bool:
    """Отправляет выбранное слово, логируя отправку в ``delivery_log``."""
    settings: UserSettings = await repository.get_or_create_user_settings(session, user)
    now = utcnow()
    delivery = await repository.create_delivery(session, user.id, selected.word.id, sent_at=now)

    if selected.is_refresh:
        card = build_refresh_card(selected.word, settings, reveal_mode=user.reveal_mode)
    else:
        card = build_word_card(selected.word, settings, reveal_mode=user.reveal_mode)

    try:
        await send_card(bot, user, card, reveal_keyboard(user, selected.word.id, delivery.id))
    except UserUnreachable:
        await repository.update_user(session, user, paused=True)
        return False

    logger.info(
        "Пользователю {} отправлено слово {!r} (этап {}, освежение: {})",
        user.telegram_id,
        selected.word.word,
        selected.user_word.stage if selected.user_word else config.SRS_FIRST_STAGE,
        selected.is_refresh,
    )
    return True


async def deliver_main_word(
    bot: Bot,
    session: AsyncSession,
    user: User,
    *,
    exclude_word_ids: Collection[int] = (),
) -> bool:
    """Отправляет очередное слово из очереди повторений/новых слов.

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`services.word_selector.on_demand_exclusions`).
    """
    selected = await select_main_word(
        session, user, utcnow(), exclude_word_ids=exclude_word_ids
    )
    if selected is None:
        return False
    return await _deliver(bot, session, user, selected)


async def deliver_refresh_word(
    bot: Bot,
    session: AsyncSession,
    user: User,
    *,
    exclude_word_ids: Collection[int] = (),
) -> bool:
    """Отправляет выученное слово, у которого подошёл срок освежения.

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`services.word_selector.on_demand_exclusions`).
    """
    selected = await select_refresh_word(
        session, user, utcnow(), exclude_word_ids=exclude_word_ids
    )
    if selected is None:
        return False
    return await _deliver(bot, session, user, selected)


async def deliver_slot(bot: Bot, session: AsyncSession, user: User) -> int:
    """Одно «слово слота» плюс, если нужно, одно слово на освежение.

    Освежение идёт отдельным сообщением и не расходует дневной лимит слов.

    :return: сколько сообщений со словами было отправлено.
    """
    sent = 0
    if await deliver_main_word(bot, session, user):
        sent += 1
    if await deliver_refresh_word(bot, session, user):
        sent += 1
    return sent


async def deliver_on_demand(bot: Bot, session: AsyncSession, user: User) -> bool:
    """Отправка по команде ``/word``: слово из основной очереди, иначе освежение.

    Сначала берём слово, которого у пользователя ещё нет в чате: ``/word`` и
    кнопка «🎲 Слово» должны показывать разные слова. Внутри основной очереди
    приоритет такой (см. :func:`services.word_selector.select_main_word`):
    созревшие повторения, которых сегодня ещё не было, → новое слово → повторение
    слова, которое сегодня уже показывали. Последний вариант появляется, только
    когда других слов не осталось: у слова с наступившим сроком повторения он
    остаётся наступившим до ответа, а после ответа «не знаю» срок обнуляется
    (этап 0), поэтому без такого порядка очередь зацикливалась на одном слове.
    """
    skip = await on_demand_exclusions(session, user)
    if await deliver_main_word(bot, session, user, exclude_word_ids=skip):
        return True
    if await deliver_refresh_word(bot, session, user, exclude_word_ids=skip):
        return True
    if not skip:
        return False

    logger.info(
        "Пользователь {}: других слов нет — повторяю слово из очереди",
        user.telegram_id,
    )
    if await deliver_main_word(bot, session, user):
        return True
    return await deliver_refresh_word(bot, session, user)

"""Отправка слов пользователю.

Используется планировщиком (слова по расписанию) и командой ``/word``.
Здесь же определяется, какая клавиатура прикладывается к сообщению: в режиме
«сначала слово, потом перевод» — кнопка «Показать», в режиме «всё сразу» —
сразу «Знаю» / «Не знаю». Перед сборкой карточки дозапрашиваются русские
переводы определения и примера (:mod:`services.translator`): в тексте они идут
под английскими строками под спойлером.

Отправки одного пользователя сериализуются, а строка журнала ``delivery_log``
фиксируется до отправки: иначе слот планировщика и нажатие «🎲 Слово» в одну
секунду выбирали одно и то же слово (см. :func:`user_send_lock`).
"""
from __future__ import annotations

import asyncio
from collections.abc import Collection
from weakref import WeakKeyDictionary

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import User, UserSettings, utcnow
from keyboards.inline import answer_keyboard, show_word_keyboard
from services import translator
from services.message_builder import build_refresh_card, build_word_card
from services.word_selector import (
    SelectedWord,
    new_words_allowed,
    on_demand_exclusions,
    select_main_word,
    select_refresh_word,
)


class UserUnreachable(RuntimeError):
    """Telegram не позволил отправить сообщение (бот заблокирован и т.п.)."""


#: Замки «одна отправка слова на пользователя за раз»: цикл событий → ``users.id``
#: → замок. ``asyncio.Lock`` привязывается к циклу событий при первом ожидании,
#: поэтому ключ — пара (цикл, пользователь): у тестов свой цикл на каждый тест
#: (``asyncio_default_fixture_loop_scope = function``), а в самом боте цикл один.
_send_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[int, asyncio.Lock]] = (
    WeakKeyDictionary()
)


def user_send_lock(user_id: int) -> asyncio.Lock:
    """Замок, который пропускает к отправке по одному запросу на пользователя.

    Слово выбирается по журналу ``delivery_log``, а решение принимается до того,
    как в журнал попадёт строка. Поэтому одновременные запросы успевали выбрать
    одно и то же слово: в базе 30.09.2026 есть пары строк с одинаковым ``sent_at``
    и одинаковым ``word_id`` — кнопка «🎲 Слово» и слот планировщика сработали
    в одну секунду. Замок сериализует такие отправки в процессе бота; каждая
    следующая застаёт строку предыдущей в журнале и берёт другое слово.

    Замки живут в памяти процесса: второй запущенный экземпляр бота решается не
    этим, а своим токеном (иначе Telegram отдаёт апдейты только одному из них).
    """

    loop = asyncio.get_running_loop()
    per_loop = _send_locks.get(loop)
    if per_loop is None:
        per_loop = {}
        _send_locks[loop] = per_loop
    lock = per_loop.get(user_id)
    if lock is None:
        lock = per_loop[user_id] = asyncio.Lock()
    return lock


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
    # Русские переводы определения и примера нужны до сборки текста: в карточке
    # они идут под своими английскими строками. Запрос к MyMemory случается
    # только при первом показе слова — у переведённого дозаполнять нечего.
    await translator.ensure_word_translations(session, selected.word)
    now = utcnow()
    delivery = await repository.create_delivery(session, user.id, selected.word.id, sent_at=now)
    # Строку журнала фиксируем до отправки: по ней следующая отправка выбирает
    # слово, а незафиксированную запись другая сессия (нажатие кнопки приходит со
    # своей сессией, слот планировщика — со своей) не увидела бы и выбрала то же
    # слово второй раз. Порядок «сначала журнал, потом отправка» делает факт
    # показа слова видимым сразу после выбора.
    await session.commit()

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
    Отправка идёт под замком пользователя (:func:`user_send_lock`): слот не
    должен пересечься с нажатием «🎲 Слово» — иначе обе выбирают одно слово.

    :return: сколько сообщений со словами было отправлено.
    """
    async with user_send_lock(user.id):
        sent = 0
        if await deliver_main_word(bot, session, user):
            sent += 1
        if await deliver_refresh_word(bot, session, user):
            sent += 1
        return sent


async def deliver_on_demand(bot: Bot, session: AsyncSession, user: User) -> bool:
    """Отправка по команде ``/word``: слово, которого сегодня ещё не было в чате.

    ``/word`` и кнопка «🎲 Слово» показывают разные слова: всё, чьи карточки уже
    уходили сегодня, исключается (см.
    :func:`services.word_selector.on_demand_exclusions`). Ответ «не знаю»
    возвращает слово на этап 0, то есть в расписание, а не в чат: иначе каждое
    нажатие кнопки присылало то же слово, которое только что ответили, и очередь
    ходила по кругу одних и тех же слов. Когда показывать больше нечего, отправки
    нет, а причину объясняет хендлер (:func:`handlers.common.no_word_text`).

    Особый случай — достигнутый лимит слов в изучении: новые слова не приходят, но
    повторения уже начатых остаются, и расписание их как раз и присылает. Кнопка
    ведёт себя так же и повторяет, в том числе, слово, чья карточка сегодня уже
    уходила и ответ на неё получен: иначе ручной запрос отказывал бы, пока те же
    слова идут по слотам. Висящую же карточку без ответа очередь не повторяет и в
    этом случае — её отсекает сам выбор слова
    (:func:`services.word_selector.select_main_word`), поэтому у повторений
    исключений не остаётся. Освежение — отдельная очередь, и к ней исключения
    применяются как обычно: карточка освежения уходит раз в сутки, а её срок
    повторения после отправки не сдвигается
    (``db.repository.mark_user_word_for_refresh``), поэтому без исключений каждое
    нажатие присылало бы ту же самую карточку.

    Проход идёт под замком пользователя (:func:`user_send_lock`): два быстрых
    нажатия подряд обрабатываются одновременно, и без замка оба успевали выбрать
    одно и то же слово, пока в журнал ``delivery_log`` не попала строка о первом.

    :return: получилось ли отправить карточку.
    """
    async with user_send_lock(user.id):
        skip = await on_demand_exclusions(session, user)
        # Лимит в изучении снимает исключения только для повторений: новых слов нет,
        # но слова, чьи карточки сегодня уже уходили, кнопка повторяет так же, как
        # это делает расписание. Висящую карточку и в этом случае отсекает сам выбор
        # слова (services.word_selector.select_main_word), так что дублей нет.
        repeat_skip: Collection[int] = skip
        if not await new_words_allowed(session, user):
            repeat_skip = ()
        if await deliver_main_word(bot, session, user, exclude_word_ids=repeat_skip):
            return True
        # Освежение — «бонусная» очередь, её исключения (сегодняшние показы) остаются
        # в силе: см. docstring.
        return await deliver_refresh_word(bot, session, user, exclude_word_ids=skip)

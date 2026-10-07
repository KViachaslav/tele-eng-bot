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

Ручной запрос «дай слово» идёт по плану дня в два прохода
(:func:`deliver_on_demand`): сначала слова, которые ещё предстоит отдать сегодня
(срок повторения — до конца местных суток пользователя), затем слова дня, которые
уже приходили и получили ответ, — чтобы день можно было пройти заново.
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
    on_demand_exclusions,
    select_main_word,
    select_plan_word,
    select_refresh_word,
    select_repeat_word,
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


async def deliver_plan_word(
    bot: Bot,
    session: AsyncSession,
    user: User,
    *,
    exclude_word_ids: Collection[int] = (),
) -> bool:
    """Отправляет слово из плана дня: повторение со сроком до конца суток или новое.

    Этим проходом пользуется ручной запрос «дай слово»: он идёт по плану дня, а не
    по срочности «пора показать сейчас», поэтому сегодняшние повторения можно
    пройти досрочно (см. :func:`services.word_selector.select_plan_word`).

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`services.word_selector.on_demand_exclusions`).
    """
    selected = await select_plan_word(
        session, user, utcnow(), exclude_word_ids=exclude_word_ids
    )
    if selected is None:
        return False
    return await _deliver(bot, session, user, selected)


async def deliver_repeat_word(
    bot: Bot,
    session: AsyncSession,
    user: User,
    *,
    exclude_word_ids: Collection[int] = (),
) -> bool:
    """Отправляет слово дня, которое уже приходило и отвечено (повторный проход).

    Идёт после того, как план дня выполнен: так кнопкой «🎲 Слово» можно пройти день
    заново (см. :func:`services.word_selector.select_repeat_word`).

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`services.word_selector.on_demand_exclusions`).
    """
    selected = await select_repeat_word(
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
    """Отправка по команде ``/word``: слова плана дня, затем повтор дня.

    Кнопка «🎲 Слово» идёт ровно по словам текущего дня — в два прохода:

    1. **План дня** (:func:`deliver_plan_word`): повторения, срок которых наступает
       не позже конца местных суток пользователя, и новые слова по общим правилам.
       Границы суток те же, что у плана дня (:func:`services.plan.words_left_today`),
       поэтому день можно пройти досрочно: отданное слово расходует пункт плана,
       интервал до следующего слова растёт, а пройденное расписание больше не
       присылает. Освежение (выученные слова) в этот проход не входит: его в плане
       дня нет, и расписание присылает его отдельным сообщением.
    2. **Повтор дня** (:func:`deliver_repeat_word`): слова, которые сегодня уже
       приходили и получили ответ, — чтобы день можно было пройти заново, когда план
       исчерпан.

    Лишний раз то же слово не вернётся: висящая карточка и слова, исчерпавшие лимит
    показов за сутки, исключены (:func:`services.word_selector.on_demand_exclusions`).
    Так пара «нажатие → «не знаю»» не ходит по кругу: ответ возвращает слово на
    этап 0 (то есть в план дня), но после ``config.ON_DEMAND_MAX_SENDS_PER_DAY``
    показов за сутки очередь его пропускает — и ручной запрос честно сообщает, что
    план дня выполнен (:func:`handlers.common.no_word_text`).

    Проход идёт под замком пользователя (:func:`user_send_lock`): два быстрых
    нажатия подряд обрабатываются одновременно, и без замка оба успевали выбрать
    одно и то же слово, пока в журнал ``delivery_log`` не попала строка о первом.

    :return: получилось ли отправить карточку.
    """
    async with user_send_lock(user.id):
        skip = await on_demand_exclusions(session, user)
        if await deliver_plan_word(bot, session, user, exclude_word_ids=skip):
            return True
        return await deliver_repeat_word(bot, session, user, exclude_word_ids=skip)

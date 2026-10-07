"""Общие помощники хендлеров: доступ к пользователю, меню, пауза, справка, ошибки."""
from __future__ import annotations

from datetime import datetime, time

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    MaybeInaccessibleMessage,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.text_decorations import markdown_decoration
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
import locales.ru as texts
from db import repository
from db.models import User, utcnow
from keyboards.reply import main_menu_keyboard
from services import slots, word_selector
from services.scheduler import SchedulerService

router = Router(name="common")

#: Страховочный роутер: подключается в ``main.register_routers`` **последним**,
#: поэтому сюда попадают только сообщения, которые не взял ни один хендлер.
fallback_router = Router(name="fallback")

#: Тексты кнопок reply-меню: их нельзя трактовать как ответ на вопрос бота.
MENU_TEXTS: frozenset[str] = frozenset(
    {
        texts.BTN_MENU_WORD,
        texts.BTN_MENU_STATS,
        texts.BTN_MENU_SETTINGS,
        texts.BTN_MENU_HELP,
        texts.BTN_MENU_PAUSE,
        texts.BTN_MENU_RESUME,
    }
)

#: Ключ FSM-данных, в котором хранится начало окна (между двумя шагами ввода).
WINDOW_START_KEY = "window_start"

#: Текстовый ввод пользователя: команды и кнопки reply-меню ответом не считаются.
TEXT_INPUT = F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_TEXTS)

#: Ответ Telegram, когда сообщение правится тем же текстом (не является ошибкой).
NOT_MODIFIED_MARKER = "message is not modified"


def escape(value: str) -> str:
    """Экранирует текст для MarkdownV2 (тексты карточек)."""
    return markdown_decoration.quote(value)


def command_filter(name: str) -> Command:
    """Фильтр команды по её текстовому виду: ``"/start"`` → ``Command("start")``.

    В текстах (``locales.ru``) команды хранятся со слэшем, а aiogram сверяет с
    названием команды **без** префикса, поэтому ``Command("/start")`` не
    совпадает ни с одной командой Telegram и бот молча игнорирует её.
    """
    return Command(name.removeprefix("/"))


def parse_words_per_day(raw: str | None) -> int | None:
    """Разбирает количество слов в день; ``None`` — если значение вне диапазона."""
    candidate = (raw or "").strip()
    if not candidate.isdigit():
        return None
    value = int(candidate)
    if config.WORDS_PER_DAY_MIN <= value <= config.WORDS_PER_DAY_MAX:
        return value
    return None


def parse_learning_limit(raw: str | None) -> int | None:
    """Разбирает лимит слов в изучении; ``None`` — если значение недопустимо.

    ``0`` — «без ограничения», остальные значения — от ``LEARNING_LIMIT_MIN`` до
    ``LEARNING_LIMIT_MAX``.
    """
    candidate = (raw or "").strip()
    if not candidate.isdigit():
        return None
    value = int(candidate)
    if value == config.LEARNING_LIMIT_UNLIMITED:
        return value
    if config.LEARNING_LIMIT_MIN <= value <= config.LEARNING_LIMIT_MAX:
        return value
    return None


async def no_word_text(session: AsyncSession, user: User, now: datetime | None = None) -> str:
    """Почему слово не пришло: пустой словарь, план дня, лимит в изучении, фильтр.

    Отдельный случай — «план дня выполнен»: слова дня уже приходили, а каждое слово
    отдаётся вручную не больше лимита показов за сутки
    (``config.ON_DEMAND_MAX_SENDS_PER_DAY``, см.
    :func:`services.word_selector.on_demand_exclusions`). Тогда честнее сказать об
    этом прямо, чем отправлять «не нашёл подходящего слова» — которое намекает на
    фильтр по части речи или на пустой словарь. Признак именно этого случая —
    сегодняшние показы (:func:`services.word_selector.words_shown_today`).

    Лимит слов в изучении проверяется последним: пока в плане дня были слова,
    ручной запрос отдавал их (:func:`services.delivery.deliver_on_demand`), поэтому
    настоящая причина пустой очереди — именно сегодняшние показы, а не лимит.
    """
    if await repository.count_words(session) == 0:
        return texts.NO_WORDS_IN_DATABASE

    if await word_selector.words_shown_today(session, user, now or utcnow()):
        return texts.WORD_PLAN_DONE_TODAY

    if user.learning_limit_enabled:
        learning = await repository.count_learning_user_words(session, user)
        if learning >= user.learning_limit:
            return texts.NEW_WORDS_LIMIT_REACHED.format(
                learning=learning,
                limit=user.learning_limit,
                stats=texts.CMD_STATS,
            )

    return texts.WORD_NOT_FOUND


async def safe_edit_text(
    message: MaybeInaccessibleMessage | None,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
    *,
    parse_mode: str | None = None,
) -> bool:
    """Правит сообщение с инлайн-клавиатурой.

    :param parse_mode: разметка текста; карточки слов приходят в MarkdownV2,
        служебные экраны (настройки, статистика) — обычным текстом.
    :return: ``True``, если текст обновлён; ``False`` — если сообщение править
        нельзя (слишком старое/недоступное). Повторная отрисовка того же экрана
        ошибкой не считается.
    """
    if not isinstance(message, Message):
        return False
    try:
        await message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramBadRequest as error:
        if NOT_MODIFIED_MARKER not in str(error):
            raise
    return True


async def drop_inline_keyboard(message: MaybeInaccessibleMessage | None) -> None:
    """Убирает инлайн-кнопки у сообщения (после ответа или завершения настройки)."""
    if not isinstance(message, Message):
        return
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        logger.debug("Не удалось убрать инлайн-клавиатуру у сообщения")


def user_name(message: Message) -> str:
    """Имя пользователя для приветствия."""
    if message.from_user is None:
        return ""
    return message.from_user.first_name or message.from_user.full_name


def menu_markup(user: User | None) -> ReplyKeyboardMarkup:
    """Reply-меню с учетом состояния паузы."""
    return main_menu_keyboard(is_paused=bool(user and user.paused))


def next_word_suffix(user: User, moment: datetime | None) -> str:
    """Дополняет ответ информацией о ближайшем слове (если слот посчитан)."""
    if moment is None:
        return ""
    return texts.NEXT_WORD_AT.format(
        moment=slots.local_time_string(user, moment),
        timezone=user.timezone,
    )


async def load_user(session: AsyncSession, message: Message) -> User | None:
    """Пользователь по ``telegram_id``; если его нет — подсказка про ``/start``."""
    telegram_id = message.chat.id
    user = await repository.get_user_by_telegram_id(session, telegram_id)
    if user is None:
        await message.answer(texts.render_not_registered())
    return user


async def load_user_from_callback(session: AsyncSession, callback: CallbackQuery) -> User | None:
    """То же, что :func:`load_user`, но для инлайн-кнопок."""
    user = await repository.get_user_by_telegram_id(session, callback.from_user.id)
    if user is None:
        await callback.answer(texts.render_not_registered(), show_alert=True)
    return user


def window_error_text(check: slots.WindowCheck, start: time, words_per_day: int) -> str:
    """Текст ошибки для некорректного временного окна."""
    if check.reason == slots.WINDOW_REASON_END_BEFORE_START:
        return texts.WINDOW_END_BEFORE_START.format(start=slots.format_time(start))
    return texts.WINDOW_TOO_SHORT.format(
        words_per_day=words_per_day,
        min_minutes=check.required_minutes,
        current_minutes=check.current_minutes,
    )


@router.message(command_filter(texts.CMD_HELP))
@router.message(lambda message: message.text == texts.BTN_MENU_HELP)
async def cmd_help(message: Message, session: AsyncSession) -> None:
    """Справка по боту и списку команд."""
    user = await repository.get_user_by_telegram_id(session, message.chat.id)
    await message.answer(texts.render_help(), reply_markup=menu_markup(user))


@router.message(command_filter(texts.CMD_CANCEL))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Прерывает пошаговый ввод (регистрация или настройки)."""
    await state.clear()
    await message.answer(texts.REGISTRATION_CANCELED.format(start=texts.CMD_START))


@router.message(command_filter(texts.CMD_PAUSE))
@router.message(lambda message: message.text == texts.BTN_MENU_PAUSE)
async def cmd_pause(message: Message, session: AsyncSession, scheduler: SchedulerService) -> None:
    """Ставит рассылку на паузу и снимает задачу из планировщика."""
    user = await load_user(session, message)
    if user is None:
        return
    if user.paused:
        await message.answer(texts.ALREADY_PAUSED, reply_markup=menu_markup(user))
        return

    await repository.update_user(session, user, paused=True)
    scheduler.unschedule_user(user.id)
    logger.info("Пользователь {} поставил рассылку на паузу", user.telegram_id)
    await message.answer(texts.PAUSED.format(resume=texts.CMD_RESUME), reply_markup=menu_markup(user))


@router.message(command_filter(texts.CMD_RESUME))
@router.message(lambda message: message.text == texts.BTN_MENU_RESUME)
async def cmd_resume(message: Message, session: AsyncSession, scheduler: SchedulerService) -> None:
    """Снимает паузу и планирует ближайший слот."""
    user = await load_user(session, message)
    if user is None:
        return
    if not user.paused:
        await message.answer(texts.ALREADY_ACTIVE, reply_markup=menu_markup(user))
        return

    await repository.update_user(session, user, paused=False)
    moment = await scheduler.schedule_user(session, user)
    logger.info("Пользователь {} возобновил рассылку", user.telegram_id)
    await message.answer(
        texts.RESUMED.format(window_start=user.window_start, window_end=user.window_end)
        + next_word_suffix(user, moment),
        reply_markup=menu_markup(user),
    )


@fallback_router.message()
async def on_unhandled_message(message: Message) -> None:
    """Логирует сообщение, которое не обработал ни один хендлер.

    aiogram в этом случае молчит: в ``bot.log`` не появляется ни строки, поэтому
    опечатку в команде или неизвестный текст иначе никак не заметить.
    """
    logger.warning(
        "Сообщение без обработчика: chat_id={}, text={!r}",
        message.chat.id,
        message.text or message.caption,
    )


@fallback_router.callback_query()
async def on_unhandled_callback(callback: CallbackQuery) -> None:
    """Логирует нажатие кнопки, которое не обработал ни один хендлер.

    Такое нажатие раньше было невидимым: aiogram о нём не пишет, индикатор на
    кнопке висит, и со стороны это выглядит как «бот завис». Именно так молчала
    кнопка «✍️ Ввести вручную», у которой был неверный формат ``callback_data``.

    ``callback.answer()`` обязателен: он гасит индикатор загрузки на кнопке.
    """
    logger.warning(
        "Кнопка без обработчика: chat_id={}, data={!r}",
        callback.message.chat.id if isinstance(callback.message, Message) else None,
        callback.data,
    )
    try:
        await callback.answer()
    except TelegramBadRequest as error:
        logger.debug("Кнопка без обработчика: ответ не доставлен ({})", error)


async def on_error(event: ErrorEvent) -> None:
    """Логирует необработанную ошибку и вежливо сообщает о ней пользователю."""
    logger.opt(exception=event.exception).error("Необработанная ошибка: {}", event.exception)
    update = event.update
    try:
        if isinstance(update.message, Message):
            await update.message.answer(texts.ERROR_GENERIC)
        elif isinstance(update.callback_query, CallbackQuery):
            await update.callback_query.answer(texts.ERROR_GENERIC, show_alert=True)
    except Exception:
        logger.exception("Не удалось сообщить об ошибке пользователю")


def register_error_handlers(dispatcher: Dispatcher) -> None:
    """Подключает обработчик ошибок к диспетчеру."""
    dispatcher.errors.register(on_error)

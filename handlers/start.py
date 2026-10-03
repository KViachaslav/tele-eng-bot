"""Регистрация пользователя: команда ``/start`` и три шага настройки.

Шаги: часовой пояс → количество слов в день → временное окно. Значение можно
выбрать инлайн-кнопкой или прислать текстом. После последнего шага бот планирует
ближайший слот рассылки и показывает reply-меню.
"""
from __future__ import annotations

from datetime import time

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    MaybeInaccessibleMessage,
    Message,
)
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
import locales.ru as texts
from db import repository
from db.models import User
from handlers import common
from keyboards.callbacks import RegistrationCallback
from keyboards.inline import timezone_keyboard, window_keyboard, words_per_day_keyboard
from keyboards.inline import decode_window_value
from services import slots
from services.scheduler import SchedulerService

router = Router(name="start")

#: Ключ FSM-данных с началом окна: общий для регистрации и меню настроек.
WINDOW_START_KEY = common.WINDOW_START_KEY

#: Фильтр текстового ввода: команды и кнопки reply-меню ответом не считаются.
TEXT_INPUT = common.TEXT_INPUT


class RegistrationStates(StatesGroup):
    """Шаги регистрации пользователя."""

    timezone = State()
    words_per_day = State()
    window_start = State()
    window_end = State()


def _words_question() -> str:
    """Вопрос о количестве слов в день."""
    return texts.ASK_WORDS_PER_DAY.format(
        min_words=config.WORDS_PER_DAY_MIN,
        max_words=config.WORDS_PER_DAY_MAX,
        default_words=config.WORDS_PER_DAY_DEFAULT,
    )


def _words_error() -> str:
    """Подсказка при некорректном количестве слов."""
    return texts.WORDS_PER_DAY_INVALID.format(
        min_words=config.WORDS_PER_DAY_MIN,
        max_words=config.WORDS_PER_DAY_MAX,
    )


def _summary(user: User) -> str:
    """Итоговое сообщение регистрации с выбранными настройками."""
    return texts.REGISTRATION_DONE.format(
        timezone=user.timezone,
        words_per_day=user.words_per_day,
        window_start=user.window_start,
        window_end=user.window_end,
        reveal_mode=texts.REVEAL_MODE_TITLES.get(user.reveal_mode, user.reveal_mode),
        word_button=texts.BTN_MENU_WORD,
        word_command=texts.CMD_WORD,
    )


async def _save_timezone(session: AsyncSession, user: User, timezone: str) -> None:
    """Сохраняет часовой пояс пользователя."""
    await repository.update_user(session, user, timezone=timezone)
    logger.info("Пользователь {} выбрал часовой пояс {}", user.telegram_id, timezone)


async def _save_window(session: AsyncSession, user: User, start: time, end: time) -> None:
    """Сохраняет временное окно рассылки."""
    await repository.update_user(
        session,
        user,
        window_start=slots.format_time(start),
        window_end=slots.format_time(end),
    )
    logger.info(
        "Пользователь {} выбрал окно {}–{}", user.telegram_id, user.window_start, user.window_end
    )


async def _finish_registration(
    bot: Bot,
    chat_id: int,
    state: FSMContext,
    user: User,
    session: AsyncSession,
    scheduler: SchedulerService,
    *,
    source: MaybeInaccessibleMessage | None = None,
) -> None:
    """Завершает регистрацию: планирует слот и показывает итог вместе с меню."""
    await state.clear()
    moment = await scheduler.schedule_user(session, user)
    await common.drop_inline_keyboard(source)
    await bot.send_message(
        chat_id,
        _summary(user) + common.next_word_suffix(user, moment),
        reply_markup=common.menu_markup(user),
    )
    logger.info(
        "Пользователь {} завершил регистрацию: {} слов, окно {}–{}, часовой пояс {}",
        user.telegram_id,
        user.words_per_day,
        user.window_start,
        user.window_end,
        user.timezone,
    )


def _chat_id(callback: CallbackQuery) -> int:
    """Чат, в который бот отправит следующее сообщение."""
    if callback.message is None:
        return callback.from_user.id
    return callback.message.chat.id


async def _render(
    target: Message | CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Показывает шаг регистрации: правит текущее сообщение или пишет новое."""
    if isinstance(target, CallbackQuery):
        await common.safe_edit_text(target.message, text, markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _goto_timezone(target: Message | CallbackQuery, state: FSMContext) -> None:
    """Шаг 1: выбор часового пояса."""
    await state.set_state(RegistrationStates.timezone)
    await _render(target, texts.ASK_TIMEZONE.format(manual=texts.BTN_MANUAL), timezone_keyboard())


async def _goto_words(target: Message | CallbackQuery, state: FSMContext) -> None:
    """Шаг 2: количество слов в день."""
    await state.set_state(RegistrationStates.words_per_day)
    await _render(target, _words_question(), words_per_day_keyboard(allow_default=True))


async def _goto_window(target: Message | CallbackQuery, state: FSMContext) -> None:
    """Шаг 3: временное окно — пресеты кнопками или два шага ручного ввода."""
    await state.clear()
    await _render(target, texts.ASK_WINDOW, window_keyboard())


async def _save_words(session: AsyncSession, user: User, words: int) -> None:
    """Сохраняет количество слов в день."""
    await repository.update_user(session, user, words_per_day=words)
    logger.info("Пользователь {} выбрал {} слов в день", user.telegram_id, words)


@router.message(common.command_filter(texts.CMD_START))
async def cmd_start(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Регистрация нового пользователя или приветствие уже настроенного."""
    user = await repository.get_user_by_telegram_id(session, message.chat.id)
    if user is not None:
        await state.clear()
        await message.answer(
            texts.render_start_existing(
                name=common.user_name(message),
                timezone=user.timezone,
                words_per_day=user.words_per_day,
                window_start=user.window_start,
                window_end=user.window_end,
            ),
            reply_markup=common.menu_markup(user),
        )
        return

    user = await repository.create_user(session, message.chat.id)
    logger.info("Пользователь {} начал регистрацию", user.telegram_id)
    await message.answer(
        texts.START_NEW_USER.format(
            name=common.user_name(message),
            know=texts.BTN_KNOW,
            dont_know=texts.BTN_DONT_KNOW,
        )
    )
    await _goto_timezone(message, state)


@router.callback_query(RegistrationCallback.filter(F.action == config.ACTION_TIMEZONE))
async def on_timezone_chosen(
    callback: CallbackQuery,
    callback_data: RegistrationCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Часовой пояс выбран пресетом или запрошен ручной ввод."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == config.ACTION_MANUAL:
        await state.set_state(RegistrationStates.timezone)
        await common.safe_edit_text(callback.message, texts.ASK_TIMEZONE_MANUAL)
        await callback.answer()
        return

    timezone = slots.parse_timezone(callback_data.value)
    if timezone is None:
        await callback.answer(
            texts.TIMEZONE_INVALID.format(value=callback_data.value), show_alert=True
        )
        return

    await _save_timezone(session, user, timezone)
    await _goto_words(callback, state)
    await callback.answer()


@router.message(RegistrationStates.timezone, TEXT_INPUT)
async def on_timezone_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Ручной ввод часового пояса на первом шаге."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    raw = message.text or ""
    timezone = slots.parse_timezone(raw)
    if timezone is None:
        await message.answer(texts.TIMEZONE_INVALID.format(value=raw))
        return

    await _save_timezone(session, user, timezone)
    await _goto_words(message, state)


@router.callback_query(RegistrationCallback.filter(F.action == config.ACTION_WORDS_PER_DAY))
async def on_words_chosen(
    callback: CallbackQuery,
    callback_data: RegistrationCallback,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    """Количество слов выбрано кнопкой: пресет, «по умолчанию» или ручной ввод."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == config.ACTION_MANUAL:
        await state.set_state(RegistrationStates.words_per_day)
        await common.safe_edit_text(callback.message, _words_question())
        await callback.answer()
        return

    if callback_data.value == config.ACTION_SKIP:
        words = config.WORDS_PER_DAY_DEFAULT
    else:
        words = common.parse_words_per_day(callback_data.value)
        if words is None:
            await callback.answer(_words_error(), show_alert=True)
            return

    await _save_words(session, user, words)
    await _goto_window(callback, state)
    await callback.answer()


@router.message(RegistrationStates.words_per_day, TEXT_INPUT)
async def on_words_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Ручной ввод количества слов в день."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    words = common.parse_words_per_day(message.text)
    if words is None:
        await message.answer(_words_error())
        return

    await _save_words(session, user, words)
    await _goto_window(message, state)


@router.callback_query(RegistrationCallback.filter(F.action == config.ACTION_WINDOW))
async def on_window_chosen(
    callback: CallbackQuery,
    callback_data: RegistrationCallback,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
    bot: Bot,
) -> None:
    """Шаг 3: окно выбрано пресетом или запущен ручной ввод (два шага)."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == config.ACTION_MANUAL:
        await state.set_state(RegistrationStates.window_start)
        await common.safe_edit_text(callback.message, texts.ASK_WINDOW_START)
        await callback.answer()
        return

    decoded = decode_window_value(callback_data.value)
    if decoded is None:
        await callback.answer(texts.WINDOW_INVALID, show_alert=True)
        return

    start, end = decoded
    check = slots.check_window(start, end, user.words_per_day)
    if not check.ok:
        await callback.answer(
            common.window_error_text(check, start, user.words_per_day), show_alert=True
        )
        return

    await _save_window(session, user, start, end)
    await _finish_registration(
        bot,
        _chat_id(callback),
        state,
        user,
        session,
        scheduler,
        source=callback.message,
    )
    await callback.answer()


@router.message(RegistrationStates.window_start, TEXT_INPUT)
async def on_window_start_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Ручной ввод начала окна: запоминаем начало и просим конец."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    start = slots.parse_time(message.text or "")
    if start is None:
        await message.answer(texts.WINDOW_INVALID)
        return

    start_text = slots.format_time(start)
    await state.update_data(**{WINDOW_START_KEY: start_text})
    await state.set_state(RegistrationStates.window_end)
    await message.answer(texts.ASK_WINDOW_END.format(start=start_text))


@router.message(RegistrationStates.window_end, TEXT_INPUT)
async def on_window_end_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
    bot: Bot,
) -> None:
    """Ручной ввод конца окна: сохраняем окно и завершаем регистрацию."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    data = await state.get_data()
    start = slots.parse_time(str(data.get(WINDOW_START_KEY, "")))
    if start is None:
        await state.set_state(RegistrationStates.window_start)
        await message.answer(texts.ASK_WINDOW_START)
        return

    end = slots.parse_time(message.text or "")
    if end is None:
        await message.answer(texts.WINDOW_INVALID)
        return

    check = slots.check_window(start, end, user.words_per_day)
    if not check.ok:
        await message.answer(common.window_error_text(check, start, user.words_per_day))
        return

    await _save_window(session, user, start, end)
    await _finish_registration(bot, message.chat.id, state, user, session, scheduler)


@router.callback_query(RegistrationCallback.filter(F.action == config.ACTION_BACK))
async def on_back(callback: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    """Возвращает на предыдущий шаг регистрации."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    current = await state.get_state()
    if current in (RegistrationStates.window_start.state, RegistrationStates.window_end.state):
        await _goto_words(callback, state)
    else:
        await _goto_timezone(callback, state)
    await callback.answer()

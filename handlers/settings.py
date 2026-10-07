"""Меню настроек: слова в день, окно рассылки, часовой пояс, поля, фильтр, режим.

Кнопки меню настроек приходят с префиксом ``settings:``; нажатие на пункт
верхнего уровня (например «🔢 Слов в день») открывает подэкран, а нажатие на
конкретное значение — сразу применяет его. Пустое значение в callback-данных
означает «показать подэкран».
"""
from __future__ import annotations

from datetime import datetime, time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
import locales.ru as texts
from db import repository
from db.models import User, UserSettings, utcnow
from handlers import common
from keyboards.callbacks import SettingsCallback
from keyboards.inline import (
    decode_window_value,
    fields_keyboard,
    forecast_keyboard,
    learning_limit_keyboard,
    mode_keyboard,
    pos_filter_keyboard,
    settings_callback,
    settings_menu_keyboard,
    timezone_keyboard,
    window_keyboard,
    words_per_day_keyboard,
)
from services import plan, slots
from services.scheduler import SchedulerService

router = Router(name="settings")


class SettingsStates(StatesGroup):
    """Шаги ручного ввода в меню настроек."""

    words_per_day = State()
    learning_limit = State()
    window_start = State()
    window_end = State()
    timezone = State()


def _words_error() -> str:
    """Подсказка при некорректном количестве слов."""
    return texts.WORDS_PER_DAY_INVALID.format(
        min_words=config.WORDS_PER_DAY_MIN,
        max_words=config.WORDS_PER_DAY_MAX,
    )


def _words_question() -> str:
    """Вопрос о количестве слов в день в меню настроек."""
    return texts.ASK_WORDS_PER_DAY_CHANGE.format(
        min_words=config.WORDS_PER_DAY_MIN,
        max_words=config.WORDS_PER_DAY_MAX,
    )


def _limit_error() -> str:
    """Подсказка при некорректном лимите слов в изучении."""
    return texts.LEARNING_LIMIT_INVALID.format(
        min_words=config.LEARNING_LIMIT_MIN,
        max_words=config.LEARNING_LIMIT_MAX,
    )


def _limit_question() -> str:
    """Вопрос о лимите слов в изучении в меню настроек."""
    return texts.ASK_LEARNING_LIMIT_CHANGE.format(
        min_words=config.LEARNING_LIMIT_MIN,
        max_words=config.LEARNING_LIMIT_MAX,
        unlimited=texts.LEARNING_LIMIT_UNLIMITED_TITLE,
    )


def _short_titles(values: list[str], titles: dict[str, str]) -> str:
    """Подписи выбранных пунктов без эмодзи: ``Definition, Example``."""
    clean = [titles[value].split(" ", 1)[-1] for value in values if value in titles]
    return ", ".join(clean)


def _menu_text(user: User, settings: UserSettings) -> str:
    """Сводка текущих настроек для экрана ``/settings``."""
    selected = user.pos_filter_values
    return texts.SETTINGS_MENU.format(
        timezone=user.timezone,
        words_per_day=user.words_per_day,
        learning_limit=texts.render_learning_limit(user.learning_limit),
        window_start=user.window_start,
        window_end=user.window_end,
        reveal_mode=texts.REVEAL_MODE_TITLES.get(user.reveal_mode, user.reveal_mode),
        pos_filter=_short_titles(selected, texts.POS_TITLES) or texts.BTN_POS_ALL,
        fields=_short_titles(settings.enabled_fields(), texts.FIELD_TITLES),
        state=texts.SETTINGS_STATE_PAUSED if user.paused else texts.SETTINGS_STATE_ACTIVE,
    )


def _pos_text() -> str:
    """Текст экрана выбора части речи."""
    return texts.POS_MENU.format(values=", ".join(texts.POS_TITLES.values()))


def _forecast_text(user: User, days: list[plan.DayPlan]) -> str:
    """Текст экрана «📅 План слов»: слова сегодня и в ближайшие дни."""
    today = days[0]
    upcoming = "\n".join(
        texts.FORECAST_DAY_LINE.format(
            date=day.local_day.strftime(config.DATE_FORMAT),
            words=texts.render_words(day.total),
            new=day.new_words,
            reviews=day.reviews,
        )
        for day in days[1:]
    )
    return texts.FORECAST_MENU.format(
        date=today.local_day.strftime(config.DATE_FORMAT),
        words=texts.render_words(today.total),
        new=today.new_words,
        reviews=today.reviews,
        upcoming=upcoming or config.EMPTY_VALUE_MARKER,
    ) + texts.FORECAST_HINT


def _notice(user: User, value: object, moment: datetime | None) -> str:
    """Подтверждение сохранения с подсказкой о ближайшем слове."""
    return texts.SETTING_SAVED.format(value=value) + common.next_word_suffix(user, moment)


async def _menu_state(session: AsyncSession, user: User) -> tuple[str, UserSettings]:
    """Текст и настройки пользователя для отрисовки экрана ``/settings``."""
    settings = await repository.get_or_create_user_settings(session, user)
    return _menu_text(user, settings), settings


async def _edit_menu(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User,
    *,
    notice: str | None = None,
) -> None:
    """Перерисовывает главный экран настроек (с необязательным уведомлением)."""
    text, settings = await _menu_state(session, user)
    if notice:
        text = f"{notice}\n\n{text}"
    await common.safe_edit_text(callback.message, text, settings_menu_keyboard(user, settings))


async def _send_menu(
    message: Message,
    session: AsyncSession,
    user: User,
    *,
    notice: str | None = None,
) -> None:
    """Отправляет главный экран настроек новым сообщением."""
    text, settings = await _menu_state(session, user)
    if notice:
        text = f"{notice}\n\n{text}"
    await message.answer(text, reply_markup=settings_menu_keyboard(user, settings))


@router.message(common.command_filter(texts.CMD_SETTINGS))
@router.message(F.text == texts.BTN_MENU_SETTINGS)
async def cmd_settings(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Показывает меню настроек."""
    user = await common.load_user(session, message)
    if user is None:
        return
    await state.clear()
    await _send_menu(message, session, user)


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_CLOSE))
async def on_close(callback: CallbackQuery, session: AsyncSession) -> None:
    """Закрывает меню настроек."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return
    await common.safe_edit_text(callback.message, texts.SETTINGS_CLOSED)
    await callback.answer()


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_BACK))
async def on_back(callback: CallbackQuery, session: AsyncSession) -> None:
    """Возвращает на главный экран настроек."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return
    await _edit_menu(callback, session, user)
    await callback.answer()


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_FORECAST))
async def on_forecast(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает, сколько слов придёт сегодня и в ближайшие дни.

    Экран только читает настройки и расписание: он ничего не меняет, поэтому
    кнопка стоит рядом с «🕘 Временное окно» — там же, где настраивается график.
    Слова считает :func:`services.plan.upcoming_plan`: прогноз строится по
    текущему плану дня и срокам повторений.
    """
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return
    days = await plan.upcoming_plan(session, user, utcnow(), config.FORECAST_DAYS)
    await common.safe_edit_text(
        callback.message, _forecast_text(user, days), forecast_keyboard()
    )
    await callback.answer()


async def _save_words(session: AsyncSession, user: User, words: int) -> None:
    """Сохраняет новое количество слов в день."""
    await repository.update_user(session, user, words_per_day=words)
    logger.info("Пользователь {} изменил норму: {} слов в день", user.telegram_id, words)


async def _save_learning_limit(session: AsyncSession, user: User, limit: int) -> None:
    """Сохраняет лимит слов в изучении (``0`` — без ограничения)."""
    await repository.update_user(session, user, learning_limit=limit)
    logger.info(
        "Пользователь {} изменил лимит слов в изучении: {}",
        user.telegram_id,
        texts.render_learning_limit(limit),
    )


async def _save_window(session: AsyncSession, user: User, start: time, end: time) -> None:
    """Сохраняет новое временное окно рассылки."""
    await repository.update_user(
        session,
        user,
        window_start=slots.format_time(start),
        window_end=slots.format_time(end),
    )
    logger.info(
        "Пользователь {} изменил окно: {}–{}",
        user.telegram_id,
        user.window_start,
        user.window_end,
    )


def _window_title(user: User) -> str:
    """Подпись окна для подтверждения сохранения."""
    return f"{user.window_start}–{user.window_end}"


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_WORDS_PER_DAY))
async def on_words_selected(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Открывает подэкран «Слов в день» или применяет выбранное значение."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == config.ACTION_MANUAL:
        await state.set_state(SettingsStates.words_per_day)
        await common.safe_edit_text(callback.message, _words_question())
        await callback.answer()
        return

    if not callback_data.value:
        await state.clear()
        await common.safe_edit_text(
            callback.message,
            _words_question(),
            words_per_day_keyboard(pack=settings_callback, back=True, allow_default=True),
        )
        await callback.answer()
        return

    if callback_data.value == config.ACTION_SKIP:
        words = config.WORDS_PER_DAY_DEFAULT
    else:
        parsed = common.parse_words_per_day(callback_data.value)
        if parsed is None:
            await callback.answer(_words_error(), show_alert=True)
            return
        words = parsed

    await _save_words(session, user, words)
    moment = await scheduler.schedule_user(session, user)
    await _edit_menu(callback, session, user, notice=_notice(user, words, moment))
    await callback.answer()


@router.message(SettingsStates.words_per_day, common.TEXT_INPUT)
async def on_words_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Ручной ввод количества слов в день из меню настроек."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    words = common.parse_words_per_day(message.text)
    if words is None:
        await message.answer(_words_error())
        return

    await _save_words(session, user, words)
    moment = await scheduler.schedule_user(session, user)
    await state.clear()
    await _send_menu(message, session, user, notice=_notice(user, words, moment))


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_LEARNING_LIMIT))
async def on_learning_limit_selected(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Открывает подэкран «Лимит в изучении» или применяет выбранное значение.

    Лимит влияет на дневной план (при достигнутом лимите новых слов нет), поэтому
    расписание пересчитывается.
    """
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == config.ACTION_MANUAL:
        await state.set_state(SettingsStates.learning_limit)
        await common.safe_edit_text(callback.message, _limit_question())
        await callback.answer()
        return

    if not callback_data.value:
        await state.clear()
        await common.safe_edit_text(
            callback.message,
            _limit_question(),
            learning_limit_keyboard(pack=settings_callback, back=True),
        )
        await callback.answer()
        return

    limit = common.parse_learning_limit(callback_data.value)
    if limit is None:
        await callback.answer(_limit_error(), show_alert=True)
        return

    await _save_learning_limit(session, user, limit)
    moment = await scheduler.schedule_user(session, user)
    await _edit_menu(
        callback,
        session,
        user,
        notice=_notice(user, texts.render_learning_limit(limit), moment),
    )
    await callback.answer()


@router.message(SettingsStates.learning_limit, common.TEXT_INPUT)
async def on_learning_limit_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Ручной ввод лимита слов в изучении."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    limit = common.parse_learning_limit(message.text)
    if limit is None:
        await message.answer(_limit_error())
        return

    await _save_learning_limit(session, user, limit)
    moment = await scheduler.schedule_user(session, user)
    await state.clear()
    await _send_menu(
        message,
        session,
        user,
        notice=_notice(user, texts.render_learning_limit(limit), moment),
    )


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_WINDOW))
async def on_window_selected(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Открывает подэкран «Временное окно» или применяет выбранный пресет."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == config.ACTION_MANUAL:
        await state.set_state(SettingsStates.window_start)
        await common.safe_edit_text(callback.message, texts.ASK_WINDOW_START)
        await callback.answer()
        return

    if not callback_data.value:
        await state.clear()
        await common.safe_edit_text(
            callback.message,
            texts.ASK_WINDOW_CHANGE,
            window_keyboard(pack=settings_callback, back=True),
        )
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
    moment = await scheduler.schedule_user(session, user)
    await _edit_menu(callback, session, user, notice=_notice(user, _window_title(user), moment))
    await callback.answer()


@router.message(SettingsStates.window_start, common.TEXT_INPUT)
async def on_window_start_text(message: Message, state: FSMContext, session: AsyncSession) -> None:
    """Ручной ввод начала окна в настройках: запоминает и просит конец."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    start = slots.parse_time(message.text or "")
    if start is None:
        await message.answer(texts.WINDOW_INVALID)
        return

    start_text = slots.format_time(start)
    await state.update_data(**{common.WINDOW_START_KEY: start_text})
    await state.set_state(SettingsStates.window_end)
    await message.answer(texts.ASK_WINDOW_END.format(start=start_text))


@router.message(SettingsStates.window_end, common.TEXT_INPUT)
async def on_window_end_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Ручной ввод конца окна: сохраняет окно и показывает настройки."""
    user = await common.load_user(session, message)
    if user is None:
        await state.clear()
        return

    data = await state.get_data()
    start = slots.parse_time(str(data.get(common.WINDOW_START_KEY, "")))
    if start is None:
        await state.set_state(SettingsStates.window_start)
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
    moment = await scheduler.schedule_user(session, user)
    await state.clear()
    await _send_menu(message, session, user, notice=_notice(user, _window_title(user), moment))


async def _save_timezone(session: AsyncSession, user: User, timezone: str) -> None:
    """Сохраняет новый часовой пояс."""
    await repository.update_user(session, user, timezone=timezone)
    logger.info("Пользователь {} изменил часовой пояс: {}", user.telegram_id, timezone)


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_TIMEZONE))
async def on_timezone_selected(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Открывает подэкран «Часовой пояс» или применяет выбранное значение."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == config.ACTION_MANUAL:
        await state.set_state(SettingsStates.timezone)
        await common.safe_edit_text(callback.message, texts.ASK_TIMEZONE_MANUAL)
        await callback.answer()
        return

    if not callback_data.value:
        await state.clear()
        await common.safe_edit_text(
            callback.message,
            texts.ASK_TIMEZONE_CHANGE.format(manual=texts.BTN_MANUAL),
            timezone_keyboard(pack=settings_callback, back=True),
        )
        await callback.answer()
        return

    timezone = slots.parse_timezone(callback_data.value)
    if timezone is None:
        await callback.answer(
            texts.TIMEZONE_INVALID.format(value=callback_data.value), show_alert=True
        )
        return

    await _save_timezone(session, user, timezone)
    moment = await scheduler.schedule_user(session, user)
    await _edit_menu(callback, session, user, notice=_notice(user, timezone, moment))
    await callback.answer()


@router.message(SettingsStates.timezone, common.TEXT_INPUT)
async def on_timezone_text(
    message: Message,
    state: FSMContext,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Ручной ввод часового пояса из меню настроек."""
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
    moment = await scheduler.schedule_user(session, user)
    await state.clear()
    await _send_menu(message, session, user, notice=_notice(user, timezone, moment))


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_FIELDS))
async def on_fields(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает экран выбора полей карточки."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return
    settings = await repository.get_or_create_user_settings(session, user)
    await common.safe_edit_text(callback.message, texts.FIELDS_MENU, fields_keyboard(settings))
    await callback.answer()


@router.callback_query(
    SettingsCallback.filter((F.action == config.ACTION_TOGGLE) & F.value.in_(config.FIELD_NAMES))
)
async def on_field_toggle(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    session: AsyncSession,
) -> None:
    """Включает или выключает одно поле карточки."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    settings = await repository.get_or_create_user_settings(session, user)
    enabled = not bool(getattr(settings, callback_data.value))
    await repository.update_user_settings(session, settings, **{callback_data.value: enabled})
    logger.info(
        "Пользователь {} переключил поле {}: {}", user.telegram_id, callback_data.value, enabled
    )
    await common.safe_edit_text(callback.message, texts.FIELDS_MENU, fields_keyboard(settings))
    await callback.answer()


@router.callback_query(SettingsCallback.filter(F.action == config.ACTION_POS))
async def on_pos(callback: CallbackQuery, session: AsyncSession) -> None:
    """Показывает экран фильтра по части речи."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return
    await common.safe_edit_text(callback.message, _pos_text(), pos_filter_keyboard(user))
    await callback.answer()


@router.callback_query(
    SettingsCallback.filter(
        (F.action == config.ACTION_TOGGLE)
        & F.value.in_((*config.POS_FILTER_VALUES, config.POS_FILTER_ALL))
    )
)
async def on_pos_toggle(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Включает/выключает часть речи или сбрасывает фильтр.

    Фильтр решает, какие повторения попадут в рассылку, то есть меняет дневной
    план, поэтому расписание пересчитывается.
    """
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    selected = set(user.pos_filter_values)
    if callback_data.value == config.POS_FILTER_ALL:
        selected.clear()
    elif callback_data.value in selected:
        selected.discard(callback_data.value)
    else:
        selected.add(callback_data.value)

    await repository.set_pos_filter(session, user, selected)
    logger.info("Пользователь {} изменил фильтр частей речи: {}", user.telegram_id, selected)
    await scheduler.schedule_user(session, user)
    await common.safe_edit_text(callback.message, _pos_text(), pos_filter_keyboard(user))
    await callback.answer()


@router.callback_query(
    SettingsCallback.filter((F.action == config.ACTION_MODE) & F.value.in_(config.REVEAL_MODES))
)
async def on_mode_selected(
    callback: CallbackQuery,
    callback_data: SettingsCallback,
    session: AsyncSession,
) -> None:
    """Применяет выбранный режим или открывает экран выбора режима."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.value == user.reveal_mode:
        await common.safe_edit_text(
            callback.message, texts.MODE_MENU, mode_keyboard(user.reveal_mode)
        )
        await callback.answer()
        return

    await repository.update_user(session, user, reveal_mode=callback_data.value)
    logger.info("Пользователь {} изменил режим показа: {}", user.telegram_id, callback_data.value)
    await _edit_menu(
        callback,
        session,
        user,
        notice=texts.MODE_SAVED.format(value=texts.REVEAL_MODE_TITLES[callback_data.value]),
    )
    await callback.answer()

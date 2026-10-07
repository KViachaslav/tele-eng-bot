"""Инлайн-клавиатуры бота.

Чтобы не дублировать код для регистрации (``reg:*``) и настроек (``settings:*``),
клавиатуры принимают функцию-упаковщик callback-данных (``pack``): по умолчанию
это :func:`registration_callback`, в меню настроек передаётся
:func:`settings_callback`.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import time

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import locales.ru as texts
from db.models import User, UserSettings
from keyboards.callbacks import (
    AnswerCallback,
    AudioCallback,
    RegistrationCallback,
    SettingsCallback,
    ShowCallback,
)
from services import slots

#: Функция, собирающая строку ``callback_data`` из действия и значения.
Packer = Callable[[str, str], str]

PRESET_COLUMNS = 2


def registration_callback(action: str, value: str = "") -> str:
    """``callback_data`` для шагов регистрации."""
    return RegistrationCallback(action=action, value=value).pack()


def settings_callback(action: str, value: str = "") -> str:
    """``callback_data`` для меню настроек."""
    return SettingsCallback(action=action, value=value).pack()


def _button(title: str, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=title, callback_data=callback_data)


def _chunked(items: Sequence[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    """Раскладывает кнопки по строкам фиксированной длины."""
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


def _toggle(title: str, checked: bool) -> str:
    """Подпись пункта-переключателя с галочкой."""
    mark = texts.CHECKED_MARK if checked else texts.UNCHECKED_MARK
    return f"{mark} {title}"


def _with_tail(
    rows: list[list[InlineKeyboardButton]],
    *,
    pack: Packer,
    manual_action: str | None = None,
    back: bool = False,
) -> InlineKeyboardMarkup:
    """Добавляет завершающие строки: ручной ввод, «Назад», «Закрыть».

    Кнопка ручного ввода получает действие того шага, к которому она относится
    (``settings:window:manual``, ``reg:words:manual`` ...): хендлеры шагов
    различаются именно по действию, а ``manual`` — это их *значение*
    (см. ``handlers/settings.py`` и ``handlers/start.py``). Пока в действии стояло
    ``manual``, кнопка не совпадала ни с одним хендлером и молча не работала.
    """
    if manual_action is not None:
        rows.append([_button(texts.BTN_MANUAL, pack(manual_action, config.ACTION_MANUAL))])
    if back:
        rows.append([_button(texts.BTN_BACK, pack(config.ACTION_BACK))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def timezone_keyboard(*, pack: Packer = registration_callback, back: bool = False) -> InlineKeyboardMarkup:
    """Часовые пояса: популярные варианты + ручной ввод."""
    buttons = [
        _button(name, pack(config.ACTION_TIMEZONE, name)) for name in config.TIMEZONE_PRESETS
    ]
    return _with_tail(
        _chunked(buttons, PRESET_COLUMNS),
        pack=pack,
        manual_action=config.ACTION_TIMEZONE,
        back=back,
    )


def words_per_day_keyboard(
    *,
    pack: Packer = registration_callback,
    back: bool = False,
    allow_default: bool = False,
) -> InlineKeyboardMarkup:
    """Количество слов в день: пресеты + ручной ввод (+ «по умолчанию» на регистрации)."""
    buttons = [
        _button(str(value), pack(config.ACTION_WORDS_PER_DAY, str(value)))
        for value in config.WORDS_PER_DAY_PRESETS
    ]
    rows = _chunked(buttons, PRESET_COLUMNS)
    if allow_default:
        rows.append(
            [
                _button(
                    texts.BTN_DEFAULT.format(value=config.WORDS_PER_DAY_DEFAULT),
                    pack(config.ACTION_WORDS_PER_DAY, config.ACTION_SKIP),
                )
            ]
        )
    return _with_tail(rows, pack=pack, manual_action=config.ACTION_WORDS_PER_DAY, back=back)


def learning_limit_keyboard(
    *,
    pack: Packer = settings_callback,
    back: bool = False,
) -> InlineKeyboardMarkup:
    """Лимит слов в изучении: пресеты, «без ограничения» + ручной ввод."""
    buttons = [
        _button(str(value), pack(config.ACTION_LEARNING_LIMIT, str(value)))
        for value in config.LEARNING_LIMIT_PRESETS
    ]
    rows = _chunked(buttons, PRESET_COLUMNS)
    rows.append(
        [
            _button(
                texts.BTN_LIMIT_UNLIMITED,
                pack(config.ACTION_LEARNING_LIMIT, str(config.LEARNING_LIMIT_UNLIMITED)),
            )
        ]
    )
    return _with_tail(rows, pack=pack, manual_action=config.ACTION_LEARNING_LIMIT, back=back)


def window_keyboard(*, pack: Packer = registration_callback, back: bool = False) -> InlineKeyboardMarkup:
    """Временные окна: готовые варианты + ручной ввод."""
    buttons = [
        _button(
            texts.BTN_WINDOW_PRESET.format(start=start, end=end),
            pack(config.ACTION_WINDOW, _window_value(start, end)),
        )
        for start, end in config.WINDOW_PRESETS
    ]
    return _with_tail(
        _chunked(buttons, PRESET_COLUMNS),
        pack=pack,
        manual_action=config.ACTION_WINDOW,
        back=back,
    )


def _window_value(start: str, end: str) -> str:
    """Компактное значение окна для callback: ``0900-2100``."""
    start_time = slots.parse_time(start)
    end_time = slots.parse_time(end)
    if start_time is None or end_time is None:
        raise ValueError(f"Некорректный пресет окна: {start}-{end}")
    return (
        f"{slots.encode_time(start_time)}"
        f"{config.WINDOW_VALUE_SEPARATOR}"
        f"{slots.encode_time(end_time)}"
    )


def decode_window_value(value: str) -> tuple[time, time] | None:
    """Разбирает ``0900-2100`` обратно в пару ``(начало, конец)``; ``None`` — мусор."""
    start_raw, separator, end_raw = (value or "").partition(config.WINDOW_VALUE_SEPARATOR)
    if not separator:
        return None
    start_time = slots.decode_time(start_raw)
    end_time = slots.decode_time(end_raw)
    if start_time is None or end_time is None:
        return None
    return start_time, end_time


def settings_menu_keyboard(user: User, settings: UserSettings) -> InlineKeyboardMarkup:
    """Главное меню настроек."""
    pack = settings_callback
    fields_count = len(settings.enabled_fields())
    rows = [
        [_button(texts.BTN_SETTINGS_WORDS, pack(config.ACTION_WORDS_PER_DAY))],
        [_button(texts.BTN_SETTINGS_LIMIT, pack(config.ACTION_LEARNING_LIMIT))],
        [_button(texts.BTN_SETTINGS_WINDOW, pack(config.ACTION_WINDOW))],
        [_button(texts.BTN_SETTINGS_TIMEZONE, pack(config.ACTION_TIMEZONE))],
        [_button(texts.BTN_SETTINGS_FORECAST, pack(config.ACTION_FORECAST))],
        [_button(f"{texts.BTN_SETTINGS_FIELDS} · {fields_count}", pack(config.ACTION_FIELDS))],
        [_button(texts.BTN_SETTINGS_POS, pack(config.ACTION_POS))],
        [
            _button(
                texts.BTN_SETTINGS_MODE,
                pack(config.ACTION_MODE, user.reveal_mode),
            )
        ],
        [_button(texts.BTN_CLOSE, pack(config.ACTION_CLOSE))],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def fields_keyboard(settings: UserSettings) -> InlineKeyboardMarkup:
    """Переключатели полей карточки слова."""
    pack = settings_callback
    rows = [
        [
            _button(
                _toggle(texts.FIELD_TITLES[name], bool(getattr(settings, name))),
                pack(config.ACTION_TOGGLE, name),
            )
        ]
        for name in config.FIELD_NAMES
    ]
    rows.append([_button(texts.BTN_BACK, pack(config.ACTION_BACK))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pos_filter_keyboard(user: User) -> InlineKeyboardMarkup:
    """Фильтр по части речи (мультивыбор)."""
    pack = settings_callback
    selected = set(user.pos_filter_values)
    rows = [
        [
            _button(
                _toggle(texts.POS_TITLES[value], value in selected),
                pack(config.ACTION_TOGGLE, value),
            )
        ]
        for value in config.POS_FILTER_VALUES
    ]
    rows.append([_button(texts.BTN_POS_ALL, pack(config.ACTION_TOGGLE, config.POS_FILTER_ALL))])
    rows.append([_button(texts.BTN_BACK, pack(config.ACTION_BACK))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def mode_keyboard(current: str) -> InlineKeyboardMarkup:
    """Режим показа перевода."""
    pack = settings_callback
    rows = [
        [
            _button(
                _toggle(title, mode == current),
                pack(config.ACTION_MODE, mode),
            )
        ]
        for mode, title in texts.REVEAL_MODE_TITLES.items()
    ]
    rows.append([_button(texts.BTN_BACK, pack(config.ACTION_BACK))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def show_word_keyboard(word_id: int, delivery_id: int) -> InlineKeyboardMarkup:
    """Кнопка «Показать» для режима «сначала слово, потом перевод»."""
    callback = ShowCallback(word_id=word_id, delivery_id=delivery_id).pack()
    return InlineKeyboardMarkup(inline_keyboard=[[_button(texts.BTN_SHOW, callback)]])


def audio_keyboard_row(word_id: int, delivery_id: int) -> list[InlineKeyboardButton]:
    """Строка кнопок озвучки: по кнопке на каждый акцент (🇬🇧 UK и 🇺🇸 US)."""
    return [
        _button(
            texts.render_audio_button(accent),
            AudioCallback(word_id=word_id, delivery_id=delivery_id, accent=accent).pack(),
        )
        for accent in config.AUDIO_ACCENTS
    ]


def audio_keyboard(word_id: int, delivery_id: int) -> InlineKeyboardMarkup:
    """Клавиатура только с озвучкой: остаётся в карточке после ответа.

    Telegram убирает инлайн-клавиатуру у сообщения, если ``editMessageText``
    пришёл без ``reply_markup``. Поэтому после ответа «Знаю» / «Не знаю» карточка
    правится с этой клавиатурой: кнопки ответа исчезают (повторный клик уже не
    нужен), а озвучку можно послушать и после ответа — SRS она не меняет.
    """
    return InlineKeyboardMarkup(inline_keyboard=[audio_keyboard_row(word_id, delivery_id)])


def answer_keyboard(word_id: int, delivery_id: int) -> InlineKeyboardMarkup:
    """Кнопки «Знаю» / «Не знаю» и, во второй строке, озвучка слова (два акцента)."""
    rows = [
        [
            _button(
                texts.BTN_KNOW,
                AnswerCallback(
                    answer=config.ANSWER_KNOW, word_id=word_id, delivery_id=delivery_id
                ).pack(),
            ),
            _button(
                texts.BTN_DONT_KNOW,
                AnswerCallback(
                    answer=config.ANSWER_DONT_KNOW, word_id=word_id, delivery_id=delivery_id
                ).pack(),
            ),
        ],
        audio_keyboard_row(word_id, delivery_id),
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def forecast_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура экрана «📅 План слов»: только возврат в меню настроек.

    Экран ничего не меняет, поэтому кроме «⬅️ Назад» кнопок нет.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[[_button(texts.BTN_BACK, settings_callback(config.ACTION_BACK))]]
    )



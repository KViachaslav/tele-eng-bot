"""Постоянное reply-меню под полем ввода."""
from __future__ import annotations

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

import locales.ru as texts


def main_menu_keyboard(*, is_paused: bool = False) -> ReplyKeyboardMarkup:
    """Кнопки основных действий; вместо «Пауза» показывается «Возобновить»."""
    pause_button = texts.BTN_MENU_RESUME if is_paused else texts.BTN_MENU_PAUSE
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=texts.BTN_MENU_WORD), KeyboardButton(text=texts.BTN_MENU_STATS)],
            [KeyboardButton(text=texts.BTN_MENU_SETTINGS)],
            [KeyboardButton(text=texts.BTN_MENU_HELP), KeyboardButton(text=pause_button)],
        ],
        resize_keyboard=True,
        input_field_placeholder=texts.BTN_MENU_WORD,
    )

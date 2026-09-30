"""Фабрики callback-данных (aiogram 3 ``CallbackData``).

Единый источник формата ``callback_data`` для клавиатур и фильтров хендлеров:
``reg:<action>:<value>``, ``settings:<action>:<value>``,
``show:<word_id>:<delivery_id>``, ``answer:<answer>:<word_id>:<delivery_id>``.
"""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

import config


class RegistrationCallback(CallbackData, prefix=config.CALLBACK_REG):
    """Шаги регистрации: часовой пояс, число слов, временное окно."""

    action: str
    value: str = ""


class SettingsCallback(CallbackData, prefix=config.CALLBACK_SETTINGS):
    """Действия в меню настроек."""

    action: str
    value: str = ""


class ShowCallback(CallbackData, prefix=config.CALLBACK_SHOW):
    """Кнопка «Показать» в режиме «сначала слово, потом перевод»."""

    word_id: int
    delivery_id: int


class AnswerCallback(CallbackData, prefix=config.CALLBACK_ANSWER):
    """Кнопки «Знаю» / «Не знаю»."""

    answer: str
    word_id: int
    delivery_id: int

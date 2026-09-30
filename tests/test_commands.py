"""Тесты команд: связка «текст команды → хендлер» и логирование необработанного.

Регрессия: фильтр ``Command("/start")`` (со слэшем — так команды хранятся в
``locales.ru``) не совпадает ни с одной командой Telegram: aiogram сравнивает с
названием команды **без** префикса. Из-за этого бот молча игнорировал ``/start``
и остальные команды, а в ``bot.log`` не появлялось ни строчки.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest
from aiogram import Dispatcher, Router
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TgUser
from loguru import logger

import locales.ru as texts
import main
from handlers import common as common_handlers
from handlers import settings as settings_handlers
from handlers import start as start_handlers
from handlers import stats as stats_handlers
from handlers import word_actions
from logging_setup import InterceptHandler

#: Роутеры в том же порядке, в каком их подключает ``main.register_routers``.
ROUTERS: tuple[Router, ...] = (
    common_handlers.router,
    start_handlers.router,
    settings_handlers.router,
    word_actions.router,
    stats_handlers.router,
)

#: Команда → единственный хендлер, который её обрабатывает.
COMMAND_HANDLERS: dict[str, str] = {
    texts.CMD_START: "cmd_start",
    texts.CMD_SETTINGS: "cmd_settings",
    texts.CMD_WORD: "cmd_word",
    texts.CMD_STATS: "cmd_stats",
    texts.CMD_HELP: "cmd_help",
    texts.CMD_CANCEL: "cmd_cancel",
    texts.CMD_PAUSE: "cmd_pause",
    texts.CMD_RESUME: "cmd_resume",
}


class FakeBot:
    """Заглушка ``Bot``: фильтрам команд достаточно ``me()`` (без сети)."""

    username = "tele_eng_test_bot"

    async def me(self) -> TgUser:
        """Пользователь бота — нужен фильтру для команд вида ``/start@bot``."""
        return TgUser(id=1, is_bot=True, first_name="Test Bot", username=self.username)


def make_message(text: str, *, user_id: int = 1001) -> Message:
    """Сообщение с текстом команды — так его видит хендлер."""
    return Message(
        message_id=1,
        date=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Tester"),
        text=text,
    )


async def matched_handlers(message: Message) -> set[str]:
    """Имена хендлеров роутеров, чьи фильтры совпали с сообщением."""
    names: set[str] = set()
    for router in ROUTERS:
        for handler in router.message.handlers:
            is_match, _ = await handler.check(message, bot=FakeBot())
            if is_match:
                names.add(handler.callback.__name__)
    return names


@pytest.mark.parametrize(("command", "handler_name"), sorted(COMMAND_HANDLERS.items()))
async def test_command_hits_exactly_one_handler(command: str, handler_name: str) -> None:
    """Каждая команда из меню Telegram доходит ровно до одного хендлера."""
    assert await matched_handlers(make_message(command)) == {handler_name}


async def test_command_with_mention_hits_handler() -> None:
    """``/start@bot`` (команда, отправленная в группе) тоже доходит до хендлера."""
    message = make_message(f"{texts.CMD_START}@{FakeBot.username}")

    assert await matched_handlers(message) == {"cmd_start"}


async def test_command_arguments_hits_handler() -> None:
    """Аргументы после команды (``/word 5``) фильтр не ломают."""
    assert await matched_handlers(make_message(f"{texts.CMD_WORD} 5")) == {"cmd_word"}


async def test_unknown_command_matches_no_handler() -> None:
    """Незнакомая команда не должна попадать в хендлеры других команд."""
    assert await matched_handlers(make_message("/nonsense")) == set()


def test_fallback_router_is_registered_last() -> None:
    """Страховка «сообщение без обработчика» подключена последней.

    Иначе она перехватывала бы команды и кнопки меню, до которых не дошло бы
    управление (подробнее — порядок в ``main.register_routers``).
    """
    dispatcher = Dispatcher()

    main.register_routers(dispatcher)

    assert dispatcher.sub_routers[-1] is common_handlers.fallback_router
    assert dispatcher.sub_routers[-2] is stats_handlers.router


def test_fallback_handler_has_no_filters() -> None:
    """У страховочных хендлеров нет фильтров — они ловят всё, что дошло до них."""
    handlers = common_handlers.fallback_router.message.handlers
    callbacks = common_handlers.fallback_router.callback_query.handlers

    assert [handler.callback.__name__ for handler in handlers] == ["on_unhandled_message"]
    assert handlers[0].filters == []
    assert [handler.callback.__name__ for handler in callbacks] == ["on_unhandled_callback"]
    assert callbacks[0].filters == []


class CallbackFakeBot(FakeBot):
    """Заглушка ``Bot`` для ``callback.answer()``: собирает методы API."""

    def __init__(self) -> None:
        self.methods: list[Any] = []

    async def __call__(self, method: Any, *args: Any, **kwargs: Any) -> bool:
        """Сюда попадает ``await callback.answer()``."""
        self.methods.append(method)
        return True


def make_callback(data: str, bot: CallbackFakeBot) -> CallbackQuery:
    """Callback-query так, как его видит хендлер (с привязанным ботом)."""
    message = Message(
        message_id=3,
        date=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
        chat=Chat(id=1001, type="private"),
        text="настройки",
    ).as_(bot)
    callback = CallbackQuery(
        id="callback-1",
        from_user=TgUser(id=1001, is_bot=False, first_name="Tester"),
        chat_instance="chat-instance",
        message=message,
        data=data,
    )
    return callback.as_(bot)


async def test_unhandled_callback_is_logged_and_answered() -> None:
    """Кнопка без хендлера: строка в логе и погашенный индикатор на кнопке.

    Регрессия: такое нажатие было невидимым, поэтому «мёртвая» кнопка
    «✍️ Ввести вручную» выглядела как зависший бот.
    """
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    bot = CallbackFakeBot()
    try:
        await common_handlers.on_unhandled_callback(make_callback("settings:manual:", bot))
    finally:
        logger.remove(sink_id)

    assert "settings:manual:" in "".join(messages)
    assert bot.methods


def test_stdlib_logs_are_forwarded_to_loguru() -> None:
    """Логи aiogram (стандартный ``logging``) попадают в loguru, а не теряются."""
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="ERROR", format="{message}")
    aiogram_logger = logging.getLogger("aiogram.dispatcher")
    previous_handlers, previous_propagate = aiogram_logger.handlers, aiogram_logger.propagate
    aiogram_logger.handlers = [InterceptHandler()]
    aiogram_logger.propagate = False
    try:
        aiogram_logger.error("Failed to fetch updates - TelegramConflictError")
    finally:
        aiogram_logger.handlers = previous_handlers
        aiogram_logger.propagate = previous_propagate
        logger.remove(sink_id)

    assert "Failed to fetch updates - TelegramConflictError" in "".join(messages)

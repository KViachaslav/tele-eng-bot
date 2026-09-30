"""Точка входа бота: логирование, база, хендлеры и планировщик.

Запуск из корня проекта::

    python main.py

Порядок работы: читаем настройки из ``.env`` → готовим базу и при необходимости
импортируем словарь → подключаем middleware и роутеры → восстанавливаем
расписания активных пользователей → уходим в long polling.
"""
from __future__ import annotations

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from loguru import logger

import config
import locales.ru as texts
from database import DatabaseSessionMiddleware, dispose_database, init_database
from handlers import common, settings, start, stats, word_actions
from handlers.common import register_error_handlers
from logging_setup import setup_logging
from services import csv_loader
from services.scheduler import SchedulerService


async def prepare_storage(settings_obj: config.Settings) -> None:
    """Готовит базу данных и словарь: схема плюс автоимпорт CSV при пустой таблице."""
    await init_database(settings_obj.database_path)
    await csv_loader.ensure_words_imported(settings_obj.dataset_path)


def register_routers(dispatcher: Dispatcher) -> None:
    """Подключает роутеры в порядке приоритета обработки обновлений."""
    dispatcher.include_router(common.router)
    dispatcher.include_router(start.router)
    dispatcher.include_router(settings.router)
    dispatcher.include_router(word_actions.router)
    dispatcher.include_router(stats.router)
    # Страховка (лог «сообщение без обработчика») — строго после остальных,
    # иначе она перехватит команды и кнопки меню.
    dispatcher.include_router(common.fallback_router)


async def setup_commands(bot: Bot) -> None:
    """Публикует список команд в меню Telegram."""
    commands = [
        BotCommand(command=name.lstrip("/"), description=description)
        for name, description in texts.COMMAND_DESCRIPTIONS.items()
    ]
    await bot.set_my_commands(commands)


async def run_bot() -> None:
    """Полный жизненный цикл бота: от чтения настроек до остановки polling."""
    settings_obj = config.get_settings()
    setup_logging(settings_obj)

    token = settings_obj.bot_token.strip()
    if not token:
        logger.error(texts.BOT_TOKEN_MISSING)
        raise SystemExit(1)

    await prepare_storage(settings_obj)

    # Карточки слов приходят в MarkdownV2 и передают parse_mode явно
    # (см. services/message_builder и services/delivery), поэтому parse_mode по
    # умолчанию не задаём: экраны настроек и статистики — обычный текст.
    bot = Bot(token)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.update.outer_middleware(DatabaseSessionMiddleware())
    register_routers(dispatcher)
    register_error_handlers(dispatcher)

    scheduler = SchedulerService(bot)
    try:
        await setup_commands(bot)
        scheduler.start()
        await scheduler.restore_all()
        logger.info("Бот запущен, начинаю опрос Telegram")
        await dispatcher.start_polling(
            bot,
            scheduler=scheduler,
            allowed_updates=dispatcher.resolve_used_update_types(),
        )
    finally:
        await scheduler.shutdown()
        await bot.session.close()
        await dispose_database()
        logger.info("Бот остановлен")


def main() -> None:
    """Синхронная точка входа: запускает asyncio-цикл."""
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Остановка по сигналу с клавиатуры")


if __name__ == "__main__":
    main()

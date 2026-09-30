"""Настройка логирования приложения (loguru).

Консоль всегда пишет в ``stderr``; файл подключается, если в настройках задан
``log_file`` (переменная ``LOG_FILE`` в ``.env``). Записи стандартного модуля
``logging`` (aiogram, SQLAlchemy, APScheduler) пересылаются в loguru — иначе
ошибки внутри библиотек остаются невидимыми и бот «молчит» без следов в логах.
"""
from __future__ import annotations

import logging
import sys

from loguru import logger

import config

#: Библиотеки, которые пишут через стандартный ``logging``, и их уровень по
#: умолчанию. SQLAlchemy на INFO печатает каждый SQL-запрос и разбор маппингов
#: (сотни строк), поэтому его логи видны только в DEBUG-режиме.
STDLIB_LOGGERS: dict[str, int] = {
    "aiogram": logging.INFO,
    "sqlalchemy": logging.WARNING,
    "apscheduler": logging.WARNING,
}


#: Файлы, которые не считаются «местом вызова»: сам этот модуль и внутренности
#: ``logging`` — из них записи приходят в ``InterceptHandler``.
_SKIPPED_FRAMES: frozenset[str] = frozenset({logging.__file__, __file__})


class InterceptHandler(logging.Handler):
    """Пересылает записи стандартного ``logging`` в loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # ``logging.currentframe()`` — это кадр самого ``emit`` (индекс 0), поэтому
        # отсчёт начинается с нуля: поднимаемся мимо logging и попадаем в место
        # вызова записи (иначе в логе будет ``logging:1762`` или ``None:0``).
        frame, depth = logging.currentframe(), 0
        while frame is not None and frame.f_code.co_filename in _SKIPPED_FRAMES:
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _intercept_stdlib(level: str) -> None:
    """Направляет логи ``logging`` в loguru; подробность — по ``LOG_LEVEL``."""
    handler = InterceptHandler()
    logging.basicConfig(handlers=[handler], level=0, force=True)
    for name, default_level in STDLIB_LOGGERS.items():
        library_logger = logging.getLogger(name)
        library_logger.handlers = [handler]
        library_logger.propagate = False
        # В DEBUG-режиме библиотеки пишут подробно (SQL-запросы, планировщик).
        library_logger.setLevel(logging.DEBUG if level == "DEBUG" else default_level)


def setup_logging(settings: config.Settings) -> None:
    """Перенастраивает loguru: консоль + (опционально) файл с ротацией."""
    logger.remove()
    level = (settings.log_level or config.DEFAULT_LOG_LEVEL).upper()
    logger.add(
        sys.stderr,
        level=level,
        format=config.LOG_FORMAT,
        colorize=True,
        backtrace=True,
        diagnose=False,
    )

    # Логи библиотек (aiogram, SQLAlchemy) — тоже в loguru.
    _intercept_stdlib(level)

    path = settings.log_file_path
    if path is None:
        logger.debug("Файл логов отключён — пишу только в консоль")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        path,
        level=level,
        format=config.LOG_FORMAT,
        rotation=config.LOG_ROTATION,
        retention=config.LOG_RETENTION,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
    logger.debug("Логи пишутся в {}", path)

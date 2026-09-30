"""Подключение к SQLite: async-движок SQLAlchemy, фабрика сессий, middleware.

Обоснование выбора SQLAlchemy 2.0 async + aiosqlite: декларативные модели делают
схему из раздела 8 ТЗ самодокументируемой, а ``AsyncSession`` даёт единый способ
работать с БД из хендлеров, планировщика и скриптов без ручного дублирования SQL.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from db.migrations import ensure_columns
from db.models import Base

SQLITE_DRIVER = "sqlite+aiosqlite"
SESSION_DATA_KEY = "session"

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_database_url(db_path: Path) -> str:
    """Формирует URL подключения к файлу SQLite."""
    return f"{SQLITE_DRIVER}:///{db_path.as_posix()}"


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """Включает внешние ключи и WAL-журнал для каждого нового соединения."""

    @event.listens_for(engine.sync_engine, "connect")
    def _on_connect(dbapi_connection: Any, _connection_record: Any) -> None:  # noqa: ANN401
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()


async def init_database(db_path: Path) -> AsyncEngine:
    """Создаёт движок, фабрику сессий и все таблицы. Возвращает движок."""
    global _engine, _session_factory

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(build_database_url(db_path), future=True)
    _apply_sqlite_pragmas(engine)

    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        added_columns = await ensure_columns(connection)

    if added_columns:
        logger.info("Схема БД обновлена, добавлены колонки: {}", ", ".join(added_columns))
    logger.info("База данных готова: {}", db_path)
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Возвращает фабрику сессий (должна быть инициализирована заранее)."""
    if _session_factory is None:
        raise RuntimeError("База данных не инициализирована: вызови init_database()")
    return _session_factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Открывает сессию с автоматическим commit/rollback."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def dispose_database() -> None:
    """Закрывает движок и освобождает соединения."""
    global _engine, _session_factory

    if _engine is not None:
        await _engine.dispose()
        logger.info("Соединения с БД закрыты")
    _engine = None
    _session_factory = None


class DatabaseSessionMiddleware(BaseMiddleware):
    """Кладёт :class:`AsyncSession` в контекст хендлеров (``data['session']``)."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with session_scope() as session:
            data[SESSION_DATA_KEY] = session
            return await handler(event, data)

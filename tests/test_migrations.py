"""Тесты мини-миграций схемы: новая колонка дописывается в старую базу.

Регрессия: ``Base.metadata.create_all`` не меняет существующие таблицы, поэтому
рабочий ``bot.db`` со старой схемой падал бы на ``no such column:
users.learning_limit`` — колонку добавляет :mod:`db.migrations`.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import config
import database
from db import migrations, repository


#: Таблица ``users`` в том виде, в каком она была до появления ``learning_limit``
#: (те же колонки, что в ``db/models.py``, но без новой).
OLD_USERS_DDL = (
    "CREATE TABLE users ("
    "id INTEGER PRIMARY KEY, "
    "telegram_id BIGINT NOT NULL UNIQUE, "
    "timezone VARCHAR(64) NOT NULL, "
    "words_per_day INTEGER NOT NULL, "
    "window_start VARCHAR(5) NOT NULL, "
    "window_end VARCHAR(5) NOT NULL, "
    "paused BOOLEAN NOT NULL, "
    "reveal_mode VARCHAR(16) NOT NULL, "
    "pos_filter VARCHAR(128) NOT NULL, "
    "created_at DATETIME NOT NULL)"
)

OLD_USER_INSERT = (
    "INSERT INTO users (telegram_id, timezone, words_per_day, window_start, "
    "window_end, paused, reveal_mode, pos_filter, created_at) "
    "VALUES (777, 'Europe/Moscow', 10, '09:00', '21:00', 0, 'on_button', 'all', "
    "'2026-01-01 00:00:00')"
)


def _engine(path: Path):
    """Async-движок к временному файлу SQLite."""
    return create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")


async def test_ensure_columns_adds_learning_limit(tmp_path: Path) -> None:
    """Старые ``users`` получают колонку лимита со значением по умолчанию."""
    engine = _engine(tmp_path / "old.db")
    try:
        async with engine.begin() as connection:
            await connection.execute(text(OLD_USERS_DDL))
            await connection.execute(text(OLD_USER_INSERT))

        async with engine.begin() as connection:
            assert await migrations.ensure_columns(connection) == ["users.learning_limit"]

        async with engine.begin() as connection:
            # Повторный запуск ничего не меняет: схема уже актуальна.
            assert await migrations.ensure_columns(connection) == []
            limit = (
                await connection.execute(text("SELECT learning_limit FROM users"))
            ).scalar_one()
            assert limit == config.LEARNING_LIMIT_DEFAULT
    finally:
        await engine.dispose()


async def test_ensure_columns_skips_existing_column(tmp_path: Path) -> None:
    """Схема, где колонка уже есть, не меняется."""
    engine = _engine(tmp_path / "new.db")
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE users ("
                    "id INTEGER PRIMARY KEY, "
                    "learning_limit INTEGER NOT NULL DEFAULT 0)"
                )
            )
            assert await migrations.ensure_columns(connection) == []
    finally:
        await engine.dispose()


async def test_init_database_upgrades_old_schema(tmp_path: Path) -> None:
    """``init_database`` сам обновляет базу со старой схемой."""
    path = tmp_path / "bot.db"
    engine = _engine(path)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(OLD_USERS_DDL))
    finally:
        await engine.dispose()

    await database.init_database(path)
    try:
        async with database.session_scope() as session:
            user = await repository.create_user(session, telegram_id=1001)
            assert user.learning_limit == config.LEARNING_LIMIT_DEFAULT
    finally:
        await database.dispose_database()

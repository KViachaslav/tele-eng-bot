"""Мини-миграции схемы для уже существующих баз SQLite.

``Base.metadata.create_all`` создаёт только отсутствующие таблицы: новые колонки
в существующей таблице он не добавляет. Поэтому поля, появившиеся позже первой
версии схемы (``users.learning_limit``), дописываются здесь — иначе старая
``bot.db`` падала бы при первом же запросе с ``no such column``.
"""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

import config

#: Колонки, добавленные после первой версии схемы: (таблица, колонка, DDL).
ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (
        "users",
        "learning_limit",
        "ALTER TABLE users ADD COLUMN learning_limit INTEGER NOT NULL "
        f"DEFAULT {config.LEARNING_LIMIT_DEFAULT}",
    ),
)


async def ensure_columns(connection: AsyncConnection) -> list[str]:
    """Добавляет отсутствующие колонки из :data:`ADDED_COLUMNS`.

    :return: список добавленных колонок в виде ``users.learning_limit`` (пустой,
        если схема уже актуальна).
    """
    added: list[str] = []
    for table, column, ddl in ADDED_COLUMNS:
        result = await connection.execute(text(f"PRAGMA table_info({table})"))
        if column in {row[1] for row in result}:
            continue
        await connection.execute(text(ddl))
        added.append(f"{table}.{column}")
    return added

"""Общие фикстуры тестов: временная база SQLite, пользователь и слова.

Каждый тест получает чистую базу в ``tmp_path``, поэтому тесты не зависят от
рабочего ``bot.db`` и не влияют друг на друга.
"""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import database
from db import repository
from db.models import User, Word

#: Словарные статьи для тестов: noun, adverb, adjective.
WORD_ROWS: tuple[dict[str, str | None], ...] = (
    {
        "word": "apple",
        "definition": "a round fruit with red or green skin",
        "russian_translation": "яблоко",
        "example": "She ate an apple for breakfast.",
        "part_of_speech": "noun",
        "related_forms": "apples",
        "synonyms": "fruit",
        "antonyms": None,
        "collocations": "green apple",
    },
    {
        "word": "quickly",
        "definition": "at a fast speed",
        "russian_translation": "быстро",
        "example": "He quickly left the room.",
        "part_of_speech": "adverb",
        "related_forms": "quick, quicker",
        "synonyms": "fast",
        "antonyms": "slowly",
        "collocations": None,
    },
    {
        "word": "curious",
        "definition": "wanting to know something",
        "russian_translation": "любопытный",
        "example": "The child was curious about everything.",
        "part_of_speech": "adjective",
        "related_forms": None,
        "synonyms": "inquisitive",
        "antonyms": None,
        "collocations": None,
    },
)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[None]:
    """Пустая база со схемой из раздела 8 ТЗ (файл в ``tmp_path``)."""
    await database.init_database(tmp_path / "test.db")
    try:
        yield
    finally:
        await database.dispose_database()


@pytest.fixture
async def session(db: None) -> AsyncIterator[AsyncSession]:
    """Сессия с авто-commit (аналог хендлерной сессии из middleware)."""
    async with database.session_scope() as session:
        yield session


@pytest.fixture
async def user(session: AsyncSession) -> User:
    """Пользователь с настройками по умолчанию (10 слов, 09:00–21:00)."""
    return await repository.create_user(session, telegram_id=1001)


@pytest.fixture
async def words(session: AsyncSession) -> Sequence[Word]:
    """Три словарные статьи, загруженные в базу."""
    await repository.upsert_words(session, WORD_ROWS)
    result = await session.execute(select(Word).order_by(Word.id))
    return result.scalars().all()


@pytest.fixture
def word_rows() -> tuple[dict[str, str | None], ...]:
    """Те же строки, что и в фикстуре ``words`` — для проверки повторного импорта."""
    return WORD_ROWS


@pytest.fixture
def now() -> datetime:
    """Фиксированный «текущий» момент (наивный UTC — так даты лежат в БД)."""
    return datetime(2026, 1, 15, 9, 0, 0)

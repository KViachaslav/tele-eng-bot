"""ORM-модели SQLAlchemy 2.0 (async) для базы SQLite.

Схема соответствует разделу 8 ТЗ: ``users``, ``user_settings``, ``words``,
``user_words``, ``delivery_log``. Дополнительно у пользователя есть
``users.learning_limit`` — лимит слов, которые одновременно могут быть в
изучении (расширение проекта, раздел 8 ТЗ его не описывает).
"""
from __future__ import annotations

import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

import config


def utcnow() -> datetime:
    """Текущее время в UTC без tzinfo — именно так даты хранятся в SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    """Базовый класс моделей."""


class User(Base):
    """Пользователь бота и его настройки рассылки."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default=config.DEFAULT_TIMEZONE
    )
    words_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=config.WORDS_PER_DAY_DEFAULT
    )
    window_start: Mapped[str] = mapped_column(
        String(5), nullable=False, default=config.WINDOW_START_DEFAULT
    )
    window_end: Mapped[str] = mapped_column(
        String(5), nullable=False, default=config.WINDOW_END_DEFAULT
    )
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reveal_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default=config.REVEAL_MODE_ON_BUTTON
    )
    pos_filter: Mapped[str] = mapped_column(
        String(128), nullable=False, default=config.POS_FILTER_ALL
    )
    learning_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=config.LEARNING_LIMIT_DEFAULT
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    user_settings: Mapped["UserSettings"] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    user_words: Mapped[list["UserWord"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # -- удобные аксессоры -------------------------------------------------
    @property
    def tzinfo(self) -> ZoneInfo:
        """Часовой пояс пользователя."""
        return ZoneInfo(self.timezone)

    @property
    def window_start_time(self) -> time:
        """Время начала окна рассылки."""
        return datetime.strptime(self.window_start, config.TIME_FORMAT).time()

    @property
    def window_end_time(self) -> time:
        """Время окончания окна рассылки."""
        return datetime.strptime(self.window_end, config.TIME_FORMAT).time()

    @property
    def pos_filter_values(self) -> list[str]:
        """Выбранные части речи (пустой список означает «все»)."""
        raw = (self.pos_filter or config.POS_FILTER_ALL).strip()
        if not raw or raw == config.POS_FILTER_ALL:
            return []
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(values, list):
            return []
        return [str(value) for value in values if value in config.POS_FILTER_VALUES]

    @property
    def learning_limit_enabled(self) -> bool:
        """Действует ли лимит слов в изучении (``0`` — без ограничения)."""
        return int(self.learning_limit or 0) > 0

    @property
    def local_now(self) -> datetime:
        """Текущее локальное время пользователя."""
        return datetime.now(timezone.utc).astimezone(self.tzinfo)


class UserSettings(Base):
    """Чекбоксы «что показывать в сообщении» (персонально для пользователя)."""

    __tablename__ = "user_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    show_definition: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_example: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_pos: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    show_related: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    show_synonyms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_antonyms: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_collocations: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    user: Mapped[User] = relationship(back_populates="user_settings")

    def enabled_fields(self) -> list[str]:
        """Названия включённых полей карточки."""
        return [name for name in config.FIELD_NAMES if getattr(self, name)]


class Word(Base):
    """Словарная статья из Oxford 3000."""

    __tablename__ = "words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    word: Mapped[str] = mapped_column(String(128), unique=True, index=True, nullable=False)
    definition: Mapped[str | None] = mapped_column(Text)
    russian_translation: Mapped[str | None] = mapped_column(Text)
    example: Mapped[str | None] = mapped_column(Text)
    part_of_speech: Mapped[str | None] = mapped_column(String(64))
    related_forms: Mapped[str | None] = mapped_column(Text)
    synonyms: Mapped[str | None] = mapped_column(Text)
    antonyms: Mapped[str | None] = mapped_column(Text)
    collocations: Mapped[str | None] = mapped_column(Text)


class UserWord(Base):
    """Прогресс пользователя по конкретному слову (состояние SRS)."""

    __tablename__ = "user_words"
    __table_args__ = (UniqueConstraint("user_id", "word_id", name="uq_user_words_user_word"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    word_id: Mapped[int] = mapped_column(
        ForeignKey("words.id", ondelete="CASCADE"), index=True, nullable=False
    )
    stage: Mapped[int] = mapped_column(Integer, nullable=False, default=config.SRS_FIRST_STAGE)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=config.STATUS_NEW)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    last_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    times_correct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    times_wrong: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_refresh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    user: Mapped[User] = relationship(back_populates="user_words")
    word: Mapped[Word] = relationship(lazy="joined")


class DeliveryLog(Base):
    """Журнал отправок слов и ответов пользователя на них."""

    __tablename__ = "delivery_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    word_id: Mapped[int] = mapped_column(
        ForeignKey("words.id", ondelete="CASCADE"), index=True, nullable=False
    )
    sent_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime)
    answer: Mapped[str | None] = mapped_column(String(16))

    word: Mapped[Word] = relationship(lazy="joined")

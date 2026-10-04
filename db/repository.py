"""Репозиторий: все SQL-запросы к базе собраны в одном модуле.

Хендлеры, планировщик и скрипты работают с БД только через эти функции.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TypeVar

from sqlalchemy import ColumnElement, Subquery, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

import config
from db.models import (
    TRANSLATION_BOTH,
    DeliveryLog,
    User,
    UserSettings,
    UserWord,
    Word,
    translation_field_pairs,
    utcnow,
)

ModelT = TypeVar("ModelT")


@dataclass(slots=True)
class UpsertResult:
    """Результат пакетной вставки слов."""

    imported: int = 0
    skipped: int = 0


@dataclass(slots=True)
class UserStats:
    """Сводная статистика пользователя."""

    total_words: int = 0
    learned: int = 0
    learning: int = 0
    new: int = 0
    correct: int = 0
    wrong: int = 0
    streak: int = 0
    problems: list[UserWord] | None = None

    @property
    def accuracy(self) -> float:
        """Точность ответов в процентах (0, если ответов ещё не было)."""
        answers = self.correct + self.wrong
        return round(self.correct * 100 / answers, 1) if answers else 0.0


@dataclass(slots=True)
class DeliveryLogStats:
    """Размер журнала отправок: число записей и максимальный идентификатор."""

    count: int = 0
    max_id: int = 0


@dataclass(slots=True)
class TranslationPending:
    """Сколько словарю ещё нужно русского текста (:func:`pending_translations`)."""

    words: int = 0
    chars: int = 0


# ---------------------------------------------------------------------------
# Пользователи
# ---------------------------------------------------------------------------
async def get_user_by_telegram_id(session: AsyncSession, telegram_id: int) -> User | None:
    """Находит пользователя по Telegram ID."""
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: int) -> User | None:
    """Находит пользователя по первичному ключу."""
    return await session.get(User, user_id)


async def list_users(session: AsyncSession, *, only_active: bool = False) -> Sequence[User]:
    """Возвращает всех (или только не находящихся на паузе) пользователей."""
    stmt = select(User).order_by(User.id)
    if only_active:
        stmt = stmt.where(User.paused.is_(False))
    result = await session.execute(stmt)
    return result.scalars().all()


async def create_user(
    session: AsyncSession,
    telegram_id: int,
    *,
    timezone: str | None = None,
    words_per_day: int | None = None,
    window_start: str | None = None,
    window_end: str | None = None,
    reveal_mode: str | None = None,
    learning_limit: int | None = None,
) -> User:
    """Создаёт пользователя вместе с настройками карточки."""
    user = User(
        telegram_id=telegram_id,
        timezone=timezone or config.DEFAULT_TIMEZONE,
        words_per_day=words_per_day or config.WORDS_PER_DAY_DEFAULT,
        window_start=window_start or config.WINDOW_START_DEFAULT,
        window_end=window_end or config.WINDOW_END_DEFAULT,
        reveal_mode=reveal_mode or config.REVEAL_MODE_ON_BUTTON,
        paused=False,
        pos_filter=config.POS_FILTER_ALL,
        # лимит сравнивается как есть: 0 — значимое значение («без ограничения»)
        learning_limit=(
            config.LEARNING_LIMIT_DEFAULT if learning_limit is None else learning_limit
        ),
        created_at=utcnow(),
    )
    user.user_settings = UserSettings()
    session.add(user)
    await session.flush()
    return user


async def update_user(session: AsyncSession, user: User, **fields: object) -> User:
    """Обновляет переданные поля пользователя."""
    return await _apply_fields(session, user, fields)


async def update_user_settings(session: AsyncSession, settings: UserSettings, **flags: bool) -> UserSettings:
    """Обновляет чекбоксы полей карточки."""
    return await _apply_fields(session, settings, flags)


async def _apply_fields(
    session: AsyncSession, instance: ModelT, fields: Mapping[str, object]
) -> ModelT:
    """Применяет значения полей к ORM-объекту (общий помощник)."""
    for name, value in fields.items():
        setattr(instance, name, value)
    await session.flush()
    return instance


async def get_or_create_user_settings(session: AsyncSession, user: User) -> UserSettings:
    """Возвращает настройки карточки, создавая их при необходимости."""
    if user.user_settings is None:
        user.user_settings = UserSettings(user_id=user.id)
        session.add(user.user_settings)
        await session.flush()
    return user.user_settings


async def set_pos_filter(session: AsyncSession, user: User, values: Iterable[str]) -> User:
    """Сохраняет фильтр по части речи (пустой список == ``all``)."""
    cleaned = [value for value in values if value in config.POS_FILTER_VALUES]
    user.pos_filter = json.dumps(cleaned, ensure_ascii=False) if cleaned else config.POS_FILTER_ALL
    await session.flush()
    return user


# ---------------------------------------------------------------------------
# Слова
# ---------------------------------------------------------------------------
async def count_words(session: AsyncSession) -> int:
    """Количество словарных статей в базе."""
    result = await session.execute(select(func.count()).select_from(Word))
    return int(result.scalar_one())


async def get_word_by_id(session: AsyncSession, word_id: int) -> Word | None:
    """Словарная статья по идентификатору."""
    return await session.get(Word, word_id)


async def save_word_translations(
    session: AsyncSession,
    word: Word,
    **fields: str,
) -> Word:
    """Сохраняет русские переводы полей слова (``russian_definition``, ``russian_example``).

    Перевод определения и примера приходит по сети (MyMemory, см.
    :mod:`services.translator`) и складывается в саму статью: следующая карточка
    этого слова берёт готовый текст, а не ходит в сервис заново.
    """
    return await _apply_fields(session, word, fields)


def _has_text(column: ColumnElement[str]) -> ColumnElement[bool]:
    """Условие «в колонке есть текст» (не ``NULL`` и не пустая строка)."""
    return and_(column.is_not(None), column != "")


def _blank(column: ColumnElement[str]) -> ColumnElement[bool]:
    """Условие «в колонке нет текста» (``NULL`` или пустая строка)."""
    return or_(column.is_(None), column == "")


def _missing_pair_conditions(field: str) -> list[ColumnElement[bool]]:
    """Условия «английский текст есть, а русского перевода ещё нет» по полям.

    По одному условию на пару из :func:`db.models.translation_field_pairs`:
    например, для ``both`` — определение без ``russian_definition`` или пример
    без ``russian_example``.
    """
    return [
        and_(_has_text(getattr(Word, source)), _blank(getattr(Word, target)))
        for source, target in translation_field_pairs(field)
    ]


async def get_words_without_translations(
    session: AsyncSession,
    *,
    limit: int | None = None,
    field: str = TRANSLATION_BOTH,
    learning_first: bool = False,
) -> Sequence[Word]:
    """Слова, которые ещё ждут русского текста (для пакетного перевода).

    Пара к :func:`services.translator.translate_pending_words` и
    ``scripts/translate_words.py``: у статьи есть английское поле
    (``definition`` / ``example``), а его русская версия пуста — именно такие
    строки бот дозапрашивает у MyMemory при первом показе.

    Порядок — по ``Word.id``, чтобы порция шла предсказуемо и прогоны не
    пересекались. ``learning_first`` поднимает вперёд слова из чьих-то планов
    изучения (``user_words``): их пользователь увидит раньше, значит и русская
    подсказка нужнее.

    :param limit: сколько слов вернуть (``None`` — все).
    :param field: какие поля считать непереведёнными (``both``, ``definition``,
        ``example``).
    :param learning_first: сначала слова, которые уже есть в планах изучения.
    """
    stmt = select(Word).where(or_(*_missing_pair_conditions(field)))
    if learning_first:
        # EXISTS вместо join: у слова может быть много планов, а строка нужна одна.
        in_plans = select(UserWord.id).where(UserWord.word_id == Word.id).exists()
        stmt = stmt.order_by(in_plans.desc(), Word.id)
    else:
        stmt = stmt.order_by(Word.id)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def pending_translations(
    session: AsyncSession, *, field: str = TRANSLATION_BOTH
) -> TranslationPending:
    """Сколько слов ещё ждут русского текста и сколько символов нужно перевести.

    Символы считаются по английским строкам: по этому числу видно, во сколько
    суточных лимитов MyMemory (:data:`config.MYMEMORY_DAILY_CHARS_ANONYMOUS` /
    :data:`config.MYMEMORY_DAILY_CHARS_WITH_EMAIL`) обойдётся остаток словаря.
    """
    pending = TranslationPending()
    conditions = _missing_pair_conditions(field)
    for (source, _target), condition in zip(translation_field_pairs(field), conditions):
        chars = await session.scalar(
            select(func.coalesce(func.sum(func.length(getattr(Word, source))), 0)).where(
                condition
            )
        )
        pending.chars += int(chars or 0)
    pending.words = int(
        await session.scalar(select(func.count()).select_from(Word).where(or_(*conditions))) or 0
    )
    return pending


async def upsert_words(session: AsyncSession, rows: Sequence[Mapping[str, object]]) -> UpsertResult:
    """Вставляет новые слова, пропуская уже существующие (уникальный ``word``).

    Пакетный ``INSERT`` по чанкам + предварительная выборка существующих слов:
    повторный импорт того же CSV не создаёт дубликатов.
    """
    result = UpsertResult()
    if not rows:
        return result

    for start in range(0, len(rows), config.IMPORT_CHUNK_SIZE):
        chunk = rows[start : start + config.IMPORT_CHUNK_SIZE]
        words = [str(row["word"]) for row in chunk]
        existing_result = await session.execute(select(Word.word).where(Word.word.in_(words)))
        existing = set(existing_result.scalars().all())

        fresh: list[dict[str, object]] = []
        seen_in_chunk: set[str] = set()
        for row in chunk:
            word = str(row["word"])
            if word in existing or word in seen_in_chunk:
                result.skipped += 1
                continue
            seen_in_chunk.add(word)
            fresh.append(dict(row))

        if fresh:
            await session.execute(Word.__table__.insert(), fresh)
            result.imported += len(fresh)
        await session.flush()

    return result


def build_pos_filter_condition(
    values: Sequence[str], column: ColumnElement[str] | None = None
) -> ColumnElement[bool] | None:
    """Условие фильтра по части речи; ``None`` — фильтр не нужен.

    ``noun``/``verb``/``adjective``/``adverb`` ищутся подстрокой (в CSV часть
    речи бывает составной, например ``noun, verb``), ``other`` — всё, что не
    попало в стандартные категории.
    """
    if not values:
        return None

    target = column if column is not None else Word.part_of_speech
    normalized = func.lower(func.coalesce(target, ""))

    conditions: list[ColumnElement[bool]] = []
    for value in values:
        if value == config.POS_OTHER:
            conditions.append(
                and_(
                    *[
                        normalized.not_like(f"%{standard}%")
                        for standard in config.POS_STANDARD_VALUES
                    ]
                )
            )
        else:
            conditions.append(normalized.like(f"%{value}%"))
    return or_(*conditions)


def last_sent_subquery(user: User) -> Subquery:
    """Подзапрос «когда это слово последний раз отправляли пользователю».

    Общий для очередей повторений, освежения и новых слов: по нему видно, что уже
    показывали недавно, а что ещё нет.
    """
    return (
        select(
            DeliveryLog.word_id.label("word_id"),
            func.max(DeliveryLog.sent_at).label("last_sent_at"),
        )
        .where(DeliveryLog.user_id == user.id)
        .group_by(DeliveryLog.word_id)
        .subquery()
    )


def first_sent_subquery(user: User) -> Subquery:
    """Подзапрос «когда это слово впервые отправили пользователю».

    Первая отправка — момент, когда слово попало «в изучение» и потратило пункт
    дневного плана (``users.words_per_day``), поэтому по подзапросу видно, сколько
    слов дня уже израсходовано (см. :func:`services.plan.words_left_today`).
    """
    return (
        select(
            DeliveryLog.word_id.label("word_id"),
            func.min(DeliveryLog.sent_at).label("first_sent_at"),
        )
        .where(DeliveryLog.user_id == user.id)
        .group_by(DeliveryLog.word_id)
        .subquery()
    )


async def fetch_due_user_words(
    session: AsyncSession,
    user: User,
    now: datetime,
    limit: int,
    pos_values: Sequence[str] = (),
    exclude_word_ids: Sequence[int] = (),
    not_shown_since: datetime | None = None,
) -> Sequence[UserWord]:
    """Слова в статусе «изучается», у которых наступила дата повторения.

    Самые просроченные идут первыми; при равном сроке вперёд попадают те, что
    дольше ждали показа (``last_reviewed_at``), поэтому слово, на которое только
    что ответили «не знаю» (этап 0), не вытесняет остальные.

    :param exclude_word_ids: слова, которые сейчас показывать не нужно (например,
        уже отправленные карточки без ответа при ручном запросе ``/word``).
    :param not_shown_since: если задано, в выборку попадают только слова, которых
        пользователю не показывали с этого момента (см.
        :func:`services.slots.local_day_start_utc`). Так очередь сначала отдаёт
        повторения, которых сегодня ещё не было, и только потом новое слово.
    """
    stmt = (
        select(UserWord)
        .join(Word, Word.id == UserWord.word_id)
        .where(
            UserWord.user_id == user.id,
            UserWord.status == config.STATUS_LEARNING,
            UserWord.is_refresh.is_(False),
            UserWord.next_review_at.is_not(None),
            UserWord.next_review_at <= now,
        )
        .order_by(
            UserWord.next_review_at.asc(),
            UserWord.last_reviewed_at.asc(),
            UserWord.id.asc(),
        )
        .limit(limit)
        .options(joinedload(UserWord.word))
    )
    if not_shown_since is not None:
        last_sent = last_sent_subquery(user)
        stmt = stmt.outerjoin(last_sent, last_sent.c.word_id == UserWord.word_id).where(
            or_(
                last_sent.c.last_sent_at.is_(None),
                last_sent.c.last_sent_at < not_shown_since,
            )
        )
    if exclude_word_ids:
        stmt = stmt.where(UserWord.word_id.not_in(exclude_word_ids))
    condition = build_pos_filter_condition(pos_values)
    if condition is not None:
        stmt = stmt.where(condition)
    result = await session.execute(stmt)
    return result.scalars().all()


async def fetch_new_words(
    session: AsyncSession,
    user: User,
    limit: int,
    pos_values: Sequence[str] = (),
    exclude_word_ids: Sequence[int] = (),
) -> Sequence[Word]:
    """Слова, которые пользователь ещё ни разу не оценивал.

    Порядок: сначала **случайное** слово из тех, что ещё ни разу не приходили,
    затем — случайное из отправленных ранее. Случайный выбор нужен, чтобы новые
    слова не шли подряд по порядку загрузки словаря; проигнорированное слово
    повторится только после того, как опустеет очередь «совсем новых».

    :param exclude_word_ids: слова, которые сейчас показывать не нужно (см.
        :func:`fetch_due_user_words`).
    """
    last_sent = last_sent_subquery(user)

    stmt = (
        select(Word)
        .outerjoin(UserWord, and_(UserWord.word_id == Word.id, UserWord.user_id == user.id))
        .outerjoin(last_sent, last_sent.c.word_id == Word.id)
        .where(UserWord.id.is_(None))
        # Порядок: слова, которые ещё не отправляли (last_sent_at IS NULL), идут
        # первыми, но внутри каждой группы — случайный выбор (random()), иначе
        # новые слова шли бы подряд по порядку загрузки словаря.
        .order_by(last_sent.c.last_sent_at.is_(None).desc(), func.random())
        .limit(limit)
    )
    if exclude_word_ids:
        stmt = stmt.where(Word.id.not_in(exclude_word_ids))
    condition = build_pos_filter_condition(pos_values)
    if condition is not None:
        stmt = stmt.where(condition)
    result = await session.execute(stmt)
    return result.scalars().all()


async def fetch_refresh_user_words(
    session: AsyncSession,
    user: User,
    now: datetime,
    limit: int,
    pos_values: Sequence[str] = (),
    exclude_word_ids: Sequence[int] = (),
) -> Sequence[UserWord]:
    """Выученные слова, у которых подошёл срок освежения (этап 6, 90 дней).

    :param exclude_word_ids: слова, которые сейчас показывать не нужно (см.
        :func:`fetch_due_user_words`).
    """
    last_sent = last_sent_subquery(user)

    stmt = (
        select(UserWord)
        .join(Word, Word.id == UserWord.word_id)
        .outerjoin(last_sent, last_sent.c.word_id == UserWord.word_id)
        .where(
            UserWord.user_id == user.id,
            UserWord.status == config.STATUS_LEARNED,
            UserWord.next_review_at.is_not(None),
            UserWord.next_review_at <= now,
        )
        .order_by(
            last_sent.c.last_sent_at.asc().nulls_first(),
            UserWord.next_review_at.asc(),
            UserWord.id.asc(),
        )
        .limit(limit)
        .options(joinedload(UserWord.word))
    )
    if exclude_word_ids:
        stmt = stmt.where(UserWord.word_id.not_in(exclude_word_ids))
    condition = build_pos_filter_condition(pos_values)
    if condition is not None:
        stmt = stmt.where(condition)
    result = await session.execute(stmt)
    return result.scalars().all()


async def fetch_shown_word_ids_since(
    session: AsyncSession, user: User, since: datetime | None = None
) -> Sequence[int]:
    """Слова, карточки которых уже уходили пользователю не раньше ``since``.

    Ответ роли не играет: и карточка без ответа, и слово, на которое ответили
    «не знаю» (этап 0 — срок повторения наступает сразу же), уже были в чате
    сегодня. Показывать такое слово снова подряд нельзя, иначе ручной запрос
    ``/word`` ходит по кругу одних и тех же слов (см.
    :func:`services.word_selector.on_demand_exclusions`).

    :param since: если задано, учитываются только отправки не раньше этого
        момента. Карточка, оставшаяся без ответа накануне, из очереди не
        выпадает: она уже не «висит» перед глазами пользователя.
    """
    stmt = (
        select(DeliveryLog.word_id)
        .where(DeliveryLog.user_id == user.id)
        .distinct()
    )
    if since is not None:
        stmt = stmt.where(DeliveryLog.sent_at >= since)
    result = await session.execute(stmt)
    return result.scalars().all()


async def fetch_last_delivered_word_id(session: AsyncSession, user: User) -> int | None:
    """Слово из самой последней отправки пользователю (даже если на неё ответили)."""
    result = await session.execute(
        select(DeliveryLog.word_id)
        .where(DeliveryLog.user_id == user.id)
        .order_by(DeliveryLog.sent_at.desc(), DeliveryLog.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Прогресс пользователя по словам
# ---------------------------------------------------------------------------
async def get_user_word(session: AsyncSession, user_id: int, word_id: int) -> UserWord | None:
    """Состояние SRS для пары (пользователь, слово)."""
    result = await session.execute(
        select(UserWord)
        .where(UserWord.user_id == user_id, UserWord.word_id == word_id)
        .options(joinedload(UserWord.word))
    )
    return result.scalar_one_or_none()


async def count_learning_user_words(session: AsyncSession, user: User) -> int:
    """Сколько слов у пользователя «в изучении» (этапы 0–5).

    Это основа лимита ``users.learning_limit``: пока таких слов не меньше
    лимита, новые слова не показываются. Выученные слова (``learned``) и слова
    на освежении в подсчёт не входят — они лимит не занимают.
    """
    result = await session.execute(
        select(func.count())
        .select_from(UserWord)
        .where(
            UserWord.user_id == user.id,
            UserWord.status == config.STATUS_LEARNING,
        )
    )
    return int(result.scalar_one())


async def count_started_words_since(session: AsyncSession, user: User, since: datetime) -> int:
    """Сколько слов пользователь начал изучать не раньше ``since``.

    «Начал» — первая отправка слова (:func:`first_sent_subquery`): именно она
    расходует пункт дневного плана ``users.words_per_day``, независимо от того,
    пришло слово по расписанию или по запросу «дай слово».
    """
    first_sent = first_sent_subquery(user)
    result = await session.execute(
        select(func.count())
        .select_from(first_sent)
        .where(first_sent.c.first_sent_at >= since)
    )
    return int(result.scalar_one())


async def count_pending_review_user_words(
    session: AsyncSession,
    user: User,
    until: datetime,
    *,
    not_shown_since: datetime | None = None,
    pos_values: Sequence[str] = (),
) -> int:
    """Сколько повторений ждут показа к моменту ``until``.

    Считаются слова «в изучении» (этапы 0–5), срок повторения которых наступает
    не позже ``until``: для плана дня это конец местных суток пользователя. Так в
    план попадают и просроченные повторения, к которым ещё не приступали, и слова,
    сброшенные на этап 0 ответом «не знаю». Выученные слова и освежение не
    считаются: они приходят отдельным сообщением в том же слоте, места не занимая.

    :param not_shown_since: если задано, уже показанные с этого момента слова не
        считаются — кроме тех, на которые с тех пор ответили: «не знаю» возвращает
        слово на этап 0, и оно должно прийти снова (см.
        :func:`fetch_due_user_words`). Карточка, отправленная после последнего
        ответа, план не занимает: слово уже в чате у пользователя, заново его
        планировать не нужно. Поэтому каждая отправка (в том числе кнопкой
        «🎲 Слово») уменьшает план дня, а интервал до следующего слова растёт
        (см. :mod:`services.plan`).
    """
    stmt = (
        select(func.count())
        .select_from(UserWord)
        .join(Word, Word.id == UserWord.word_id)
        .where(
            UserWord.user_id == user.id,
            UserWord.status == config.STATUS_LEARNING,
            UserWord.is_refresh.is_(False),
            UserWord.next_review_at.is_not(None),
            UserWord.next_review_at <= until,
        )
    )
    if not_shown_since is not None:
        last_sent = last_sent_subquery(user)
        stmt = stmt.outerjoin(last_sent, last_sent.c.word_id == UserWord.word_id).where(
            or_(
                last_sent.c.last_sent_at.is_(None),
                last_sent.c.last_sent_at < not_shown_since,
                and_(
                    UserWord.last_reviewed_at.is_not(None),
                    UserWord.last_reviewed_at >= not_shown_since,
                    UserWord.last_reviewed_at >= last_sent.c.last_sent_at,
                ),
            )
        )
    condition = build_pos_filter_condition(pos_values)
    if condition is not None:
        stmt = stmt.where(condition)
    result = await session.execute(stmt)
    return int(result.scalar_one())


async def get_or_create_user_word(session: AsyncSession, user_id: int, word_id: int) -> UserWord:
    """Возвращает состояние SRS, создавая запись «новое слово» при необходимости."""
    user_word = await get_user_word(session, user_id, word_id)
    if user_word is None:
        user_word = UserWord(
            user_id=user_id,
            word_id=word_id,
            stage=config.SRS_FIRST_STAGE,
            status=config.STATUS_NEW,
            times_correct=0,
            times_wrong=0,
            is_refresh=False,
        )
        session.add(user_word)
        await session.flush()
    return user_word


async def save_user_word(
    session: AsyncSession,
    user_word: UserWord,
    *,
    stage: int,
    status: str,
    next_review_at: datetime | None,
    last_reviewed_at: datetime,
    times_correct: int,
    times_wrong: int,
    is_refresh: bool,
) -> UserWord:
    """Записывает новое состояние SRS после ответа пользователя."""
    user_word.stage = stage
    user_word.status = status
    user_word.next_review_at = next_review_at
    user_word.last_reviewed_at = last_reviewed_at
    user_word.times_correct = times_correct
    user_word.times_wrong = times_wrong
    user_word.is_refresh = is_refresh
    await session.flush()
    return user_word


async def mark_user_word_for_refresh(session: AsyncSession, user_word: UserWord) -> UserWord:
    """Помечает выученное слово как отправленное на освежение."""
    user_word.is_refresh = True
    await session.flush()
    return user_word


# ---------------------------------------------------------------------------
# Журнал отправок
# ---------------------------------------------------------------------------
async def create_delivery(
    session: AsyncSession,
    user_id: int,
    word_id: int,
    *,
    sent_at: datetime | None = None,
) -> DeliveryLog:
    """Фиксирует факт отправки слова пользователю."""
    delivery = DeliveryLog(user_id=user_id, word_id=word_id, sent_at=sent_at or utcnow())
    session.add(delivery)
    await session.flush()
    return delivery


async def get_delivery(session: AsyncSession, delivery_id: int) -> DeliveryLog | None:
    """Отправка по идентификатору (вместе со словом)."""
    result = await session.execute(
        select(DeliveryLog)
        .where(DeliveryLog.id == delivery_id)
        .options(joinedload(DeliveryLog.word))
    )
    return result.scalar_one_or_none()


async def get_delivery_log_stats(session: AsyncSession) -> DeliveryLogStats:
    """Размер журнала отправок — для диагностики «кнопка без отправки».

    Если в кнопке ``delivery_id`` больше :attr:`DeliveryLogStats.max_id`, бот
    работает не на той базе, из которой отправлялась карточка (свежая база,
    потерянный журнал WAL или второй экземпляр бота со своей базой).
    """
    result = await session.execute(select(func.count(), func.max(DeliveryLog.id)))
    count, max_id = result.one()
    return DeliveryLogStats(count=count or 0, max_id=max_id or 0)


async def mark_delivery_answered(
    session: AsyncSession,
    delivery: DeliveryLog,
    answer: str,
    *,
    answered_at: datetime | None = None,
) -> DeliveryLog:
    """Записывает ответ пользователя на отправленное слово."""
    delivery.answer = answer
    delivery.answered_at = answered_at or utcnow()
    await session.flush()
    return delivery


# ---------------------------------------------------------------------------
# Статистика
# ---------------------------------------------------------------------------
async def collect_user_stats(
    session: AsyncSession,
    user: User,
    *,
    now: datetime | None = None,
    problem_limit: int = config.TOP_PROBLEM_WORDS_LIMIT,
) -> UserStats:
    """Собирает статистику: статусы, точность ответов, streak, топ проблемных слов."""
    moment = now or utcnow()
    stats = UserStats(problems=[])

    stats.total_words = await count_words(session)

    status_result = await session.execute(
        select(UserWord.status, func.count())
        .where(UserWord.user_id == user.id)
        .group_by(UserWord.status)
    )
    for status, amount in status_result.all():
        if status == config.STATUS_LEARNED:
            stats.learned = int(amount)
        elif status == config.STATUS_LEARNING:
            stats.learning = int(amount)
        elif status == config.STATUS_NEW:
            stats.new = int(amount)

    answer_result = await session.execute(
        select(DeliveryLog.answer, func.count())
        .where(DeliveryLog.user_id == user.id, DeliveryLog.answered_at.is_not(None))
        .group_by(DeliveryLog.answer)
    )
    for answer, amount in answer_result.all():
        if answer == config.ANSWER_KNOW:
            stats.correct = int(amount)
        elif answer == config.ANSWER_DONT_KNOW:
            stats.wrong = int(amount)

    problems_result = await session.execute(
        select(UserWord)
        .join(Word, Word.id == UserWord.word_id)
        .where(UserWord.user_id == user.id, UserWord.times_wrong > 0)
        .order_by(UserWord.times_wrong.desc(), UserWord.times_correct.asc(), UserWord.id.asc())
        .limit(problem_limit)
        .options(joinedload(UserWord.word))
    )
    stats.problems = list(problems_result.scalars().all())

    stats.streak = await calculate_streak(session, user, moment)
    return stats


async def calculate_streak(session: AsyncSession, user: User, now: datetime) -> int:
    """Сколько дней подряд (по местному времени) пользователь отвечал на слова.

    Серия считается «живой», если ответы были сегодня или вчера.
    """
    since = now - timedelta(days=config.STREAK_MAX_LOOKBACK_DAYS)
    result = await session.execute(
        select(DeliveryLog.answered_at)
        .where(
            DeliveryLog.user_id == user.id,
            DeliveryLog.answered_at.is_not(None),
            DeliveryLog.answered_at >= since,
        )
        .distinct()
    )

    tzinfo = user.tzinfo
    answered_dates = {
        moment.replace(tzinfo=timezone.utc).astimezone(tzinfo).date()
        for moment in result.scalars().all()
        if moment is not None
    }
    if not answered_dates:
        return 0

    today = now.replace(tzinfo=timezone.utc).astimezone(tzinfo).date()
    first_day = today if today in answered_dates else today - timedelta(days=1)
    if first_day not in answered_dates:
        return 0

    streak = 0
    day = first_day
    while day in answered_dates:
        streak += 1
        day -= timedelta(days=1)
    return streak

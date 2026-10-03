"""Тесты дневного плана (``services/plan.py``).

План дня — это ``users.words_per_day`` плюс повторения, срок которых наступает в
течение сегодняшних суток пользователя. Именно на это число планировщик делит
оставшееся время окна (``services.slots.next_slot``), поэтому здесь проверяются
все способы, которыми число меняется: начатое сегодня слово (в том числе
забранное кнопкой «🎲 Слово»), ответы «знаю» и «не знаю», лимит слов в изучении и
фильтр частей речи.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import User, UserWord
from services import plan, slots


async def put_due(
    session: AsyncSession,
    user_id: int,
    word_id: int,
    now: datetime,
    *,
    days_ago: int = 1,
) -> UserWord:
    """Готовит просроченное слово «в изучении» — повторение к сегодняшнему дню."""
    progress = await repository.get_or_create_user_word(session, user_id, word_id)
    moment = now - timedelta(days=days_ago)
    return await repository.save_user_word(
        session,
        progress,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=moment,
        last_reviewed_at=moment,
        times_correct=0,
        times_wrong=0,
        is_refresh=False,
    )


async def show_word(
    session: AsyncSession, user: User, word_id: int, now: datetime, *, days_ago: int = 0
) -> None:
    """Фиксирует отправку карточки слова в ``delivery_log``."""
    await repository.create_delivery(
        session, user.id, word_id, sent_at=now - timedelta(days=days_ago)
    )


# ---------------------------------------------------------------------------
# Запланированные слова
# ---------------------------------------------------------------------------
async def test_plan_starts_with_words_per_day(session, user, words, now) -> None:
    """Без повторений план дня — это просто ``words_per_day``."""
    assert user.words_per_day == config.WORDS_PER_DAY_DEFAULT
    assert await plan.words_left_today(session, user, now) == config.WORDS_PER_DAY_DEFAULT


async def test_plan_counts_started_word_as_spent(session, user, words, now) -> None:
    """Начатое сегодня слово расходует пункт плана — и по расписанию, и по кнопке."""
    await repository.update_user(session, user, words_per_day=3)

    await show_word(session, user, words[0].id, now)

    assert await plan.words_left_today(session, user, now) == 2


async def test_plan_counts_only_words_started_today(session, user, words, now) -> None:
    """Слово, начатое вчера по поясу пользователя, план сегодня не расходует."""
    await repository.update_user(session, user, words_per_day=2)
    # now — 09:00 UTC, то есть 12:00 по Москве; сутки начались в 21:00 UTC вчера.
    await show_word(session, user, words[0].id, now, days_ago=1)

    assert await plan.words_left_today(session, user, now) == 2


async def test_planned_words_left_stops_when_dictionary_exhausted(
    session, user, words, now
) -> None:
    """Если все слова словаря уже начаты, новых слов в плане нет."""
    await repository.update_user(session, user, words_per_day=5)
    for word in words:
        await repository.get_or_create_user_word(session, user.id, word.id)

    day_start = slots.local_day_start_utc(user, now)

    assert await plan.planned_words_left(session, user, day_start) == 0


# ---------------------------------------------------------------------------
# Повторения: план дня растёт и уменьшается вместе с ними
# ---------------------------------------------------------------------------
async def test_plan_adds_repetitions_due_today(session, user, words, now) -> None:
    """План дня — это запланированные слова плюс повторения, срок которых наступил.

    Пример из постановки: на второй день 15 запланированных слов и 15 слов,
    сброшенных на этап 0 вчерашним «не знаю», — 30 слов (здесь словарь уменьшен:
    2 запланированных и 2 повторения).
    """
    await repository.update_user(session, user, words_per_day=2)
    await put_due(session, user.id, words[0].id, now, days_ago=1)
    await put_due(session, user.id, words[1].id, now, days_ago=1)
    # Одно из повторений вчера уже показывали — сегодня оно всё равно в плане.
    await show_word(session, user, words[0].id, now, days_ago=1)

    assert await plan.words_left_today(session, user, now) == 4


async def test_plan_skips_word_shown_today_without_answer(session, user, words, now) -> None:
    """Показанное сегодня слово без ответа второй раз не планируется."""
    await repository.update_user(session, user, words_per_day=1)
    await put_due(session, user.id, words[0].id, now, days_ago=1)
    # Слово начато вчера, сегодня показано снова — ответа всё ещё нет.
    await show_word(session, user, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now)

    assert await plan.words_left_today(session, user, now) == 1


async def test_plan_returns_word_reset_by_dont_know(session, user, words, now) -> None:
    """«Не знаю» возвращает слово в план дня: его показывают ещё раз."""
    await repository.update_user(session, user, words_per_day=1)
    progress = await put_due(session, user.id, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now)

    answered_at = now + timedelta(minutes=30)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=answered_at,
        last_reviewed_at=answered_at,
        times_correct=0,
        times_wrong=1,
        is_refresh=False,
    )

    assert await plan.words_left_today(session, user, answered_at) == 2


async def test_plan_drops_word_answered_know(session, user, words, now) -> None:
    """«Знаю» убирает слово из плана: следующий показ — только через дни."""
    await repository.update_user(session, user, words_per_day=1)
    progress = await put_due(session, user.id, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now)

    await repository.save_user_word(
        session,
        progress,
        stage=2,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(days=3),
        last_reviewed_at=now,
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )

    assert await plan.words_left_today(session, user, now) == 1


async def test_plan_ignores_distant_and_learned_words(session, user, words, now) -> None:
    """Далёкий срок и освежение выученных слов в план дня не входят."""
    await repository.update_user(session, user, words_per_day=1)
    progress = await put_due(session, user.id, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now, days_ago=1)
    # Выученное слово на освежении приходит отдельным сообщением в том же слоте.
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=now - timedelta(days=1),
        last_reviewed_at=now - timedelta(days=90),
        times_correct=5,
        times_wrong=0,
        is_refresh=True,
    )

    assert await plan.words_left_today(session, user, now) == 1


# ---------------------------------------------------------------------------
# Ограничения: лимит слов в изучении и фильтр частей речи
# ---------------------------------------------------------------------------
async def test_plan_keeps_only_repetitions_when_limit_reached(session, user, words, now) -> None:
    """Достигнут лимит слов в изучении — в плане дня остаются только повторения."""
    await repository.update_user(session, user, words_per_day=5, learning_limit=3)
    for word in words:
        await put_due(session, user.id, word.id, now, days_ago=1)

    day_start = slots.local_day_start_utc(user, now)

    assert await plan.planned_words_left(session, user, day_start) == 0
    assert await plan.words_left_today(session, user, now) == 3


async def test_plan_respects_pos_filter(session, user, words, now) -> None:
    """Фильтр частей речи убирает из плана чужие слова — и повторения, и новые."""
    await repository.update_user(session, user, words_per_day=1)
    await put_due(session, user.id, words[0].id, now, days_ago=1)  # apple — noun
    await put_due(session, user.id, words[1].id, now, days_ago=1)  # quickly — adverb

    assert await plan.words_left_today(session, user, now) == 3

    await repository.set_pos_filter(session, user, [config.POS_ADVERB])

    # Осталось повторение наречия; новых слов нет: остальные слова уже начаты.
    assert await plan.words_left_today(session, user, now) == 1

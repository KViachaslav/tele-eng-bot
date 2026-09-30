"""Тесты выбора слов (``services/word_selector.py``).

Здесь проверяется порядок очереди основного слова: созревшие повторения, которых
сегодня ещё не показывали, идут перед новыми словами, а повторение слова, которое
сегодня уже приходило, возвращается только тогда, когда других вариантов не
осталось. Именно на этом ломался ``/word``: ответ «не знаю» обнуляет срок
повторения (этап 0), поэтому очередь зацикливалась на одних и тех же словах и
новые не приходили.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import User
from services import word_selector


async def put_in_learning(
    session: AsyncSession,
    user_id: int,
    word_id: int,
    now: datetime,
    *,
    hours_ago: int = 1,
) -> None:
    """Готовит слово в статусе «изучается» с уже наступившим сроком повторения."""
    progress = await repository.get_or_create_user_word(session, user_id, word_id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=now - timedelta(hours=hours_ago),
        last_reviewed_at=now - timedelta(hours=hours_ago),
        times_correct=0,
        times_wrong=1,
        is_refresh=False,
    )


async def show_word(
    session: AsyncSession, user: User, word_id: int, now: datetime, *, hours_ago: int = 0
) -> None:
    """Фиксирует отправку карточки слова в ``delivery_log``."""
    await repository.create_delivery(
        session, user.id, word_id, sent_at=now - timedelta(hours=hours_ago)
    )


# ---------------------------------------------------------------------------
# Порядок очереди основного слова
# ---------------------------------------------------------------------------
async def test_new_word_comes_after_due_words_shown_today(session, user, words, now) -> None:
    """Слова, показанные сегодня, не мешают новым.

    Регрессия: пользователь ответил «не знаю» на два слова — у обоих срок
    повторения наступил сразу же, и ``/word`` присылал только их, хотя в словаре
    оставались новые слова.
    """
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=2)
    await put_in_learning(session, user.id, words[1].id, now, hours_ago=1)
    await show_word(session, user, words[0].id, now, hours_ago=1)
    await show_word(session, user, words[1].id, now)

    selected = await word_selector.select_main_word(session, user, now)

    assert selected is not None
    assert selected.word.word == "curious"
    # Новое слово ещё не «в изучении» — прогресса по нему нет.
    assert selected.user_word is None


async def test_due_word_from_yesterday_goes_before_new_word(session, user, words, now) -> None:
    """Созревшее повторение, которого сегодня не было, важнее нового слова."""
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=30)
    await show_word(session, user, words[0].id, now, hours_ago=30)

    selected = await word_selector.select_main_word(session, user, now)

    assert selected is not None
    assert selected.word.word == "apple"
    assert selected.user_word is not None


async def test_todays_repeat_returns_when_new_words_blocked(session, user, words, now) -> None:
    """Если новые слова показать нельзя, повторяется слово, которое уже показывали."""
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=2)
    await put_in_learning(session, user.id, words[1].id, now, hours_ago=1)
    await show_word(session, user, words[0].id, now, hours_ago=1)
    await show_word(session, user, words[1].id, now)
    # Лимит слов в изучении выбран: новых слов не будет.
    await repository.update_user(session, user, learning_limit=2)

    selected = await word_selector.select_main_word(session, user, now)

    assert selected is not None
    # Первым идёт то слово, чей срок повторения наступил раньше.
    assert selected.word.word == "apple"


async def test_excluded_words_are_skipped(session, user, words, now) -> None:
    """Исключённые слова не попадают ни в одну из очередей."""
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=2)
    await show_word(session, user, words[0].id, now)

    selected = await word_selector.select_main_word(
        session, user, now, exclude_word_ids={word.id for word in words}
    )

    assert selected is None


# ---------------------------------------------------------------------------
# Исключения для ручного запроса «дай слово»
# ---------------------------------------------------------------------------
async def test_on_demand_exclusions_skip_only_todays_cards(session, user, words, now) -> None:
    """Карточка без ответа из прошлого не исключает слово из ``/word`` навсегда.

    Регрессия: слово, чью карточку пользователь не закрыл накануне, выпадало из
    очереди ручного запроса до тех пор, пока на эту карточку не ответят.
    """
    await show_word(session, user, words[0].id, now, hours_ago=30)
    await show_word(session, user, words[1].id, now)

    skipped = await word_selector.on_demand_exclusions(session, user, now)

    assert words[1].id in skipped
    assert words[0].id not in skipped


async def test_on_demand_exclusions_skip_last_delivered_word(session, user, words, now) -> None:
    """Последнее отправленное слово тоже исключается, даже если на него ответили."""
    delivery = await repository.create_delivery(session, user.id, words[0].id, sent_at=now)
    await repository.mark_delivery_answered(
        session, delivery, config.ANSWER_DONT_KNOW, answered_at=now
    )

    skipped = await word_selector.on_demand_exclusions(session, user, now)

    assert skipped == {words[0].id}

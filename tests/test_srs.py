"""Юнит-тесты чистой логики SRS (``services/srs.py``)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

import config
from services import srs


def test_stage_intervals_match_spec() -> None:
    """Таблица интервалов совпадает с разделом 6 ТЗ."""
    assert [srs.interval_days(stage) for stage in range(7)] == [0, 1, 3, 7, 14, 30, 90]
    assert srs.refresh_interval_days() == config.SRS_STAGE_INTERVALS_DAYS[config.SRS_MAX_STAGE]


def test_clamp_stage_limits_range() -> None:
    """Этап не выходит за границы 0…6."""
    assert srs.clamp_stage(-3) == config.SRS_FIRST_STAGE
    assert srs.clamp_stage(config.SRS_MAX_STAGE - 1) == config.SRS_MAX_STAGE - 1
    assert srs.clamp_stage(config.SRS_MAX_STAGE + 5) == config.SRS_MAX_STAGE


def test_status_for_stage() -> None:
    """Этап 6 — «выучено», все остальные — «изучается»."""
    assert srs.status_for_stage(0) == config.STATUS_LEARNING
    assert srs.status_for_stage(5) == config.STATUS_LEARNING
    assert srs.status_for_stage(6) == config.STATUS_LEARNED
    assert srs.is_learned_stage(5) is False
    assert srs.is_learned_stage(6) is True


def test_initial_state_is_new_word() -> None:
    """Новое слово: этап 0, статус ``new``, счётчики пустые."""
    state = srs.initial_state()
    assert state.stage == config.SRS_FIRST_STAGE
    assert state.status == config.STATUS_NEW
    assert state.next_review_at is None
    assert (state.times_correct, state.times_wrong) == (0, 0)
    assert state.is_refresh is False


def test_apply_know_increases_stage(now: datetime) -> None:
    """«Знаю» двигает слово на следующий этап и ставит дату повторения."""
    state = srs.apply_answer(srs.initial_state(), config.ANSWER_KNOW, now)
    assert state.stage == 1
    assert state.status == config.STATUS_LEARNING
    assert state.next_review_at == now + timedelta(days=1)
    assert state.times_correct == 1
    assert state.times_wrong == 0
    assert state.is_refresh is False


def test_apply_know_three_times_follows_table(now: datetime) -> None:
    """Три правильных ответа подряд: этапы 1 → 2 → 3 с интервалами по таблице."""
    state = srs.initial_state()
    expected = [(1, 1), (2, 3), (3, 7)]
    for index, (stage, days) in enumerate(expected, start=1):
        state = srs.apply_answer(state, config.ANSWER_KNOW, now)
        assert (state.stage, srs.interval_days(state.stage)) == (stage, days)
        assert state.next_review_at == now + timedelta(days=days)
        assert state.times_correct == index


def test_apply_know_on_last_stage_keeps_learned(now: datetime) -> None:
    """На финальном этапе «Знаю» не поднимает этап выше 6."""
    learned = srs.SrsState(stage=config.SRS_MAX_STAGE, status=config.STATUS_LEARNED)
    state = srs.apply_answer(learned, config.ANSWER_KNOW, now)
    assert state.stage == config.SRS_MAX_STAGE
    assert state.status == config.STATUS_LEARNED
    assert state.next_review_at == now + timedelta(days=90)


def test_apply_dont_know_resets_to_first_stage(now: datetime) -> None:
    """«Не знаю» обнуляет этап и возвращает слово в ближайшую очередь."""
    state = srs.SrsState(stage=4, status=config.STATUS_LEARNING, times_correct=4)
    result = srs.apply_answer(state, config.ANSWER_DONT_KNOW, now)
    assert result.stage == config.SRS_FIRST_STAGE
    assert result.status == config.STATUS_LEARNING
    assert result.next_review_at == now
    assert result.times_wrong == 1
    assert result.times_correct == 4
    assert result.is_refresh is False


def test_apply_dont_know_breaks_learned_word(now: datetime) -> None:
    """Ошибка на освежении возвращает выученное слово в изучаемые."""
    refreshing = srs.SrsState(stage=config.SRS_MAX_STAGE, status=config.STATUS_LEARNED, is_refresh=True)
    result = srs.apply_answer(refreshing, config.ANSWER_DONT_KNOW, now)
    assert result.stage == config.SRS_FIRST_STAGE
    assert result.status == config.STATUS_LEARNING
    assert result.is_refresh is False
    assert result.next_review_at == now


def test_apply_answer_rejects_unknown_value(now: datetime) -> None:
    """Неизвестный ответ — программная ошибка, а не молчаливый сброс."""
    with pytest.raises(ValueError, match="Неизвестный ответ"):
        srs.apply_answer(srs.initial_state(), "maybe", now)


def test_describe_interval() -> None:
    """Пара «этап, дней до показа» для текста результата."""
    state = srs.SrsState(stage=3, status=config.STATUS_LEARNING)
    assert srs.describe_interval(state) == (3, 7)

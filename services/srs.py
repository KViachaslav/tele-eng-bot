"""Логика интервального повторения (SRS).

Модуль не обращается к БД и к Telegram: только чистые функции от этапа слова и
ответа пользователя. Именно поэтому его удобно покрывать юнит-тестами.

Единственная зависимость «извне» — окно рассылки
(:class:`services.slots.WindowBounds`): рассчитанный срок повторения передаётся
:func:`services.slots.align_to_window`, чтобы слово всегда можно было показать
внутри окна (см. :func:`apply_answer`).

Таблица этапов (раздел 6 ТЗ):

======  =============================
Этап     Интервал до следующего показа
======  =============================
0        в очередь на сегодня
1        1 день
2        3 дня
3        7 дней
4        14 дней
5        30 дней
6        90 дней (статус ``learned``)
======  =============================
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

import config
from services import slots


@dataclass(frozen=True, slots=True)
class SrsState:
    """Состояние слова в SRS для конкретного пользователя."""

    stage: int = config.SRS_FIRST_STAGE
    status: str = config.STATUS_NEW
    next_review_at: datetime | None = None
    times_correct: int = 0
    times_wrong: int = 0
    is_refresh: bool = False


def clamp_stage(stage: int) -> int:
    """Ограничивает этап допустимым диапазоном 0…6."""
    return max(config.SRS_FIRST_STAGE, min(config.SRS_MAX_STAGE, int(stage)))


def interval_days(stage: int) -> int:
    """Интервал в днях для этапа (этап 0 — «в очередь на сегодня», т.е. 0)."""
    return config.SRS_STAGE_INTERVALS_DAYS[clamp_stage(stage)]


def is_learned_stage(stage: int) -> bool:
    """Достигнут ли финальный этап (слово считается выученным)."""
    return clamp_stage(stage) >= config.SRS_MAX_STAGE


def status_for_stage(stage: int) -> str:
    """Статус слова, соответствующий этапу.

    Этап 6 — «выучено», все остальные этапы (включая 0) — «в процессе».
    """
    return config.STATUS_LEARNED if is_learned_stage(stage) else config.STATUS_LEARNING


def next_review_datetime(stage: int, now_utc: datetime) -> datetime:
    """Когда показать слово снова.

    Для этапа 0 интервал равен нулю — слово возвращается в ближайший слот
    рассылки, то есть доступно сразу («в очередь на сегодня»).
    """
    return now_utc + timedelta(days=interval_days(stage))


def _due_at(moment: datetime, window: slots.WindowBounds | None) -> datetime:
    """Срок повторения внутри окна рассылки (см. :func:`apply_answer`)."""
    if window is None:
        return moment
    return slots.align_to_window(window, moment)


def refresh_interval_days() -> int:
    """Интервал освежения выученных слов (этап 6)."""
    return interval_days(config.SRS_MAX_STAGE)


def apply_answer(
    state: SrsState,
    answer: str,
    now_utc: datetime,
    *,
    window: slots.WindowBounds | None = None,
) -> SrsState:
    """Применяет ответ пользователя и возвращает новое состояние слова.

    * «Знаю» — этап увеличивается на 1 (не выше 6), интервал берётся из таблицы;
    * «Не знаю» — этап сбрасывается в 0, слово возвращается в ближайшее окно,
      в том числе если это было освежение выученного слова.

    :param window: окно рассылки пользователя
        (:class:`services.slots.WindowBounds`). Если оно задано, рассчитанный срок
        сдвигается внутрь окна (:func:`services.slots.align_to_window`): отвечают и
        после закрытия окна, а слово со сроком «за окном» не может показать ни один
        слот — план дня считал его не отданным, а расписание его не находило
        (в базе 09.10.2026 «план дня 2», «отправлено 0»).
    :raises ValueError: если ответ не из :data:`config.ANSWERS`.
    """
    if answer not in config.ANSWERS:
        raise ValueError(f"Неизвестный ответ: {answer!r}")

    if answer == config.ANSWER_KNOW:
        stage = clamp_stage(state.stage + 1)
        return replace(
            state,
            stage=stage,
            status=status_for_stage(stage),
            next_review_at=_due_at(next_review_datetime(stage, now_utc), window),
            times_correct=state.times_correct + 1,
            is_refresh=False,
        )

    return replace(
        state,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=_due_at(
            next_review_datetime(config.SRS_FIRST_STAGE, now_utc), window
        ),
        times_wrong=state.times_wrong + 1,
        is_refresh=False,
    )


def initial_state() -> SrsState:
    """Состояние нового слова."""
    return SrsState()


def describe_interval(state: SrsState) -> tuple[int, int]:
    """Возвращает пару ``(этап, дней до следующего показа)`` для текста ответа."""
    return state.stage, interval_days(state.stage)

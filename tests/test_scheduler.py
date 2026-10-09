"""Тесты планировщика: план дня решает, когда придёт следующее слово.

``SchedulerService.schedule_user`` — единственное место, где план дня
(:mod:`services.plan`) превращается в дату следующей отправки, поэтому здесь этот
путь проверяется целиком: план из БД → интервал → задача в APScheduler. Пример из
постановки — «15 запланированных слов и 15 повторений делят 11 часов на 30» —
здесь записан уменьшенным: одно запланированное слово и два повторения.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import config
from db import repository
from db.models import User
from services import plan, slots
from services.scheduler import SchedulerService, job_id_for

#: Окно на все сутки: тест не зависит от того, в какой час он запущен.
FULL_DAY = ("00:00", "23:59")

#: Момент расчёта: 15:00 по Москве.
NOW = datetime(2026, 1, 15, 12, 0)


class StubBot:
    """Заглушка ``Bot``: планировщику он нужен только для отправки слов."""


async def make_user(session, telegram_id: int, *, words_per_day: int) -> User:
    """Пользователь с окном на все сутки — для проверки интервала."""
    return await repository.create_user(
        session,
        telegram_id=telegram_id,
        words_per_day=words_per_day,
        window_start=FULL_DAY[0],
        window_end=FULL_DAY[1],
    )


async def put_due(session, user: User, word_id: int, now: datetime) -> None:
    """Готовит вчерашнее «не знаю»: этап 0, срок повторения наступил сразу."""
    progress = await repository.get_or_create_user_word(session, user.id, word_id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=now - timedelta(hours=1),
        last_reviewed_at=now - timedelta(days=1),
        times_correct=0,
        times_wrong=1,
        is_refresh=False,
    )


def interval(moment: datetime | None, now: datetime = NOW) -> float:
    """Сколько минут осталось до следующего слова."""
    assert moment is not None
    return (moment - slots.as_utc(now)).total_seconds() / 60


# ---------------------------------------------------------------------------
# План дня → интервал → задача
# ---------------------------------------------------------------------------
async def test_schedule_user_adds_job_for_next_word(session, user, words) -> None:
    """Расписание ставит ровно одну задачу на пользователя."""
    scheduler = SchedulerService(StubBot())

    moment = await scheduler.schedule_user(session, user, now=NOW)

    job = scheduler.scheduler.get_job(job_id_for(user.id))
    assert job is not None
    assert job.trigger.run_date == moment
    assert moment is not None and moment > slots.as_utc(NOW)


async def test_interval_follows_day_plan(session, words) -> None:
    """Чем больше слов в плане дня, тем чаще они приходят."""
    scheduler = SchedulerService(StubBot())
    thick_user = await make_user(session, 3001, words_per_day=3)
    thin_user = await make_user(session, 3002, words_per_day=1)

    thick = await scheduler.schedule_user(session, thick_user, now=NOW)
    thin = await scheduler.schedule_user(session, thin_user, now=NOW)

    # Три слова вместо одного — интервал втрое короче.
    assert interval(thick) == pytest.approx(interval(thin) / 3, rel=0.002)


async def test_repetitions_of_the_day_shorten_interval(session, user, words) -> None:
    """Повторения входят в план дня: 1 запланированное + 2 повторения — втрое чаще."""
    await repository.update_user(
        session, user, words_per_day=1, window_start=FULL_DAY[0], window_end=FULL_DAY[1]
    )
    scheduler = SchedulerService(StubBot())
    alone = await scheduler.schedule_user(session, user, now=NOW)

    for word in words[:2]:
        await put_due(session, user, word.id, NOW)
    together = await scheduler.schedule_user(session, user, now=NOW)

    assert await plan.words_left_today(session, user, NOW) == 3
    assert interval(together) == pytest.approx(interval(alone) / 3, rel=0.002)


async def test_spent_word_moves_next_word_later(session, user, words) -> None:
    """Забранное кнопкой слово разряжает график: следующее придёт позже."""
    await repository.update_user(
        session, user, words_per_day=3, window_start=FULL_DAY[0], window_end=FULL_DAY[1]
    )
    scheduler = SchedulerService(StubBot())
    before = await scheduler.schedule_user(session, user, now=NOW)

    # Пользователь забрал слово кнопкой «🎲 Слово»: в плане дня на одно меньше.
    await repository.create_delivery(session, user.id, words[0].id, sent_at=NOW)
    after = await scheduler.schedule_user(session, user, now=NOW)

    assert interval(after) == pytest.approx(interval(before) * 3 / 2, rel=0.002)


async def test_delivered_repeat_moves_next_word_later(session, user, words) -> None:
    """Пришедшее повторение разряжает график: следующее слово придёт позже.

    Регрессия: слово, на которое сегодня ответили «не знаю», вернулось в план дня
    и оставалось в нём даже после повторной отправки — до следующего ответа. План
    дня не убывал от отправок, поэтому интервал до следующего слова только
    сокращался к концу окна, в том числе после кнопки «🎲 Слово».
    """
    await repository.update_user(
        session, user, words_per_day=1, window_start=FULL_DAY[0], window_end=FULL_DAY[1]
    )
    # Слово начато вчера, сегодня на него ответили «не знаю» — срок наступил снова.
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=NOW - timedelta(hours=1),
        last_reviewed_at=NOW - timedelta(hours=1),
        times_correct=0,
        times_wrong=1,
        is_refresh=False,
    )
    await repository.create_delivery(
        session, user.id, words[0].id, sent_at=NOW - timedelta(days=1)
    )
    scheduler = SchedulerService(StubBot())
    before = await scheduler.schedule_user(session, user, now=NOW)

    # Повторение пришло снова: карточка ждёт ответа, план дня — на слово меньше.
    await repository.create_delivery(session, user.id, words[0].id, sent_at=NOW)
    after = await scheduler.schedule_user(session, user, now=NOW)

    assert interval(after) == pytest.approx(interval(before) * 2, rel=0.002)


async def test_empty_plan_waits_for_next_window(session, user, words) -> None:
    """План дня выполнен — следующее слово придёт с открытием окна."""
    await repository.update_user(
        session, user, words_per_day=1, window_start=FULL_DAY[0], window_end=FULL_DAY[1]
    )
    await repository.create_delivery(session, user.id, words[0].id, sent_at=NOW)
    scheduler = SchedulerService(StubBot())

    moment = await scheduler.schedule_user(session, user, now=NOW)

    # План пуст: ждём полночи по Москве, то есть 21:00 UTC тех же суток.
    assert moment == datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc)


async def test_paused_user_has_no_job(session, user, words) -> None:
    """Пользователь на паузе задачи не получает, а прежняя задача снимается."""
    scheduler = SchedulerService(StubBot())
    assert await scheduler.schedule_user(session, user, now=NOW) is not None

    await repository.update_user(session, user, paused=True)

    assert await scheduler.schedule_user(session, user, now=NOW) is None
    assert scheduler.scheduler.get_job(job_id_for(user.id)) is None


# ---------------------------------------------------------------------------
# Надёжность: задача ставится заново, страж возвращает потерянные расписания
# ---------------------------------------------------------------------------
class RecordingBot:
    """Заглушка ``Bot``: запоминает, сколько карточек ушло пользователю."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        """Аналог ``Bot.send_message`` (отправка слова в личном чате)."""
        self.sent.append(text)


async def test_delivered_slot_reschedules_next_word(session, user, words) -> None:
    """Слот отправляет слово и сразу ставит следующую задачу.

    Задача одна на пользователя, поэтому без перепланирования после отправки
    расписание исчезло бы — ровно это и случилось 09.10.2026: «план дня 2»,
    «слов отправлено 0» и ни одной задачи в планировщике.
    """
    await repository.update_user(
        session, user, window_start=FULL_DAY[0], window_end=FULL_DAY[1]
    )
    # Слот работает со своей сессией, поэтому подготовку фиксируем в базе заранее.
    await session.commit()
    bot = RecordingBot()
    scheduler = SchedulerService(bot)

    await scheduler._deliver_due_word(user.id)

    assert len(bot.sent) == 1
    job = scheduler.scheduler.get_job(job_id_for(user.id))
    assert job is not None
    assert job.trigger.run_date > datetime.now(timezone.utc)


async def test_slot_outside_window_still_reschedules(session, user, words) -> None:
    """Слот вне окна слово не отправляет, но задачу ставит заново.

    Окно — одна минута в начале суток, поэтому слот почти всегда попадает «вне
    окна»: раньше он просто выходил, а расписание пользователя терялось.
    """
    await repository.update_user(session, user, window_start="00:00", window_end="00:01")
    await session.commit()
    scheduler = SchedulerService(RecordingBot())

    await scheduler._deliver_due_word(user.id)

    job = scheduler.scheduler.get_job(job_id_for(user.id))
    assert job is not None
    assert job.trigger.run_date > datetime.now(timezone.utc)


async def test_watchdog_restores_lost_schedule(session, user, words) -> None:
    """Страж ставит задачу заново, если её вовсе нет."""
    await session.commit()
    scheduler = SchedulerService(StubBot())
    scheduler.start()
    try:
        await scheduler.schedule_user(session, user, now=NOW)
        scheduler.unschedule_user(user.id)

        await scheduler.watchdog()

        job = scheduler.scheduler.get_job(job_id_for(user.id))
        assert job is not None
        assert job.next_run_time > datetime.now(timezone.utc)
    finally:
        await scheduler.shutdown()


async def test_watchdog_rearms_job_with_past_date(session, user, words) -> None:
    """Страж переставляет задачу, срок которой уже прошёл.

    Пропущенный из-за простоя бота слот оставлял задачу в прошлом: ``DateTrigger``
    сам по себе больше не срабатывает, поэтому без стража слово не приходило.
    """
    await repository.update_user(
        session, user, window_start=FULL_DAY[0], window_end=FULL_DAY[1]
    )
    await session.commit()
    scheduler = SchedulerService(StubBot())
    scheduler.start()
    try:
        await scheduler.schedule_user(session, user, now=NOW)  # NOW — момент в прошлом
        past = scheduler.scheduler.get_job(job_id_for(user.id))
        assert past is not None
        assert past.next_run_time < datetime.now(timezone.utc)

        await scheduler.watchdog()

        job = scheduler.scheduler.get_job(job_id_for(user.id))
        assert job is not None
        assert job.next_run_time > datetime.now(timezone.utc)
    finally:
        await scheduler.shutdown()


async def test_watchdog_job_is_registered_and_removed(session, user, words) -> None:
    """``start`` ставит стража расписаний, ``shutdown`` — снимает."""
    scheduler = SchedulerService(StubBot())

    scheduler.start()
    try:
        watchdog_job = scheduler.scheduler.get_job(config.SCHEDULER_WATCHDOG_JOB_ID)
        assert watchdog_job is not None
        assert (
            watchdog_job.trigger.interval.total_seconds()
            == config.SCHEDULER_WATCHDOG_SECONDS
        )
    finally:
        await scheduler.shutdown()

    assert scheduler.scheduler.get_job(config.SCHEDULER_WATCHDOG_JOB_ID) is None


async def test_user_job_allows_misfire_grace(session, user, words) -> None:
    """Задаче разрешено опоздание: короткий рестарт бота не «стоит» слова."""
    scheduler = SchedulerService(StubBot())

    await scheduler.schedule_user(session, user, now=NOW)

    job = scheduler.scheduler.get_job(job_id_for(user.id))
    assert job is not None
    assert job.misfire_grace_time == config.SCHEDULER_MISFIRE_GRACE_SECONDS
    assert job.coalesce is True


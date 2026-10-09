"""Тесты дневного плана (``services/plan.py``).

План дня — это ``users.words_per_day`` плюс повторения, срок которых наступает в
течение сегодняшних суток пользователя. Именно на это число планировщик делит
оставшееся время окна (``services.slots.next_slot``), поэтому здесь проверяются
все способы, которыми число меняется: начатое сегодня слово (в том числе
забранное кнопкой «🎲 Слово»), ответы «знаю» и «не знаю», лимит слов в изучении и
фильтр частей речи. Повторения считаются на всех этапах (0–5), а не только на
этапе 0 после «не знаю»: выученные слова плана не занимают.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import User, UserWord, Word
from services import plan, slots

#: Дополнительные слова: вместе с фикстурой ``words`` их ровно семь, чтобы занять
#: каждый этап SRS (0–6) в одном тесте
#: (см. :func:`test_plan_counts_repetitions_of_every_stage`).
EXTRA_STAGE_WORDS: tuple[dict[str, object], ...] = (
    {"word": "lantern", "part_of_speech": "noun"},
    {"word": "harvest", "part_of_speech": "noun"},
    {"word": "sleepy", "part_of_speech": "adjective"},
    {"word": "steady", "part_of_speech": "adjective"},
)


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


async def test_plan_drops_word_shown_again_after_answer(session, user, words, now) -> None:
    """Повторно отправленное слово план дня больше не занимает.

    Регрессия: слово, на которое ответили «не знаю» (в план оно вернулось), а
    затем пришло второй раз, продолжало занимать план дня до ответа на новую
    карточку. План дня из-за этого не убывал от отправок, и интервал до
    следующего слова только сокращался к концу окна — в том числе после нажатия
    кнопки «🎲 Слово».
    """
    await repository.update_user(session, user, words_per_day=1)
    progress = await put_due(session, user.id, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now, days_ago=1)
    await show_word(session, user, words[0].id, now)

    # «Не знаю» вернуло слово в план дня: его покажут ещё раз (1 новое + 1 повторение).
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

    # Слово пришло снова: карточка ждёт ответа, поэтому план дня на слово легче.
    again_at = answered_at + timedelta(minutes=1)
    await show_word(session, user, words[0].id, again_at)

    assert await plan.words_left_today(session, user, again_at) == 1


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
async def test_plan_counts_repetitions_of_every_stage(session, user, words, now) -> None:
    """Повторения «на сегодня» входят в план на любом этапе 0–5, а не только после «не знаю».

    У каждого этапа свой срок (1/3/7/14/30 дней), поэтому в план дня попадают и
    слова, сброшенные на этап 0 ответом «не знаю», и обычные повторения этапов
    1–5. Выученное слово (этап 6) план не занимает: освежение приходит отдельным
    сообщением в том же слоте.
    """
    await repository.upsert_words(session, EXTRA_STAGE_WORDS)
    result = await session.execute(select(Word).order_by(Word.id))
    dictionary = list(result.scalars().all())
    assert len(dictionary) == config.SRS_MAX_STAGE + 1, "нужны слова для всех этапов 0–6"
    # Новых слов в плане нет: все слова словаря уже начаты.
    for word in dictionary:
        await repository.get_or_create_user_word(session, user.id, word.id)

    for stage, word in enumerate(dictionary):
        progress = await repository.get_user_word(session, user.id, word.id)
        assert progress is not None
        await repository.save_user_word(
            session,
            progress,
            stage=stage,
            status=(
                config.STATUS_LEARNED
                if stage == config.SRS_MAX_STAGE
                else config.STATUS_LEARNING
            ),
            next_review_at=now - timedelta(days=1),
            last_reviewed_at=now - timedelta(days=1),
            times_correct=stage,
            times_wrong=0,
            is_refresh=stage == config.SRS_MAX_STAGE,
        )

    # Шесть слов «в изучении» (этапы 0–5) и ни одного выученного.
    assert await plan.words_left_today(session, user, now) == config.SRS_MAX_STAGE


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


# ---------------------------------------------------------------------------
# Прогноз на несколько дней: экран «📅 План слов»
# ---------------------------------------------------------------------------
async def test_upcoming_plan_starts_with_today(session, user, words, now) -> None:
    """Первый день прогноза — сегодня, и он совпадает с планом дня.

    Новые слова здесь ограничены ещё и словарём: неизученных слов три, а норма —
    два, поэтому на сегодня попадают ровно два (``words_left_today`` при нехватке
    слов оставляет в плане больше — лишнего просто не найдётся при отправке).
    """
    await repository.update_user(session, user, words_per_day=2)

    days = await plan.upcoming_plan(session, user, now, config.FORECAST_DAYS)

    assert [day.local_day for day in days] == [
        date(2026, 1, 15),
        date(2026, 1, 16),
        date(2026, 1, 17),
    ]
    assert days[0].total == await plan.words_left_today(session, user, now)


async def test_upcoming_plan_spends_new_words_day_by_day(session, user, words, now) -> None:
    """Новые слова расходуются по дням: три неизученных при норме два — 2, 1, 0."""
    await repository.update_user(session, user, words_per_day=2)

    days = await plan.upcoming_plan(session, user, now, 3)

    assert [day.new_words for day in days] == [2, 1, 0]
    assert [day.total for day in days] == [2, 1, 0]


async def test_upcoming_plan_puts_due_reviews_into_their_days(session, user, words, now) -> None:
    """Повторение попадает в те сутки, на которые приходится его срок.

    Слово, срок которого наступает завтра, сегодня не планируется, зато завтра
    занимает место рядом с новыми словами. В третий день оно не считается
    повторно: просроченные слова прогноз не дублирует по дням. Новых слов всего
    два (одно уже в изучении), поэтому к третьему дню они заканчиваются.
    """
    await repository.update_user(session, user, words_per_day=1)
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE + 1,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(days=1),
        last_reviewed_at=now,
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )

    days = await plan.upcoming_plan(session, user, now, 3)

    assert [(day.new_words, day.reviews) for day in days] == [(1, 0), (1, 1), (0, 0)]


async def test_upcoming_plan_has_no_new_words_at_learning_limit(
    session, user, words, now
) -> None:
    """Достигнут лимит слов в изучении — во все дни приходят только повторения."""
    await repository.update_user(session, user, words_per_day=5, learning_limit=1)
    await put_due(session, user.id, words[0].id, now, days_ago=1)

    days = await plan.upcoming_plan(session, user, now, 3)

    assert [day.new_words for day in days] == [0, 0, 0]
    assert [day.reviews for day in days] == [1, 0, 0]


async def test_upcoming_plan_skips_learned_words(session, user, words, now) -> None:
    """Выученное слово прогноз не занимает: на освежение оно придёт отдельно."""
    await repository.update_user(session, user, words_per_day=1)
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=now + timedelta(days=1),
        last_reviewed_at=now,
        times_correct=config.SRS_MAX_STAGE,
        times_wrong=0,
        is_refresh=True,
    )

    days = await plan.upcoming_plan(session, user, now, 2)

    assert [day.reviews for day in days] == [0, 0]


async def test_upcoming_plan_respects_pos_filter(session, user, words, now) -> None:
    """Фильтр частей речи убирает из прогноза и новые слова, и повторения."""
    await repository.update_user(session, user, words_per_day=1)
    await put_due(session, user.id, words[0].id, now, days_ago=1)  # apple — noun
    await repository.set_pos_filter(session, user, [config.POS_ADVERB])

    days = await plan.upcoming_plan(session, user, now, 3)

    # Новое слово осталось только наречие quickly; повторение существительного скрыто.
    assert [(day.new_words, day.reviews) for day in days] == [(1, 0), (0, 0), (0, 0)]


# ---------------------------------------------------------------------------
# Диагностика пустого слота
# ---------------------------------------------------------------------------
async def test_describe_empty_queue_reports_reason(session, user, words, now) -> None:
    """Пустой слот объясняется планом дня, очередью по этапам и ближайшим сроком.

    Ради этого отчёта и появилась диагностика: без неё в логе рядом стояли «план дня
    2» и «слов отправлено 0» (09.10.2026), а причину приходилось искать запросами к
    базе (см. :func:`services.plan.describe_empty_queue`).
    """
    await repository.update_user(session, user, learning_limit=1)
    await put_due(session, user.id, words[0].id, now, days_ago=1)
    progress = await repository.get_or_create_user_word(session, user.id, words[1].id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(hours=3),
        last_reviewed_at=now - timedelta(hours=1),
        times_correct=0,
        times_wrong=1,
        is_refresh=False,
    )

    report = await plan.describe_empty_queue(session, user, now)

    assert "план дня 2 (новых 0, повторений 2)" in report
    assert "этап 0 — 1, этап 1 — 1" in report
    assert "висящих карточек 0" in report
    assert "ближайший срок 15.01 15:00" in report
    assert "окно 09:00–21:00" in report


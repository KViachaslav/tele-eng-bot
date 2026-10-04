"""Тесты выбора слов (``services/word_selector.py``).

Здесь проверяется порядок очереди основного слова: созревшие повторения, которые
снова ждут отправки (сегодня карточки не было или на неё ответили), идут перед
новыми словами, а карточка, которая висит в чате без ответа, второй раз не приходит.
Именно на этом ломался ``/word``: ответ «не знаю» обнуляет срок повторения
(этап 0), поэтому очередь зацикливалась на одних и тех же словах и новые не
приходили. По той же причине в рассылке по слотам одно слово приходило подряд,
пока не закрывалось окно.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import DeliveryLog, User, UserWord
from services import word_selector


async def put_in_learning(
    session: AsyncSession,
    user_id: int,
    word_id: int,
    now: datetime,
    *,
    hours_ago: int = 1,
) -> UserWord:
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
    return progress


async def show_word(
    session: AsyncSession, user: User, word_id: int, now: datetime, *, hours_ago: int = 0
) -> DeliveryLog:
    """Фиксирует отправку карточки слова в ``delivery_log`` и возвращает её запись."""
    return await repository.create_delivery(
        session, user.id, word_id, sent_at=now - timedelta(hours=hours_ago)
    )


async def answer_dont_know(
    session: AsyncSession, progress: UserWord, delivery: DeliveryLog, now: datetime
) -> None:
    """Имитирует ответ «не знаю» после карточки: этап 0, срок повторения наступил сразу."""
    await repository.mark_delivery_answered(
        session, delivery, config.ANSWER_DONT_KNOW, answered_at=now
    )
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=now,
        last_reviewed_at=now,
        times_correct=0,
        times_wrong=1,
        is_refresh=False,
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


async def test_dont_know_word_waits_for_sending_again(session, user, words, now) -> None:
    """Слово, отвеченное «не знаю» после карточки, снова ждёт отправки.

    Регрессия: условие «сегодня уже показывали» смотрело только на отправку и не
    смотрело на ответ. Поэтому слово, закрытое «не знаю» (этап 0 — срок повторения
    наступил сразу), в план дня попадало, а очередь показать его не могла: каждый
    выбор упирался в самое просроченное слово, карточка на которое осталась без
    ответа. Так одно и то же слово и приходило снова и снова.
    """
    # Дневной план выполнен: новых слов сегодня не будет.
    await repository.update_user(session, user, words_per_day=1)
    progress = await put_in_learning(session, user.id, words[1].id, now, hours_ago=30)
    delivery = await show_word(session, user, words[1].id, now, hours_ago=2)
    # Ответ пришёл уже после отправки — слово снова ждёт отправки.
    await answer_dont_know(session, progress, delivery, now - timedelta(hours=1))

    selected = await word_selector.select_main_word(session, user, now)

    assert selected is not None
    assert selected.word.word == "quickly"
    assert selected.user_word is not None


async def test_hanging_card_is_not_repeated(session, user, words, now) -> None:
    """Карточка без ответа второй раз не приходит: лучше пустой слот, чем повтор.

    Регрессия: очередь всегда отдавала самое просроченное слово, а ответа на его
    карточку нет — прогресс не меняется, порядок очереди тоже, поэтому слово уходило
    в чат каждые пару минут, пока не закрывалось окно рассылки (в базе 04.10.2026
    «seat» — 11 раз подряд). Здесь новых слов показать нельзя (лимит изучения
    выбран), других повторений нет: оба слова уже показаны и ждут ответа.
    """
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=2)
    await put_in_learning(session, user.id, words[1].id, now, hours_ago=1)
    await show_word(session, user, words[0].id, now, hours_ago=1)
    await show_word(session, user, words[1].id, now)
    # Лимит слов в изучении выбран: новых слов не будет.
    await repository.update_user(session, user, learning_limit=2)

    assert await word_selector.select_main_word(session, user, now) is None


async def test_daily_plan_blocks_new_words(session, user, words, now) -> None:
    """Новых слов сверх дневного плана не выдаём — ни по кнопке, ни по расписанию.

    Регрессия: кнопка «🎲 Слово» отдавала новые слова сверх ``words_per_day``
    (за день их набиралось вдвое больше плана). Такая отправка план дня не
    уменьшала, поэтому интервал до следующего слова не увеличивался, а к концу
    окна только сокращался.
    """
    await repository.update_user(session, user, words_per_day=2)
    await show_word(session, user, words[0].id, now)

    # Один пункт дневного плана ещё свободен — новое слово приходит.
    selected = await word_selector.select_main_word(session, user, now)

    assert selected is not None
    assert selected.user_word is None

    await show_word(session, user, words[1].id, now)

    # Дневной план выполнен: новых слов больше нет.
    assert await word_selector.select_main_word(session, user, now) is None


async def test_excluded_words_are_skipped(session, user, words, now) -> None:
    """Исключённые слова не попадают ни в одну из очередей."""
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=2)
    await show_word(session, user, words[0].id, now)

    selected = await word_selector.select_main_word(
        session, user, now, exclude_word_ids={word.id for word in words}
    )

    assert selected is None


# ---------------------------------------------------------------------------
# Висящие карточки: слово без ответа очередь не повторяет
# ---------------------------------------------------------------------------
async def test_hanging_card_word_ids_skip_only_todays_cards(session, user, words, now) -> None:
    """Висящая карточка отделяется от отвеченной, а вчерашняя уже не «висит».

    Слово, на которое ответили, прогресс получило — его повторять можно. Карточка
    без ответа второй раз не приходит: прогресс она не двигает, и очередь каждый
    раз выбирала бы одно и то же слово (см.
    :func:`services.word_selector.select_main_word`). Карточка прошлых суток перед
    глазами уже не «висит» и слово из очереди не вытесняет.
    """
    await show_word(session, user, words[0].id, now)
    answered = await show_word(session, user, words[1].id, now)
    await repository.mark_delivery_answered(
        session, answered, config.ANSWER_KNOW, answered_at=now
    )
    await show_word(session, user, words[2].id, now, hours_ago=30)

    hanging = await word_selector.hanging_card_word_ids(session, user, now)

    assert hanging == {words[0].id}


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


async def test_on_demand_exclusions_skip_word_answered_today(session, user, words, now) -> None:
    """Слово, на которое сегодня ответили «не знаю», в ручной запрос не вернётся.

    Регрессия: ответ обнуляет срок повторения (этап 0 — слово снова «на сегодня»),
    а исключалась только самая последняя отправка — поэтому ``/word`` ходил по
    кругу из двух-трёх слов, которые пользователь только что ответил.
    """
    delivery_log = await repository.create_delivery(session, user.id, words[0].id, sent_at=now)
    await repository.mark_delivery_answered(
        session, delivery_log, config.ANSWER_DONT_KNOW, answered_at=now
    )

    skipped = await word_selector.on_demand_exclusions(session, user, now)

    assert words[0].id in skipped


async def test_on_demand_exclusions_skip_last_delivered_word(session, user, words, now) -> None:
    """Последнее отправленное слово исключается, даже если оно из прошлого дня."""
    delivery = await repository.create_delivery(session, user.id, words[0].id, sent_at=now)
    await repository.mark_delivery_answered(
        session, delivery, config.ANSWER_DONT_KNOW, answered_at=now
    )

    skipped = await word_selector.on_demand_exclusions(session, user, now)

    assert skipped == {words[0].id}


# ---------------------------------------------------------------------------
# Пустая очередь: есть ли слово вообще
# ---------------------------------------------------------------------------
async def test_has_candidate_word_sees_word_shown_today(session, user, words, now) -> None:
    """Слово показывали сегодня — очередь пуста именно из-за показа.

    Фильтр по части речи оставляет одно слово: новых слов нет, а оно уже в чате.
    Так бот понимает, что причина пустого ``/word`` — сегодняшние отправки, и
    говорит об этом пользователю (см. :func:`handlers.common.no_word_text`).
    """
    await repository.set_pos_filter(session, user, {config.POS_NOUN})
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=1)
    await show_word(session, user, words[0].id, now)

    assert await word_selector.has_candidate_word(session, user, now) is True


async def test_has_candidate_word_false_without_words(session, user, words, now) -> None:
    """Ни одного кандидата: слов нет по настройкам, а не из-за сегодняшних показов."""
    await repository.update_user(session, user, words_per_day=1)
    await show_word(session, user, words[0].id, now)

    assert await word_selector.has_candidate_word(session, user, now) is False

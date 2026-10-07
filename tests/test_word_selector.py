"""Тесты выбора слов (``services/word_selector.py``).

Здесь проверяется порядок очереди основного слова: созревшие повторения, которые
снова ждут отправки (сегодня карточки не было или на неё ответили), идут перед
новыми словами, а карточка, которая висит в чате без ответа, второй раз не приходит.
Именно на этом ломался ``/word``: ответ «не знаю» обнуляет срок повторения
(этап 0), поэтому очередь зацикливалась на одних и тех же словах и новые не
приходили. По той же причине в рассылке по слотам одно слово приходило подряд,
пока не закрывалось окно.

Отдельно проверяется ручной запрос «дай слово»: он идёт по плану дня
(:func:`services.word_selector.select_plan_word` — повторения со сроком до конца
местных суток, затем новое слово), а после выполнения плана отдаёт слова дня,
которые уже приходили и получили ответ (:func:`select_repeat_word`). Зациклиться
на одном слове не даёт :func:`on_demand_exclusions`: висящая карточка не
повторяется, а слово, исчерпавшее лимит показов за сутки
(``config.ON_DEMAND_MAX_SENDS_PER_DAY``), ручной запрос больше не присылает.
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
# Ручной запрос «дай слово»: ограничения против повторов
# ---------------------------------------------------------------------------
async def test_on_demand_exclusions_skip_hanging_cards(session, user, words, now) -> None:
    """Висящая карточка ручному запросу слово не отдаёт, а вчерашняя слову не мешает.

    Карточка без ответа в чате: повтор прогресс не двигает, а две одинаковые
    карточки подряд ни к чему. Карточка прошлых суток перед глазами уже не «висит»
    и слово из очереди не вытесняет (см. :func:`services.slots.local_day_start_utc`).
    """
    await show_word(session, user, words[0].id, now, hours_ago=30)
    await show_word(session, user, words[1].id, now)

    skipped = await word_selector.on_demand_exclusions(session, user, now)

    assert words[1].id in skipped
    assert words[0].id not in skipped


async def test_on_demand_exclusions_skip_word_sent_twice_today(session, user, words, now) -> None:
    """Слово, приходившее сегодня дважды, ручной запрос заново не отдаёт.

    Ответ «не знаю» возвращает слово на этап 0 (срок повторения наступает сразу),
    и без счётчика показов пара «нажатие → «не знаю»» ходила бы по кругу одного
    слова: запрос никогда не сообщил бы, что план дня выполнен
    (``config.ON_DEMAND_MAX_SENDS_PER_DAY``).
    """
    first = await show_word(session, user, words[0].id, now, hours_ago=2)
    await repository.mark_delivery_answered(
        session, first, config.ANSWER_DONT_KNOW, answered_at=now - timedelta(hours=2)
    )
    second = await show_word(session, user, words[0].id, now, hours_ago=1)
    await repository.mark_delivery_answered(
        session, second, config.ANSWER_DONT_KNOW, answered_at=now - timedelta(minutes=30)
    )

    skipped = await word_selector.on_demand_exclusions(session, user, now)

    assert words[0].id in skipped


# ---------------------------------------------------------------------------
# План дня и повтор дня: что отдаёт кнопка «🎲 Слово»
# ---------------------------------------------------------------------------
async def test_select_plan_word_takes_review_due_later_today(session, user, words, now) -> None:
    """Повторение со сроком позже «сейчас», но до конца суток, попадает в план дня.

    План дня считает слова до конца местных суток
    (:func:`services.slots.local_day_end_utc`), поэтому кнопкой «🎲 Слово» день можно
    пройти досрочно: раньше запрос смотрел на «сейчас» и отдавал только
    просроченные слова, а слова с вечерним сроком ждали расписания.
    """
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(hours=5),
        last_reviewed_at=now - timedelta(days=1),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )

    selected = await word_selector.select_plan_word(session, user, now)

    assert selected is not None
    assert selected.word.word == "apple"
    assert selected.user_word is not None


async def test_select_plan_word_ignores_review_due_tomorrow(session, user, words, now) -> None:
    """Повторение со сроком на завтра в план дня не попадает: его ждёт расписание."""
    await repository.set_pos_filter(session, user, {config.POS_NOUN})
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(days=1),
        last_reviewed_at=now - timedelta(days=1),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )

    assert await word_selector.select_plan_word(session, user, now) is None


async def test_select_repeat_word_returns_answered_today_word(session, user, words, now) -> None:
    """Слово дня, отвеченное сегодня, можно пройти заново.

    План дня выполнен (слово ушло в будущее), но карточка сегодня уже была и ответ
    получен — повторный проход отдаёт это слово.
    """
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(days=1),
        last_reviewed_at=now - timedelta(minutes=5),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )
    delivery = await show_word(session, user, words[0].id, now, hours_ago=1)
    await repository.mark_delivery_answered(
        session, delivery, config.ANSWER_KNOW, answered_at=now - timedelta(minutes=5)
    )

    selected = await word_selector.select_repeat_word(session, user, now)

    assert selected is not None
    assert selected.word.word == "apple"


async def test_select_repeat_word_skips_hanging_and_sent_twice(session, user, words, now) -> None:
    """Висящая карточка и слово, приходившее дважды, в повтор дня не попадают."""
    await put_in_learning(session, user.id, words[0].id, now, hours_ago=1)
    await show_word(session, user, words[0].id, now)  # карточка висит без ответа
    await put_in_learning(session, user.id, words[1].id, now, hours_ago=2)
    first = await show_word(session, user, words[1].id, now, hours_ago=2)
    await repository.mark_delivery_answered(
        session, first, config.ANSWER_KNOW, answered_at=now - timedelta(hours=2)
    )
    second = await show_word(session, user, words[1].id, now, hours_ago=1)
    await repository.mark_delivery_answered(
        session, second, config.ANSWER_KNOW, answered_at=now - timedelta(minutes=30)
    )

    assert await word_selector.select_repeat_word(session, user, now) is None


# ---------------------------------------------------------------------------
# Пустая очередь: приходили ли слова сегодня
# ---------------------------------------------------------------------------
async def test_words_shown_today_true_after_delivery(session, user, words, now) -> None:
    """После сегодняшней отправки ручной запрос объясняет пустую очередь планом дня.

    Слова показывали сегодня — значит причина пустого ``/word`` именно в этом, и бот
    говорит о выполненном плане, а не о фильтре по части речи или лимите
    (:func:`handlers.common.no_word_text`).
    """
    await show_word(session, user, words[0].id, now)

    assert await word_selector.words_shown_today(session, user, now) is True


async def test_words_shown_today_false_without_deliveries(session, user, words, now) -> None:
    """Карточка прошлых суток — сегодняшних показов нет."""
    await show_word(session, user, words[0].id, now, hours_ago=30)

    assert await word_selector.words_shown_today(session, user, now) is False

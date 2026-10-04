"""Тесты репозитория: пользователи, слова, прогресс SRS, журнал, статистика."""

from __future__ import annotations

from datetime import datetime, timedelta

import config
from db import repository
from db.models import UserWord


async def test_create_user_fills_defaults(session) -> None:
    """Новый пользователь получает настройки из ``config`` и пустые поля карточки."""
    user = await repository.create_user(session, telegram_id=555)

    assert user.id is not None
    assert user.timezone == config.DEFAULT_TIMEZONE
    assert user.words_per_day == config.WORDS_PER_DAY_DEFAULT
    assert user.learning_limit == config.LEARNING_LIMIT_DEFAULT
    assert user.learning_limit_enabled is True
    assert (user.window_start, user.window_end) == (
        config.WINDOW_START_DEFAULT,
        config.WINDOW_END_DEFAULT,
    )
    assert user.reveal_mode == config.REVEAL_MODE_ON_BUTTON
    assert user.paused is False
    assert user.pos_filter_values == []

    settings = await repository.get_or_create_user_settings(session, user)
    assert settings.enabled_fields() == [
        config.FIELD_DEFINITION,
        config.FIELD_EXAMPLE,
        config.FIELD_SYNONYMS,
        config.FIELD_ANTONYMS,
        config.FIELD_COLLOCATIONS,
    ]


async def test_get_or_create_user_settings_is_idempotent(session, user) -> None:
    """Повторный вызов не создаёт вторую строку настроек."""
    first = await repository.get_or_create_user_settings(session, user)
    second = await repository.get_or_create_user_settings(session, user)

    assert first.id == second.id


async def test_update_user_and_settings(session, user) -> None:
    """Поля пользователя и флаги карточки обновляются поштучно."""
    await repository.update_user(session, user, paused=True, words_per_day=5)
    assert user.paused is True
    assert user.words_per_day == 5

    settings = await repository.get_or_create_user_settings(session, user)
    await repository.update_user_settings(session, settings, show_pos=True, show_example=False)
    assert settings.show_pos is True
    assert settings.show_example is False


async def test_set_pos_filter_round_trip(session, user) -> None:
    """Фильтр по части речи хранится в ``users.pos_filter`` как JSON."""
    await repository.set_pos_filter(session, user, [config.POS_NOUN, config.POS_VERB])
    assert sorted(user.pos_filter_values) == [config.POS_NOUN, config.POS_VERB]

    await repository.set_pos_filter(session, user, [])
    assert user.pos_filter_values == []


async def test_list_users_only_active(session, user) -> None:
    """``only_active`` скрывает пользователей на паузе."""
    other = await repository.create_user(session, telegram_id=2002)
    await repository.update_user(session, other, paused=True)

    assert [item.telegram_id for item in await repository.list_users(session)] == [1001, 2002]
    active = await repository.list_users(session, only_active=True)
    assert [item.telegram_id for item in active] == [1001]


async def test_upsert_words_is_idempotent(session, words, word_rows) -> None:
    """Повторный импорт тех же слов не создаёт дубликатов."""
    assert [word.word for word in words] == ["apple", "quickly", "curious"]
    assert await repository.count_words(session) == 3

    again = await repository.upsert_words(session, word_rows)
    assert (again.imported, again.skipped) == (0, 3)
    assert await repository.count_words(session) == 3


async def test_get_word_by_id(session, words) -> None:
    """Слово достаётся по идентификатору, отсутствующее — ``None``."""
    apple = words[0]
    found = await repository.get_word_by_id(session, apple.id)
    assert found is not None and found.word == "apple"
    assert await repository.get_word_by_id(session, 10_000) is None


async def test_fetch_new_words_skips_words_with_progress(session, user, words) -> None:
    """Слова, по которым уже есть прогресс, не попадают в очередь «новых»."""
    await repository.get_or_create_user_word(session, user.id, words[0].id)

    fresh = await repository.fetch_new_words(session, user, 10)
    assert {word.word for word in fresh} == {"quickly", "curious"}


async def test_fetch_new_words_respects_pos_filter(session, user, words) -> None:
    """Фильтр по части речи ограничивает выдачу."""
    await repository.set_pos_filter(session, user, [config.POS_NOUN])

    fresh = await repository.fetch_new_words(session, user, 10, user.pos_filter_values)
    assert [word.word for word in fresh] == ["apple"]


async def test_fetch_new_words_limit(session, user, words) -> None:
    """Лимит выдаёт только одно слово за слот (какое именно — решает random)."""
    fresh = await repository.fetch_new_words(session, user, 1)

    assert len(fresh) == 1
    assert fresh[0].word in {word.word for word in words}


async def test_fetch_new_words_prefers_never_sent(session, user, words) -> None:
    """Сначала идут слова, которых в чате ещё не было; отправленные — в конце."""
    await repository.create_delivery(session, user.id, words[0].id)

    fresh = await repository.fetch_new_words(session, user, 10)

    assert fresh[-1].word == "apple"
    assert {word.word for word in fresh[:-1]} == {"quickly", "curious"}


async def test_fetch_new_words_is_random(session, user, words) -> None:
    """Новые слова приходят в случайном порядке, а не подряд по словарю.

    Регрессия: выборка шла ``ORDER BY Word.id``, поэтому «новые» слова выдавались
    по алфавиту загрузки. Проверка статистическая: тридцать выборок одного слова
    из трёх кандидатов не могут совпасть все (вероятность порядка 1e-14).
    """
    draws = [
        (await repository.fetch_new_words(session, user, 1))[0].word for _ in range(30)
    ]

    assert len(set(draws)) > 1


async def test_count_learning_user_words(session, user, words, now) -> None:
    """В «изучении» считаются только слова со статусом ``learning``."""
    assert await repository.count_learning_user_words(session, user) == 0

    learning = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        learning,
        stage=2,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(days=3),
        last_reviewed_at=now,
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )
    learned = await repository.get_or_create_user_word(session, user.id, words[1].id)
    await repository.save_user_word(
        session,
        learned,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=now + timedelta(days=90),
        last_reviewed_at=now,
        times_correct=6,
        times_wrong=0,
        is_refresh=False,
    )
    # Слово без ответа (статус ``new``) лимит тоже не занимает.
    await repository.get_or_create_user_word(session, user.id, words[2].id)

    assert await repository.count_learning_user_words(session, user) == 1


async def test_fetch_due_user_words_by_date(session, user, words, now) -> None:
    """В очередь повторений попадают слова с наступившей датой и статусом learning."""
    due = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        due,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=now - timedelta(minutes=30),
        last_reviewed_at=now - timedelta(days=1),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )

    later = await repository.get_or_create_user_word(session, user.id, words[1].id)
    await repository.save_user_word(
        session,
        later,
        stage=2,
        status=config.STATUS_LEARNING,
        next_review_at=now + timedelta(days=3),
        last_reviewed_at=now,
        times_correct=2,
        times_wrong=0,
        is_refresh=False,
    )

    found = await repository.fetch_due_user_words(session, user, now, 10)
    assert [item.word.word for item in found] == ["apple"]


async def test_due_queue_skips_learned_and_filtered(session, user, words, now) -> None:
    """Выученные слова и слова «не своей» части речи в основную очередь не идут."""
    learned = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        learned,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=now - timedelta(days=400),
        last_reviewed_at=now - timedelta(days=400),
        times_correct=6,
        times_wrong=0,
        is_refresh=False,
    )
    adverb = await repository.get_or_create_user_word(session, user.id, words[1].id)
    await repository.save_user_word(
        session,
        adverb,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=now - timedelta(minutes=5),
        last_reviewed_at=now - timedelta(days=1),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )

    # Выученное слово в основную очередь не идёт, «созревшее» наречие — попадает.
    due = await repository.fetch_due_user_words(session, user, now, 10)
    assert [item.word.word for item in due] == ["quickly"]

    # Явный фильтр по части речи отсекает наречие.
    noun_only = await repository.fetch_due_user_words(
        session, user, now, 10, [config.POS_NOUN]
    )
    assert [item.word.word for item in noun_only] == []


async def test_due_queue_skips_words_shown_today(session, user, words, now) -> None:
    """``not_shown_since`` убирает слова, которые сегодня уже показывали.

    Регрессия: слова с наступившим сроком повторения выбирались снова и снова, а
    новые слова из-за этого не приходили вообще.
    """
    day_start = now - timedelta(hours=12)
    for word, reviewed_days_ago in zip(words, (3, 2, 1), strict=True):
        progress = await repository.get_or_create_user_word(session, user.id, word.id)
        await repository.save_user_word(
            session,
            progress,
            stage=1,
            status=config.STATUS_LEARNING,
            next_review_at=now - timedelta(hours=3),
            last_reviewed_at=now - timedelta(days=reviewed_days_ago),
            times_correct=1,
            times_wrong=0,
            is_refresh=False,
        )
    # apple показали час назад, quickly — позавчера, curious не отправляли.
    await repository.create_delivery(
        session, user.id, words[0].id, sent_at=now - timedelta(hours=1)
    )
    await repository.create_delivery(
        session, user.id, words[1].id, sent_at=now - timedelta(hours=30)
    )

    everything = await repository.fetch_due_user_words(session, user, now, 10)
    assert {item.word.word for item in everything} == {"apple", "quickly", "curious"}

    not_today = await repository.fetch_due_user_words(
        session, user, now, 10, not_shown_since=day_start
    )
    assert {item.word.word for item in not_today} == {"quickly", "curious"}


async def test_due_queue_prefers_longest_waiting(session, user, words, now) -> None:
    """При одинаковом сроке первым идёт слово, которое дольше ждало показа.

    Так слово, на которое только что ответили «не знаю» (этап 0 — срок наступил
    сразу), не вытесняет остальную очередь.
    """
    for word, reviewed_hours_ago in zip(words, (1, 24, 48), strict=True):
        progress = await repository.get_or_create_user_word(session, user.id, word.id)
        await repository.save_user_word(
            session,
            progress,
            stage=0,
            status=config.STATUS_LEARNING,
            next_review_at=now - timedelta(hours=1),
            last_reviewed_at=now - timedelta(hours=reviewed_hours_ago),
            times_correct=0,
            times_wrong=1,
            is_refresh=False,
        )

    found = await repository.fetch_due_user_words(session, user, now, 10)

    assert [item.word.word for item in found] == ["curious", "quickly", "apple"]


async def test_shown_word_ids_can_be_limited_by_date(session, user, words, now) -> None:
    """Показанные сегодня слова исключаются, вчерашняя карточка — уже нет.

    Иначе слово, чью карточку пользователь не закрыл накануне, выпадало из
    очереди ручного запроса ``/word`` навсегда, а слово, на которое только что
    ответили «не знаю», этим же запросом возвращалось в чат.
    """
    await repository.create_delivery(
        session, user.id, words[0].id, sent_at=now - timedelta(hours=30)
    )
    await repository.create_delivery(
        session, user.id, words[1].id, sent_at=now - timedelta(hours=1)
    )
    answered = await repository.create_delivery(
        session, user.id, words[2].id, sent_at=now - timedelta(hours=1)
    )
    await repository.mark_delivery_answered(
        session, answered, config.ANSWER_KNOW, answered_at=now
    )

    assert set(await repository.fetch_shown_word_ids_since(session, user)) == {
        words[0].id,
        words[1].id,
        words[2].id,
    }
    assert set(
        await repository.fetch_shown_word_ids_since(
            session, user, since=now - timedelta(hours=12)
        )
    ) == {words[1].id, words[2].id}


async def test_delivery_log_stats_reflect_journal_size(session, user, words, now) -> None:
    """Число записей и максимальный id журнала — по ним видно чужую базу.

    Если в кнопке ``delivery_id`` больше этого максимума, бот работает не на той
    базе, из которой отправлялась карточка.
    """
    empty = await repository.get_delivery_log_stats(session)
    assert (empty.count, empty.max_id) == (0, 0)

    first = await repository.create_delivery(session, user.id, words[0].id, sent_at=now)
    last = await repository.create_delivery(session, user.id, words[1].id, sent_at=now)
    await repository.mark_delivery_answered(
        session, last, config.ANSWER_KNOW, answered_at=now
    )

    stats = await repository.get_delivery_log_stats(session)
    assert stats.count == 2
    assert stats.max_id == last.id
    assert first.id < last.id


async def test_refresh_queue_and_marking(session, user, words, now) -> None:
    """Срок освежения выученного слова и флаг ``is_refresh``."""
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=now - timedelta(days=1),
        last_reviewed_at=now - timedelta(days=91),
        times_correct=6,
        times_wrong=0,
        is_refresh=False,
    )

    candidates = await repository.fetch_refresh_user_words(session, user, now, 5)
    assert [item.word.word for item in candidates] == ["apple"]

    await repository.mark_user_word_for_refresh(session, candidates[0])
    assert candidates[0].is_refresh is True


# ---------------------------------------------------------------------------
# План дня: подсчёт повторений «на сегодня» и начатых сегодня слов
# ---------------------------------------------------------------------------
async def test_count_pending_review_user_words_uses_until_boundary(
    session, user, words, now
) -> None:
    """Считаются повторения со сроком не позже ``until``, свежие и просроченные."""
    for word, due_at in zip(
        words, (now - timedelta(days=1), now + timedelta(hours=1), now), strict=True
    ):
        progress = await repository.get_or_create_user_word(session, user.id, word.id)
        await repository.save_user_word(
            session,
            progress,
            stage=1,
            status=config.STATUS_LEARNING,
            next_review_at=due_at,
            last_reviewed_at=now - timedelta(days=2),
            times_correct=1,
            times_wrong=0,
            is_refresh=False,
        )

    # Конец сегодняшнего дня: сроки «сейчас» и «через час» тоже внутри.
    assert (
        await repository.count_pending_review_user_words(
            session, user, now + timedelta(hours=2)
        )
        == 3
    )
    # Граница «сейчас»: просроченное и сегодняшнее слова попадают, будущее — нет.
    assert await repository.count_pending_review_user_words(session, user, now) == 2


async def test_count_pending_review_user_words_skips_learned_and_shown_today(
    session, user, words, now
) -> None:
    """Выученные и освежение не считаются; показанное сегодня — только с ответом."""
    day_start = now - timedelta(hours=12)
    learned = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        learned,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=now - timedelta(days=1),
        last_reviewed_at=now - timedelta(days=90),
        times_correct=6,
        times_wrong=0,
        is_refresh=True,
    )
    due = await repository.get_or_create_user_word(session, user.id, words[1].id)
    await repository.save_user_word(
        session,
        due,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=now - timedelta(hours=1),
        last_reviewed_at=now - timedelta(days=1),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )
    # Слово показали сегодня и ответа не дождались — второй раз его не планируем.
    await repository.create_delivery(
        session, user.id, words[1].id, sent_at=now - timedelta(hours=1)
    )

    until = now + timedelta(hours=2)
    assert await repository.count_pending_review_user_words(session, user, until) == 1
    assert (
        await repository.count_pending_review_user_words(
            session, user, until, not_shown_since=day_start
        )
        == 0
    )
    # Фильтр по части речи: quickly — наречие, под фильтр «существительные» не идёт.
    assert (
        await repository.count_pending_review_user_words(
            session, user, until, pos_values=[config.POS_NOUN]
        )
        == 0
    )


async def test_count_pending_review_user_words_keeps_word_reset_today(
    session, user, words, now
) -> None:
    """Слово, сброшенное «не знаю» сегодня, снова ждёт показа — вопреки фильтру."""
    day_start = now - timedelta(hours=12)
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=now - timedelta(hours=1),
        last_reviewed_at=now - timedelta(days=1),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )
    await repository.create_delivery(
        session, user.id, words[0].id, sent_at=now - timedelta(hours=2)
    )
    # Ответ «не знаю»: этап 0, срок повторения наступил сразу.
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_FIRST_STAGE,
        status=config.STATUS_LEARNING,
        next_review_at=now,
        last_reviewed_at=now,
        times_correct=1,
        times_wrong=1,
        is_refresh=False,
    )

    assert (
        await repository.count_pending_review_user_words(
            session, user, now + timedelta(hours=2), not_shown_since=day_start
        )
        == 1
    )


async def test_count_started_words_since_counts_first_deliveries(
    session, user, words, now
) -> None:
    """Слово расходует план дня один раз — в день первой отправки."""
    await repository.create_delivery(
        session, user.id, words[0].id, sent_at=now - timedelta(hours=2)
    )
    # Повторная отправка того же слова план дня не расходует.
    await repository.create_delivery(
        session, user.id, words[0].id, sent_at=now - timedelta(hours=1)
    )
    await repository.create_delivery(
        session, user.id, words[1].id, sent_at=now - timedelta(days=1)
    )

    assert (
        await repository.count_started_words_since(session, user, now - timedelta(hours=12)) == 1
    )
    assert await repository.count_started_words_since(session, user, now - timedelta(days=2)) == 2


# ---------------------------------------------------------------------------
# Пакетный перевод словаря: выборка слов, которым нужен русский текст
# ---------------------------------------------------------------------------
async def test_get_words_without_translations_skips_translated(session, words) -> None:
    """Полностью переведённое слово из выборки уходит, частично переведённое — остаётся."""
    await repository.save_word_translations(
        session, words[0], russian_definition="круглый плод", russian_example="Она ела яблоко."
    )
    await repository.save_word_translations(session, words[1], russian_definition="быстро")

    pending = await repository.get_words_without_translations(session)

    assert [word.id for word in pending] == [words[1].id, words[2].id]


async def test_get_words_without_translations_respects_field_and_limit(session, words) -> None:
    """``field`` сужает выборку до одного поля, ``limit`` — до размера порции."""
    await repository.save_word_translations(session, words[0], russian_definition="круглый плод")

    by_definition = await repository.get_words_without_translations(session, field="definition")
    by_example = await repository.get_words_without_translations(
        session, field="example", limit=1
    )

    assert [word.id for word in by_definition] == [words[1].id, words[2].id]
    assert [word.id for word in by_example] == [words[0].id]


async def test_get_words_without_translations_prefers_learning(session, user, words) -> None:
    """``learning_first`` поднимает вперёд слово, которое уже есть в планах изучения."""
    await repository.get_or_create_user_word(session, user.id, words[2].id)

    pending = await repository.get_words_without_translations(session, learning_first=True)

    assert [word.id for word in pending] == [words[2].id, words[0].id, words[1].id]


async def test_pending_translations_counts_words_and_chars(session, words) -> None:
    """Отчёт считает слова и символы по тем строкам, которым перевод ещё нужен."""
    await repository.save_word_translations(session, words[0], russian_definition="круглый плод")

    pending = await repository.pending_translations(session)

    assert pending.words == len(words)  # у первого слова не хватает только примера
    assert pending.chars == len(words[0].example) + sum(
        len(word.definition) + len(word.example) for word in words[1:]
    )


async def test_pending_translations_is_zero_for_translated_dictionary(session, words) -> None:
    """Полностью переведённый словарь: переводить нечего."""
    for word in words:
        await repository.save_word_translations(
            session, word, russian_definition="перевод", russian_example="перевод"
        )

    pending = await repository.pending_translations(session)

    assert (pending.words, pending.chars) == (0, 0)
    assert list(await repository.get_words_without_translations(session)) == []


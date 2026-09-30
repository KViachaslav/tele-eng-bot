"""Выбор слов для отправки: повторения, новые слова и освежение.

Ежедневная рассылка состоит из одного «основного» слова на слот (это и есть
дневной лимит ``words_per_day``) и, при наличии, одного слова на освежение,
которое лимит не расходует.

Основное слово выбирается по ступеням (:func:`select_main_word`): повторения,
которых сегодня ещё не было, → новое слово → повторение того, что сегодня уже
показывали. Так очередь не зацикливается на слове, сброшенном на этап 0 ответом
«не знаю» (у него срок повторения наступает сразу).

Новые слова выбираются **случайно** (а не по порядку загрузки словаря) и только
пока слов «в изучении» меньше ``users.learning_limit``: лимит общий для рассылки
по слотам и для запроса «дай слово».
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from db import repository
from db.models import User, UserWord, Word, utcnow
from services import slots


@dataclass(slots=True)
class SelectedWord:
    """Слово, выбранное к отправке."""

    word: Word
    user_word: UserWord | None = None
    is_refresh: bool = False


async def new_words_allowed(session: AsyncSession, user: User) -> bool:
    """Не превышен ли лимит слов в изучении (``users.learning_limit``).

    Пока слов «в изучении» не меньше лимита, новые слова не показываются — ни в
    рассылке по слотам, ни по запросу «дай слово». Повторения уже начатых слов и
    освежение выученных лимитом не ограничены: пользователь продолжает работать
    с тем, что уже взял, просто не получает нового.

    Значение ``0`` в настройках означает «без ограничения».
    """
    if not user.learning_limit_enabled:
        return True
    learning = await repository.count_learning_user_words(session, user)
    return learning < user.learning_limit


async def select_main_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    exclude_word_ids: Collection[int] = (),
) -> SelectedWord | None:
    """Слово для обычной рассылки.

    Приоритет:

    1. повторения, которых сегодня ещё не показывали (``not_shown_since``) —
       слова с наступившим сроком повторения, этапы 1–5;
    2. новое слово (в случайном порядке) — пока не достигнут лимит
       ``users.learning_limit``;
    3. оставшиеся повторения, то есть слова, которые сегодня уже показывали.

    Третий пункт нужен для «не знаю»: ответ сбрасывает слово на этап 0, и его срок
    повторения наступает сразу же. Без отделения таких слов от первых двух пунктов
    они выбирались бы снова и снова, и новые слова не приходили бы вообще. Поэтому
    слово, показанное сегодня, возвращается только тогда, когда других вариантов не
    осталось (например, упёрлись в лимит изучения или кончился словарь).

    Выученные слова здесь не участвуют — они приходят отдельной очередью освежения
    и не съедают дневной лимит.

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    """
    pos_values = user.pos_filter_values
    day_start = slots.local_day_start_utc(user, now)

    due_words = await repository.fetch_due_user_words(
        session,
        user,
        now,
        1,
        pos_values,
        exclude_word_ids=exclude_word_ids,
        not_shown_since=day_start,
    )
    if due_words:
        user_word = due_words[0]
        return SelectedWord(word=user_word.word, user_word=user_word)

    if await new_words_allowed(session, user):
        fresh_words = await repository.fetch_new_words(
            session, user, 1, pos_values, exclude_word_ids=exclude_word_ids
        )
        if fresh_words:
            return SelectedWord(word=fresh_words[0])
    else:
        logger.debug(
            "Пользователь {}: лимит слов в изучении ({}) достигнут — новых слов не показываю",
            user.telegram_id,
            user.learning_limit,
        )

    # Всё, что осталось, — повторение слова, которое сегодня уже показывали.
    repeated_words = await repository.fetch_due_user_words(
        session, user, now, 1, pos_values, exclude_word_ids=exclude_word_ids
    )
    if repeated_words:
        user_word = repeated_words[0]
        logger.debug(
            "Пользователь {}: других слов нет — повторяю «{}»",
            user.telegram_id,
            user_word.word.word,
        )
        return SelectedWord(word=user_word.word, user_word=user_word)

    return None


async def select_refresh_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    exclude_word_ids: Collection[int] = (),
) -> SelectedWord | None:
    """Выученное слово, у которого подошёл срок освежения (этап 6, 90 дней).

    Отправка помечается флагом ``is_refresh`` — при ответе «не знаю» слово
    вернётся в основной поток и снова станет изучаемым.

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    """
    candidates = await repository.fetch_refresh_user_words(
        session, user, now, 1, user.pos_filter_values, exclude_word_ids=exclude_word_ids
    )
    if not candidates:
        return None

    user_word = candidates[0]
    await repository.mark_user_word_for_refresh(session, user_word)
    return SelectedWord(word=user_word.word, user_word=user_word, is_refresh=True)


async def on_demand_exclusions(
    session: AsyncSession, user: User, now: datetime | None = None
) -> set[int]:
    """Слова, которые не стоит присылать снова по ручному запросу «дай слово».

    В набор попадают:

    * слова, карточки которых висят в чате без ответа **с сегодняшнего дня**:
      второе такое же слово только запутает. Карточка, отправленная вчера, уже не
      «висит» — иначе слово выпало бы из очереди ``/word`` навсегда, как только
      пользователь забудет на неё ответить (см.
      :func:`services.slots.local_day_start_utc`);
    * слово из самой последней отправки — после ответа «не знаю» его срок
      повторения обнуляется (этап 0 = «в очередь на сегодня»), и без этого
      исключения ``/word`` и кнопка «🎲 Слово» возвращали то же слово.
    """
    moment = now or utcnow()
    day_start = slots.local_day_start_utc(user, moment)
    skipped = set(await repository.fetch_open_delivery_word_ids(session, user, since=day_start))
    last_word_id = await repository.fetch_last_delivered_word_id(session, user)
    if last_word_id is not None:
        skipped.add(last_word_id)
    return skipped

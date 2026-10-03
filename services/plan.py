"""План дня: сколько слов пользователь ещё должен получить сегодня.

Слова дня — это не только ``users.words_per_day``: к ним добавляются повторения,
срок которых наступает в течение сегодняшних суток (в том числе слова, сброшенные
на этап 0 ответом «не знаю»). Планировщик делит оставшееся время окна на это
число (:func:`services.slots.next_slot`), поэтому график подстраивается сам:
ответ «знаю» убирает слово из плана и оставшиеся слова приходят реже, а «не знаю»
возвращает слово в план. Отправленное слово (карточка ждёт ответа) план тоже
покидает: показывать его второй раз не нужно, поэтому каждая отправка — по
расписанию или кнопкой «🎲 Слово» — уменьшает план дня, а интервал до следующего
слова растёт.

Модуль — «мост» между БД и математикой расписания: он один знает, какие слова
ещё впереди, и отдаёт планировщику число (см. ``tests/test_plan.py``).
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from db import repository
from db.models import User
from services import slots
from services.word_selector import new_words_allowed


async def words_left_today(session: AsyncSession, user: User, now: datetime) -> int:
    """Сколько слов пользователь ещё должен получить сегодня.

    Ответ — сумма двух частей:

    * новые слова, которые ещё осталось начать сегодня
      (:func:`planned_words_left`);
    * повторения, срок которых наступает в течение сегодняшних суток
      пользователя: просроченные, сегодняшние и сброшенные на этап 0 ответом
      «не знаю» (:func:`db.repository.count_pending_review_user_words`).

    В план входят только слова, которые ещё предстоит отправить: если карточка
    слова уже висит в чате без ответа, слово план не занимает — иначе он не
    убывал бы от отправок, и интервал до следующего слова не рос бы.

    ``0`` означает, что план дня выполнен: планировщик переносит следующее слово
    на открытие окна (:func:`services.slots.next_slot`).
    """
    day_start = slots.local_day_start_utc(user, now)
    day_end = slots.local_day_end_utc(user, now)
    planned = await planned_words_left(session, user, day_start)
    due = await repository.count_pending_review_user_words(
        session,
        user,
        day_end,
        not_shown_since=day_start,
        pos_values=user.pos_filter_values,
    )
    return planned + due


async def planned_words_left(session: AsyncSession, user: User, day_start: datetime) -> int:
    """Сколько новых слов ещё запланировано на сегодня.

    Из ``users.words_per_day`` вычитаются слова, начатые сегодня
    (:func:`db.repository.count_started_words_since`): начатое слово — это и есть
    «отданный» пункт дневного плана, поэтому запрос «дай слово» уменьшает
    оставшийся план так же, как слово по расписанию.

    План равен нулю, если новые слова сейчас всё равно не показываются: достигнут
    лимит слов в изучении (``users.learning_limit``) или в словаре не осталось
    неизученных слов. Тогда день состоит только из повторений.
    """
    started = await repository.count_started_words_since(session, user, day_start)
    left = max(0, user.words_per_day - started)
    if left == 0:
        return 0
    if not await new_words_allowed(session, user):
        return 0
    if not await repository.fetch_new_words(session, user, 1, user.pos_filter_values):
        return 0
    return left

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

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from db import repository
from db.models import User
from services import slots
from services.word_selector import new_words_allowed


@dataclass(slots=True)
class DayPlan:
    """Ожидаемые слова за одни местные сутки пользователя."""

    local_day: date
    new_words: int
    reviews: int

    @property
    def total(self) -> int:
        """Всего слов за сутки: новые слова плюс повторения."""
        return self.new_words + self.reviews


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


async def upcoming_plan(
    session: AsyncSession, user: User, now: datetime, days: int
) -> list[DayPlan]:
    """Прогноз слов на ближайшие дни: сегодня и ``days - 1`` следующих суток.

    Экран «📅 План слов» показывает по этому прогнозу, сколько слов придёт
    сегодня и в ближайшие дни. Отличие от :func:`words_left_today` (там план дня
    делится на остаток окна) в том, что здесь считается то, что действительно
    может прийти в каждые сутки:

    * новые слова — не больше ``users.words_per_day`` в день и только пока есть
      неизученные (см. :func:`db.repository.count_new_user_words`), поэтому запас
      словаря расходуется по дням: при трёх неизученных словах и норме «2 в день»
      прогноз — 2, 1, 0;
    * повторения — по сроку ``next_review_at``: слово попадает в те сутки, на
      которые этот срок приходится (см.
      :func:`db.repository.count_pending_review_user_words`). Просроченные слова
      считаются только в сегодняшних сутках, иначе одно и то же слово повторялось
      бы в прогнозе каждый следующий день.

    Если новые слова сейчас не показываются (достигнут лимит слов в изучении),
    новые равны нулю во все дни, а повторения остаются: с уже начатыми словами
    пользователь продолжает работать. ``days`` меньше единицы означает «только
    сегодня».
    """
    forecast: list[DayPlan] = []
    new_allowed = await new_words_allowed(session, user)
    available_new = await repository.count_new_user_words(
        session, user, user.pos_filter_values
    )
    for offset in range(max(1, days)):
        local_day = slots.local_date(user, now) + timedelta(days=offset)
        day_start, day_end = slots.local_day_bounds_utc(user, local_day)
        planned = 0
        if new_allowed and available_new > 0:
            started = await repository.count_started_words_since(session, user, day_start)
            planned = min(max(0, user.words_per_day - started), available_new)
            available_new -= planned
        if offset == 0:
            reviews = await repository.count_pending_review_user_words(
                session,
                user,
                day_end,
                not_shown_since=day_start,
                pos_values=user.pos_filter_values,
            )
        else:
            reviews = await repository.count_pending_review_user_words(
                session,
                user,
                day_end,
                since=day_start,
                pos_values=user.pos_filter_values,
            )
        forecast.append(DayPlan(local_day=local_day, new_words=planned, reviews=reviews))
    return forecast

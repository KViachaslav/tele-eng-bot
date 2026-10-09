"""Выбор слов для отправки: повторения, новые слова и освежение.

Ежедневная рассылка состоит из одного «основного» слова на слот (это и есть
дневной лимит ``words_per_day``) и, при наличии, одного слова на освежение,
которое лимит не расходует.

Основное слово выбирается по ступеням (:func:`select_main_word`) — так же, как
описано в ТЗ («Порядок очереди»): повторение с более высоким этапом важнее, этап 0
«не сегодня» идёт перед новым словом, а этап 0 «сегодня» («не знаю» вернуло слово в
очередь) — последним, чтобы одно слово не приходило подряд
(:func:`db.repository.queue_rank`). Слово попадает в очередь, только если карточки
сегодня ещё не было или на неё уже ответили: так очередь не зацикливается на слове
без ответа, которое прогресс не двигает. Карточка, которая висит в чате без ответа,
второй раз не приходит (:func:`hanging_card_word_ids`) — лучше пропустить слот, чем
слать одно и то же слово, пока не закроется окно рассылки.

Граница «пора показать» у расписания и у ручного запроса одна и та же — **конец
местных суток** пользователя (:func:`select_main_word`, :func:`select_plan_word`).
Раньше расписание сравнивало срок со «сейчас», а план дня
(:func:`services.plan.words_left_today`) — с концом суток, и слово со сроком 18:08
при окне до 18:00 попадало в план, но не отправлялось ни одним слотом: план не
убывал, интервал падал до минимума, и день заканчивался без слов (в базе 09.10.2026
«план дня 2», «отправлено 0»). Теперь слово из плана дня может уйти в любой
оставшийся слот.

Ручной запрос «дай слово» (``/word`` и кнопка «🎲 Слово») идёт **по плану дня**
(:func:`select_plan_word`): поэтому день можно пройти досрочно, а расписание после
этого уже не пришлёт пройденное. Когда в плане ничего не осталось, очередь отдаёт
слова, которые сегодня уже приходили и получили ответ
(:func:`select_repeat_word`), — «пройти день заново». Висящая карточка в повторный
проход не попадает, а слово, исчерпавшее лимит показов за сутки
(``config.ON_DEMAND_MAX_SENDS_PER_DAY``), не попадает по лимиту
(:func:`on_demand_exclusions`): план дня лимитом не ограничен (слово уже приходило),
а вот крутить одно слово бесконечно нельзя (см. :mod:`services.delivery`).

Новые слова выбираются **случайно** (а не по порядку загрузки словаря) и только
пока слов «в изучении» меньше ``users.learning_limit`` и не израсходован дневной
план ``users.words_per_day``: оба ограничения общие для рассылки по слотам и для
запроса «дай слово».
"""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
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


async def new_words_left_today(session: AsyncSession, user: User, now: datetime) -> bool:
    """Остались ли на сегодня новые слова по дневному плану.

    Пункт дневного плана расходует первая отправка слова, поэтому после
    ``users.words_per_day`` начатых сегодня слов новых больше не показывают — ни
    по расписанию, ни по запросу «дай слово». Иначе кнопка «🎲 Слово» выдавала бы
    слова сверх дневного плана: план дня не уменьшался бы от такой отправки, и
    интервал до следующего слова не увеличивался, хотя слов на сегодня уже меньше
    не становится (см. :mod:`services.plan`).

    Повторения уже начатых слов и освежение дневным планом не ограничены:
    пользователь продолжает работать с тем, что взял.
    """
    day_start = slots.local_day_start_utc(user, now)
    started = await repository.count_started_words_since(session, user, day_start)
    return started < user.words_per_day


async def select_due_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    due_until: datetime,
    exclude_word_ids: Collection[int] = (),
) -> SelectedWord | None:
    """Первое слово очереди со сроком повторения не позже ``due_until``.

    Порядок — по ступеням (:func:`db.repository.queue_rank`): этапы 5…1 → этап 0
    «не сегодня» → этап 0 «сегодня». Выученные слова (этап 6) сюда не попадают: они
    приходят отдельным сообщением (:func:`select_refresh_word`) и дневного плана не
    занимают.

    «Ждёт отправки» означает, что карточки сегодня ещё не было или на неё уже
    ответили (:func:`db.repository.waiting_for_repeat_condition`). Карточка,
    которая висит в чате без ответа, второй раз не приходит: отправка прогресс не
    двигает, порядок очереди не меняется, поэтому выбор каждый раз упирался бы в
    самое просроченное слово — в базе 04.10.2026 «seat» приходило 11 раз подряд,
    пока не закрылось окно рассылки.

    :param due_until: граница «пора показать»: и расписание, и запрос «дай слово»
        передают конец местных суток пользователя
        (:func:`services.slots.local_day_end_utc`). Иначе слово со сроком позже
        окна рассылки попадало бы в план дня, но не отправлялось ни одним слотом
        (см. :func:`select_main_word`).
    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    """
    due_words = await repository.fetch_queue_words(
        session,
        user,
        due_until=due_until,
        limit=1,
        day_start=slots.local_day_start_utc(user, now),
        pos_values=user.pos_filter_values,
        exclude_word_ids=exclude_word_ids,
    )
    if not due_words:
        return None
    user_word = due_words[0]
    return SelectedWord(word=user_word.word, user_word=user_word)


async def select_new_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    exclude_word_ids: Collection[int] = (),
) -> SelectedWord | None:
    """Новое слово (случайное из словаря), если это разрешено настройками.

    Новые слова показываются, только пока слов «в изучении» меньше
    ``users.learning_limit`` (:func:`new_words_allowed`) и не израсходован дневной
    план ``users.words_per_day`` (:func:`new_words_left_today`). Оба ограничения
    общие для рассылки по слотам и для запроса «дай слово».

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    """
    if not await new_words_allowed(session, user):
        logger.debug(
            "Пользователь {}: лимит слов в изучении ({}) достигнут — новых слов не показываю",
            user.telegram_id,
            user.learning_limit,
        )
        return None
    if not await new_words_left_today(session, user, now):
        logger.debug(
            "Пользователь {}: дневной план новых слов ({}) выполнен — новых слов не показываю",
            user.telegram_id,
            user.words_per_day,
        )
        return None
    fresh_words = await repository.fetch_new_words(
        session, user, 1, user.pos_filter_values, exclude_word_ids=exclude_word_ids
    )
    if not fresh_words:
        return None
    return SelectedWord(word=fresh_words[0])


def is_todays_repeat(user_word: UserWord | None, day_start: datetime) -> bool:
    """Слово этапа 0, по которому сегодня уже отвечали, — последняя ступень очереди.

    Ответ «не знаю» возвращает слово на этап 0, а срок повторения — на «сразу»
    (:func:`services.srs.apply_answer`), поэтому без отдельной ступени очередь
    отдавала бы это же слово в следующий слот вместо остальных слов. Сначала
    отдаются все прочие ступени и новое слово, и только когда других слов нет — это
    (см. :func:`select_main_word`, :data:`config.QUEUE_RANK_STAGE_ZERO_TODAY`).
    """
    return (
        user_word is not None
        and user_word.stage == config.SRS_FIRST_STAGE
        and user_word.last_reviewed_at is not None
        and user_word.last_reviewed_at >= day_start
    )


async def select_main_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    exclude_word_ids: Collection[int] = (),
) -> SelectedWord | None:
    """Слово для обычной рассылки (очередь слота).

    Очередь идёт до **конца местных суток** пользователя, а не до «сейчас»: иначе
    слово со сроком 18:08 при окне до 18:00 попадало в план дня, но не отправлялось
    ни одним слотом — план не убывал, интервал падал до минимума, и день
    заканчивался без слов (в базе 09.10.2026 «план дня 2», «отправлено 0»). Так же
    считает план дня (:func:`services.plan.words_left_today`), поэтому слово из плана
    дня может уйти в любой оставшийся слот.

    Ступени — как в ТЗ («Порядок очереди»):

    1. повторения по этапам: 5 → 4 → … → 1, затем этап 0 «не сегодня»
       (:func:`select_due_word`, :func:`db.repository.queue_rank`);
    2. новое слово — пока не достигнут лимит ``users.learning_limit`` и не
       израсходован дневной план ``users.words_per_day`` (:func:`select_new_word`);
    3. этап 0 «сегодня»: слово, по которому сегодня ответили «не знаю» — оно снова в
       плане дня, но подряд приходить не должно, поэтому отдаётся последним
       (:func:`is_todays_repeat`).

    Выученные слова здесь не участвуют — они приходят отдельной очередью освежения
    и не съедают дневной лимит.

    Ручной запрос «дай слово» идёт этой же очередью (:func:`select_plan_word`), а
    показанные слова повторяет отдельным проходом (:func:`select_repeat_word`).

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    """
    day_start = slots.local_day_start_utc(user, now)
    day_end = slots.local_day_end_utc(user, now)
    due = await select_due_word(
        session, user, now, due_until=day_end, exclude_word_ids=exclude_word_ids
    )
    if due is not None and not is_todays_repeat(due.user_word, day_start):
        return due

    fresh = await select_new_word(session, user, now, exclude_word_ids=exclude_word_ids)
    if fresh is not None:
        return fresh

    # Остался только этап 0 «сегодня»: в плане дня слово есть, поэтому отдаём его —
    # пусть и последним (см. docstring).
    logger.debug(
        "Пользователь {}: других слов нет — повторяю «{}» (этап 0, отвечено сегодня)",
        user.telegram_id,
        due.word.word if due is not None else "",
    )
    return due


async def select_plan_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    exclude_word_ids: Collection[int] = (),
) -> SelectedWord | None:
    """Слово из плана дня — ровно то же, что отдаёт расписание.

    Логика общая с :func:`select_main_word`: очередь идёт по ступеням до конца
    местных суток пользователя (граница та же, что у плана дня
    :func:`services.plan.words_left_today`). Поэтому кнопкой «🎲 Слово» день можно
    пройти досрочно: отданное слово расходует пункт плана, интервал до следующего
    слова растёт, а расписание после этого уже не пришлёт пройденное.

    Когда план дня выполнен, ручной запрос повторяет слова дня отдельным проходом
    (:func:`select_repeat_word`), а не крутит только что отвеченное слово.

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    """
    return await select_main_word(session, user, now, exclude_word_ids=exclude_word_ids)


async def select_repeat_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    exclude_word_ids: Collection[int] = (),
) -> SelectedWord | None:
    """Слово дня, которое уже приходило и отвечено, — повторный проход по дню.

    Ручной запрос отдаёт эти слова, когда план дня выполнен: так день можно пройти
    заново, не дожидаясь завтрашних сроков повторения. Подходят слова, которые
    сегодня приходили и получили ответ, а лимит показов за сутки
    (``config.ON_DEMAND_MAX_SENDS_PER_DAY``) ещё не исчерпан
    (:func:`db.repository.fetch_repeat_user_words`); висящую карточку и исчерпавшее
    лимит слово отсекает ещё и набор исключений (:func:`on_demand_exclusions`).

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    """
    day_start = slots.local_day_start_utc(user, now)
    repeats = await repository.fetch_repeat_user_words(
        session,
        user,
        since=day_start,
        limit=1,
        pos_values=user.pos_filter_values,
        exclude_word_ids=exclude_word_ids,
    )
    if not repeats:
        return None
    user_word = repeats[0]
    logger.debug(
        "Пользователь {}: план дня выполнен — повторяю слово дня «{}»",
        user.telegram_id,
        user_word.word.word,
    )
    return SelectedWord(word=user_word.word, user_word=user_word)


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
    """Слова, которые ручной запрос «дай слово» сейчас присылать не должен.

    В набор попадают:

    * слова, чьи карточки висят в чате без ответа с начала местных суток
      (:func:`hanging_card_word_ids`): повтор прогресс не двигает, а в чате
      оказались бы две одинаковые карточки;
    * слова, карточки которых уходили сегодня уже
      ``config.ON_DEMAND_MAX_SENDS_PER_DAY`` раз
      (:func:`db.repository.fetch_word_ids_sent_at_least`). Каждое слово дня можно
      пройти заново, но не бесконечно: ответ «не знаю» возвращает слово на этап 0
      (срок повторения наступает сразу же), поэтому без такого счётчика пара
      «нажатие → «не знаю»» ходила бы по кругу одного слова, и запрос никогда не
      сообщил бы, что план дня выполнен (см. :mod:`services.delivery`).

    Считаются отправки из любого источника — расписание и ручной запрос вместе:
    иначе слово, которое слоты присылают после каждого «не знаю», кнопка повторяла
    бы бесконечно.

    Набор нужен **повторному** проходу кнопки (:mod:`services.delivery`): лимит
    показов за сутки защищает именно «пройти день заново». Плану дня лимит не мешает
    — слово уже приходило, но пункт плана всё ещё занимает, поэтому молчать о нём
    нельзя: план перестал бы убывать от отправок, а кнопка отвечала бы «план на
    сегодня выполнен», хотя слова в нём остались. Висящие карточки из набора нужны
    обоим проходам (:func:`hanging_card_word_ids`).
    """
    moment = now or utcnow()
    day_start = slots.local_day_start_utc(user, moment)
    skipped = set(await hanging_card_word_ids(session, user, moment))
    skipped |= set(
        await repository.fetch_word_ids_sent_at_least(
            session,
            user,
            since=day_start,
            count=config.ON_DEMAND_MAX_SENDS_PER_DAY,
        )
    )
    return skipped


async def hanging_card_word_ids(
    session: AsyncSession, user: User, now: datetime | None = None
) -> set[int]:
    """Слова, чьи карточки висят в чате без ответа с начала местных суток.

    Ответа нет, поэтому отправка такого слова не двигает прогресс и не меняет
    порядок очереди: выбирай его каждый слот, и бот слал бы одно и то же слово, пока
    не закроется окно рассылки (в базе 04.10.2026 «seat» приходило 11 раз подряд).
    Поэтому очередь повторений такие слова пропускает — лучше пустой слот, чем та же
    карточка (см. :func:`select_main_word`).

    Карточка прошлых суток перед глазами уже не «висит» и очередь не блокирует:
    иначе забытая карточка отодвигала бы слово, пока на неё не ответят (см.
    :func:`services.slots.local_day_start_utc`).
    """
    moment = now or utcnow()
    day_start = slots.local_day_start_utc(user, moment)
    return set(
        await repository.fetch_unanswered_word_ids_since(session, user, since=day_start)
    )


async def words_shown_today(
    session: AsyncSession, user: User, now: datetime | None = None
) -> bool:
    """Приходило ли пользователю сегодня хоть одно слово.

    Нужно, чтобы объяснить пустую очередь ручного запроса: если сегодня показы уже
    были, причина пустого ``/word`` — выполненный план дня (слова дня уже приходили,
    а лимит показов за сутки исчерпан), а не настройки вроде фильтра по части речи
    или пустого словаря (см. :func:`handlers.common.no_word_text`).
    """
    moment = now or utcnow()
    day_start = slots.local_day_start_utc(user, moment)
    shown = await repository.fetch_shown_word_ids_since(session, user, since=day_start)
    return bool(shown)

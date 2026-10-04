"""Выбор слов для отправки: повторения, новые слова и освежение.

Ежедневная рассылка состоит из одного «основного» слова на слот (это и есть
дневной лимит ``words_per_day``) и, при наличии, одного слова на освежение,
которое лимит не расходует.

Основное слово выбирается по ступеням (:func:`select_main_word`): повторения,
которые снова ждут отправки, → новое слово → прочие повторения. Слово снова ждёт
отправки, если карточки сегодня ещё не было или на неё уже ответили: так очередь
не зацикливается на слове, сброшенном на этап 0 ответом «не знаю» (у него срок
повторения наступает сразу). Карточка, которая висит в чате без ответа, второй раз
не приходит (:func:`hanging_card_word_ids`) — лучше пропустить слот, чем слать одно
и то же слово, пока не закроется окно рассылки.

Ручной запрос «дай слово» третьей ступенью не пользуется: слова, которые уже
уходили сегодня, ему отдаёт :func:`on_demand_exclusions` — иначе одно нажатие
возвращало бы в чат только что отвеченное слово (см. :mod:`services.delivery`).

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


async def select_main_word(
    session: AsyncSession,
    user: User,
    now: datetime,
    *,
    exclude_word_ids: Collection[int] = (),
    allow_waiting_card: bool = False,
) -> SelectedWord | None:
    """Слово для обычной рассылки.

    Приоритет:

    1. повторения, которые **снова ждут отправки** (``not_shown_since``): слова с
       наступившим сроком повторения, этапы 1–5, которых сегодня ещё не
       показывали, а также те, на которые после сегодняшней карточки ответили;
    2. новое слово (в случайном порядке) — пока не достигнут лимит
       ``users.learning_limit`` и не израсходован дневной план
       ``users.words_per_day`` (:func:`new_words_left_today`);
    3. прочие повторения: слова, чьи карточки сегодня уже приходили и ответ на
       них получен.

    Третий пункт нужен для «не знаю»: ответ сбрасывает слово на этап 0, и его срок
    повторения наступает сразу же. Без отделения таких слов от первых двух пунктов
    они выбирались бы снова и снова, и новые слова не приходили бы вообще. Поэтому
    слово, чья карточка сегодня уже уходила, возвращается только тогда, когда других
    вариантов не осталось (например, упёрлись в лимит изучения или кончился
    словарь).

    Карточка без ответа не выбирается ни на одной ступени
    (:func:`hanging_card_word_ids`): отправка прогресс не двигает, порядок очереди
    не меняется, поэтому выбор каждый раз упирался бы в самое просроченное слово —
    в базе 04.10.2026 «seat» приходило 11 раз подряд, пока не закрылось окно
    рассылки. Пропущенный слот лучше повтора: план дня от такой отправки всё равно
    не убывает.

    Выученные слова здесь не участвуют — они приходят отдельной очередью освежения
    и не съедают дневной лимит.

    :param exclude_word_ids: слова, которые сейчас присылать не нужно
        (см. :func:`on_demand_exclusions`).
    :param allow_waiting_card: разрешить повтор слова, чья карточка ушла сегодня и
        осталась без ответа. Нужно только диагностике
        (:func:`has_candidate_word`): она отвечает на вопрос «есть ли слово вообще,
        если забыть про исключения».
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

    if not await new_words_allowed(session, user):
        logger.debug(
            "Пользователь {}: лимит слов в изучении ({}) достигнут — новых слов не показываю",
            user.telegram_id,
            user.learning_limit,
        )
    elif not await new_words_left_today(session, user, now):
        logger.debug(
            "Пользователь {}: дневной план новых слов ({}) выполнен — новых слов не показываю",
            user.telegram_id,
            user.words_per_day,
        )
    else:
        fresh_words = await repository.fetch_new_words(
            session, user, 1, pos_values, exclude_word_ids=exclude_word_ids
        )
        if fresh_words:
            return SelectedWord(word=fresh_words[0])

    # Всё, что осталось, — повторение слова, чья карточка сегодня уже уходила и
    # ответ на неё получен. Слова с висящей карточкой не выбираем (см. docstring).
    repeated_skip = set(exclude_word_ids)
    if not allow_waiting_card:
        repeated_skip |= await hanging_card_word_ids(session, user, now)
    repeated_words = await repository.fetch_due_user_words(
        session, user, now, 1, pos_values, exclude_word_ids=repeated_skip
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

    * все слова, карточки которых уходили **с сегодняшнего дня** — независимо от
      того, ответил на них пользователь или нет. Ответ «не знаю» обнуляет срок
      повторения (этап 0 — слово снова «на сегодня»), поэтому без этого правила
      каждое нажатие «🎲 Слово» присылало то же слово, которое только что было в
      чате. Карточка, отправленная вчера, уже не «висит» — иначе слово выпало бы
      из очереди ``/word`` навсегда, как только пользователь забудет на неё
      ответить (см. :func:`services.slots.local_day_start_utc`);
    * слово из самой последней отправки — на случай, когда она была вчера.
    """
    moment = now or utcnow()
    day_start = slots.local_day_start_utc(user, moment)
    skipped = set(await repository.fetch_shown_word_ids_since(session, user, since=day_start))
    last_word_id = await repository.fetch_last_delivered_word_id(session, user)
    if last_word_id is not None:
        skipped.add(last_word_id)
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


async def has_candidate_word(session: AsyncSession, user: User, now: datetime) -> bool:
    """Есть ли вообще слово, которое можно было бы отправить без исключений.

    Нужно, чтобы объяснить пользователю пустую очередь: ``False`` — слов нет по
    настройкам (фильтр по части речи, лимит в изучении, пустой словарь), а
    ``True`` при пустом ``/word`` означает, что всё доступное уже приходило
    сегодня (см. :func:`handlers.common.no_word_text`).

    Проверка идёт с ``allow_waiting_card=True``: вопрос не в том, пришлёт ли бот
    слово прямо сейчас, а в том, есть ли оно в очереди вообще — ручной запрос как
    раз и не повторяет сегодняшние карточки (:func:`on_demand_exclusions`).
    """
    if await select_main_word(session, user, now, allow_waiting_card=True) is not None:
        return True
    candidates = await repository.fetch_refresh_user_words(
        session, user, now, 1, user.pos_filter_values
    )
    return bool(candidates)

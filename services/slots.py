"""Математика расписания: временные окна, слоты и часовые пояса.

Модуль не обращается ни к БД, ни к Telegram — только чистые функции от настроек
пользователя и момента времени. Поэтому его легко покрывать юнит-тестами
(см. ``tests/test_slots.py``).

Интервал до следующего слова считается не на весь день вперёд, а от текущего
момента: оставшиеся минуты окна делятся на оставшиеся слова дня
(:func:`slot_interval`). Сколько слов осталось, знает :mod:`services.plan` — это
``users.words_per_day`` плюс повторения, срок которых наступает сегодня.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

import config
from db.models import User

#: Шаблон ввода времени: «9:00», «09:00», «9.00», «0900», «900».
TIME_INPUT_PATTERN = re.compile(r"^(\d{1,2})[:.\s]?(\d{2})$")

#: Коды причин, по которым окно не принимается.
WINDOW_REASON_END_BEFORE_START = "end_before_start"
WINDOW_REASON_TOO_SHORT = "too_short"


@dataclass(frozen=True, slots=True)
class WindowCheck:
    """Результат проверки временного окна."""

    reason: str | None = None
    current_minutes: int = 0
    required_minutes: int = 0

    @property
    def ok(self) -> bool:
        """Окно корректно."""
        return self.reason is None


# ---------------------------------------------------------------------------
# Разбор и форматирование ввода
# ---------------------------------------------------------------------------
def parse_time(value: str) -> time | None:
    """Разбирает время из свободного ввода; ``None`` — если формат неверный."""
    match = TIME_INPUT_PATTERN.match((value or "").strip())
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    if hours > 23 or minutes > 59:
        return None
    return time(hour=hours, minute=minutes)


def format_time(value: time) -> str:
    """Время в формате ``ЧЧ:ММ``."""
    return value.strftime(config.TIME_FORMAT)


def encode_time(value: time) -> str:
    """Компактный вид времени для callback-данных (``09:00`` -> ``0900``)."""
    return value.strftime("%H%M")


def decode_time(value: str) -> time | None:
    """Обратная операция к :func:`encode_time`."""
    if not value.isdigit() or len(value) != 4:
        return None
    return parse_time(f"{value[:2]}:{value[2:]}")


@lru_cache(maxsize=1)
def timezone_names_by_lowercase() -> dict[str, str]:
    """Справочник часовых поясов: имя в нижнем регистре -> каноническое имя."""
    return {name.casefold(): name for name in available_timezones()}


def parse_timezone(value: str) -> str | None:
    """Проверяет название часового пояса (IANA) и возвращает его канонический вид."""
    cleaned = (value or "").strip()
    if not cleaned:
        return None
    if cleaned.upper() in {"UTC", "GMT"}:
        return cleaned.upper()

    canonical = timezone_names_by_lowercase().get(cleaned.casefold())
    if canonical is None:
        return None
    try:
        ZoneInfo(canonical)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None
    return canonical


# ---------------------------------------------------------------------------
# Временное окно
# ---------------------------------------------------------------------------
def window_length_minutes(start: time, end: time) -> int:
    """Длина окна в минутах.

    Значение ``00:00`` в поле «конец» трактуется как конец суток, поэтому окно
    ``12:00–00:00`` длится 12 часов. Если конец раньше начала, результат
    отрицательный — окно считается некорректным.
    """
    start_minutes = start.hour * 60 + start.minute
    end_minutes = end.hour * 60 + end.minute
    if end_minutes == 0:
        end_minutes = config.MINUTES_PER_DAY
    return end_minutes - start_minutes


def min_window_minutes(words_per_day: int) -> int:
    """Минимальная длина окна: по одной минуте на каждое слово дня."""
    return max(1, words_per_day) * config.MIN_SLOT_INTERVAL_MINUTES


def check_window(start: time, end: time, words_per_day: int) -> WindowCheck:
    """Проверяет окно без обращения к текстам (тексты подставляет хендлер)."""
    length = window_length_minutes(start, end)
    if length <= 0:
        return WindowCheck(reason=WINDOW_REASON_END_BEFORE_START, current_minutes=max(length, 0))
    required = min_window_minutes(words_per_day)
    if length < required:
        return WindowCheck(
            reason=WINDOW_REASON_TOO_SHORT,
            current_minutes=length,
            required_minutes=required,
        )
    return WindowCheck(current_minutes=length, required_minutes=required)


def window_start_datetime(user: User, day: date) -> datetime:
    """Момент начала окна в местном времени пользователя."""
    return datetime.combine(day, user.window_start_time, tzinfo=user.tzinfo)


def window_end_datetime(user: User, day: date) -> datetime:
    """Момент окончания окна в местном времени пользователя.

    ``00:00`` в поле «конец» — это конец суток (см. :func:`window_length_minutes`),
    поэтому окно «12:00–00:00» закрывается в полночь следующего дня.
    """
    base = datetime.combine(day, user.window_end_time, tzinfo=user.tzinfo)
    if user.window_end_time == time(0, 0):
        return base + timedelta(days=1)
    return base


def window_minutes_left(user: User, now_utc: datetime) -> int:
    """Сколько минут окна осталось до его закрытия сегодня.

    До открытия окна возвращается полная длина окна (первое слово дня уходит в
    ``window_start``), после закрытия — ``0``.
    """
    local_now = as_utc(now_utc).astimezone(user.tzinfo)
    start = window_start_datetime(user, local_now.date())
    if local_now < start:
        return window_length_minutes(user.window_start_time, user.window_end_time)
    end = window_end_datetime(user, local_now.date())
    if local_now >= end:
        return 0
    return int((end - local_now).total_seconds() // 60)


def slot_interval(minutes_left: int, words_left: int) -> timedelta:
    """Интервал до следующего слова: оставшееся время окна / оставшиеся слова.

    Слова дня — это не только ``words_per_day``: к ним добавляются повторения,
    срок которых наступает сегодня. Пример: 15 запланированных слов и 15 слов,
    сброшенных на этап 0 вчерашним «не знаю», при окне в 11 часов — каждые
    22 минуты (660 / 30).

    Как только слово уходит из плана (ответ «знаю») или пользователь забирает
    слово вне расписания, слов остаётся меньше и интервал растёт: слова
    «расходятся» по оставшемуся времени окна.

    Ниже :data:`config.MIN_SLOT_INTERVAL_MINUTES` интервал не опускается.
    """
    seconds = max(minutes_left, 0) * 60 / max(1, words_left)
    return timedelta(seconds=max(seconds, config.MIN_SLOT_INTERVAL_MINUTES * 60))


def next_slot(user: User, now_utc: datetime, words_left: int) -> datetime | None:
    """Ближайший слот пользователя строго после ``now_utc`` (в UTC).

    :param words_left: сколько слов ещё нужно отправить сегодня (см.
        :func:`services.plan.words_left_today`). ``0`` означает, что с планом дня
        покончено: следующее слово придёт с открытием окна.
    :return: момент следующей отправки или ``None``, если рассчитать не удалось.
    """
    local_now = as_utc(now_utc).astimezone(user.tzinfo)
    if words_left <= 0:
        next_day = window_start_datetime(user, local_now.date() + timedelta(days=1))
        return next_day.astimezone(timezone.utc)

    for day_offset in range(config.SCHEDULER_LOOKAHEAD_DAYS):
        day = (local_now + timedelta(days=day_offset)).date()
        start = window_start_datetime(user, day)
        if local_now < start:
            # окно ещё не открылось — первое слово дня уходит в его начале
            return start.astimezone(timezone.utc)
        end = window_end_datetime(user, day)
        if local_now >= end:
            continue
        moment = local_now + slot_interval(window_minutes_left(user, now_utc), words_left)
        # слова не разошлись по времени: последнее из них — у самого конца окна
        return min(moment, end).astimezone(timezone.utc)
    return None


def is_within_window(user: User, now_utc: datetime) -> bool:
    """Находится ли момент внутри окна пользователя (с допуском на задержку)."""
    local_now = as_utc(now_utc).astimezone(user.tzinfo)
    start = user.window_start_time
    end = user.window_end_time

    start_seconds = start.hour * 3600 + start.minute * 60
    end_seconds = end.hour * 3600 + end.minute * 60
    if end_seconds == 0:
        end_seconds = config.SECONDS_PER_DAY
    now_seconds = local_now.hour * 3600 + local_now.minute * 60 + local_now.second
    return start_seconds <= now_seconds <= end_seconds + config.WINDOW_TOLERANCE_SECONDS


def local_day_start_utc(user: User, now_utc: datetime) -> datetime:
    """Начало текущих суток пользователя, в наивном UTC (как даты в БД).

    Сутки считаются по часовому поясу пользователя: для ``Europe/Moscow``
    «сегодня» начинается в 21:00 UTC предыдущего дня. Нужно, чтобы отличать
    сегодняшние отправки от более ранних — например, чтобы не присылать слово
    второй раз в тот же день и чтобы карточка без ответа, отправленная вчера, не
    исключала слово из очереди навсегда.
    """
    local_now = as_utc(now_utc).astimezone(user.tzinfo)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(timezone.utc).replace(tzinfo=None)


def local_day_end_utc(user: User, now_utc: datetime) -> datetime:
    """Конец текущих суток пользователя, в наивном UTC (как даты в БД).

    Используется как граница «пора показать»: одним запросом отбираются слова,
    срок повторения которых наступает в течение сегодняшних суток пользователя
    (см. :func:`services.plan.words_left_today`).
    """
    local_now = as_utc(now_utc).astimezone(user.tzinfo)
    local_end = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return local_end.astimezone(timezone.utc).replace(tzinfo=None)


def as_utc(moment: datetime) -> datetime:
    """Приводит момент к aware-UTC: наивные даты считаются UTC (как в БД)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def local_time_string(user: User, moment_utc: datetime) -> str:
    """Местное время пользователя в формате ``ЧЧ:ММ`` (для логов и сообщений)."""
    return as_utc(moment_utc).astimezone(user.tzinfo).strftime(config.TIME_FORMAT)

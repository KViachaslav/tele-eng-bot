"""Математика расписания: временные окна, слоты и часовые пояса.

Модуль не обращается ни к БД, ни к Telegram — только чистые функции от настроек
пользователя и момента времени. Поэтому его легко покрывать юнит-тестами
(см. ``tests/test_slots.py``).
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


def slot_interval(user: User) -> timedelta:
    """Интервал между словами внутри окна: длина окна, делённая на число слов."""
    start = user.window_start_time
    end = user.window_end_time
    length_seconds = window_length_minutes(start, end) * 60
    interval_seconds = length_seconds / max(1, user.words_per_day)
    minimum_seconds = config.MIN_SLOT_INTERVAL_MINUTES * 60
    return timedelta(seconds=max(interval_seconds, minimum_seconds))


def window_start_datetime(user: User, day: date) -> datetime:
    """Момент начала окна в местном времени пользователя."""
    return datetime.combine(day, user.window_start_time, tzinfo=user.tzinfo)


def slots_for_day(user: User, day: date) -> list[datetime]:
    """Слоты рассылки на конкретный день (местное время пользователя).

    Первое слово уходит в начале окна, последнее — за один интервал до его конца.
    """
    base = window_start_datetime(user, day)
    interval = slot_interval(user)
    return [base + interval * index for index in range(max(1, user.words_per_day))]


def next_slot(user: User, now_utc: datetime) -> datetime | None:
    """Ближайший слот пользователя строго после ``now_utc`` (в UTC)."""
    local_now = as_utc(now_utc).astimezone(user.tzinfo)
    for day_offset in range(config.SCHEDULER_LOOKAHEAD_DAYS):
        day = (local_now + timedelta(days=day_offset)).date()
        for slot in slots_for_day(user, day):
            if slot > local_now:
                return slot.astimezone(timezone.utc)
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


def as_utc(moment: datetime) -> datetime:
    """Приводит момент к aware-UTC: наивные даты считаются UTC (как в БД)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def local_time_string(user: User, moment_utc: datetime) -> str:
    """Местное время пользователя в формате ``ЧЧ:ММ`` (для логов и сообщений)."""
    return as_utc(moment_utc).astimezone(user.tzinfo).strftime(config.TIME_FORMAT)

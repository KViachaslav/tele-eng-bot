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


@dataclass(frozen=True, slots=True)
class WindowBounds:
    """Окно рассылки пользователя: часовой пояс и границы дня в местном времени."""

    tzinfo: ZoneInfo
    start: time
    end: time

    @classmethod
    def of(cls, user: User) -> WindowBounds:
        """Окно из настроек пользователя."""
        return cls(tzinfo=user.tzinfo, start=user.window_start_time, end=user.window_end_time)

    def open_at(self, local_day: date) -> datetime:
        """Момент открытия окна в местном времени пользователя."""
        return datetime.combine(local_day, self.start, tzinfo=self.tzinfo)

    def close_at(self, local_day: date) -> datetime:
        """Момент закрытия окна в местном времени пользователя.

        ``00:00`` в поле «конец» — это конец суток (см.
        :func:`window_length_minutes`), поэтому окно «12:00–00:00» закрывается в
        полночь следующего дня.
        """
        base = datetime.combine(local_day, self.end, tzinfo=self.tzinfo)
        if self.end == time(0, 0):
            return base + timedelta(days=1)
        return base


def window_start_datetime(user: User, day: date) -> datetime:
    """Момент начала окна в местном времени пользователя."""
    return WindowBounds.of(user).open_at(day)


def window_end_datetime(user: User, day: date) -> datetime:
    """Момент окончания окна в местном времени пользователя.

    ``00:00`` в поле «конец» — это конец суток (см. :func:`window_length_minutes`),
    поэтому окно «12:00–00:00» закрывается в полночь следующего дня.
    """
    return WindowBounds.of(user).close_at(day)


def align_to_window(bounds: WindowBounds, moment_utc: datetime) -> datetime:
    """Сдвигает срок повторения внутрь окна рассылки (наивный UTC, как в БД).

    Рассылка работает только внутри окна, поэтому срок, попавший за его закрытие,
    показывать нечем: слово стоит в плане дня, но ни один слот его не отправляет —
    оставшиеся слова загоняли интервал в минимум, и день заканчивался без слов
    (в базе 09.10.2026 «план дня 2», «отправлено 0»: слова ждали 18:08 и 18:10 при
    окне до 18:00). Ответ на карточку приходил уже после закрытия окна — отсюда и
    такой срок, поэтому сдвиг делается при записи ответа
    (:func:`services.srs.apply_answer`).

    Сдвиг — на **открытие окна тех же местных суток**, к которым относится срок:
    попав за закрытие, слово вернётся в начале того же дня и сразу станет
    просроченным (то есть попадёт в ближайший слот), а попав раньше открытия — в
    начале дня, чтобы слово не потерялось в ночном слоте.
    """
    moment = as_utc(moment_utc)
    local = moment.astimezone(bounds.tzinfo)
    opened = bounds.open_at(local.date())
    closed = bounds.close_at(local.date())
    if local < opened or local >= closed:
        return opened.astimezone(timezone.utc).replace(tzinfo=None)
    return moment.replace(tzinfo=None)


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


def local_date(user: User, moment_utc: datetime) -> date:
    """Местная дата пользователя для момента ``moment_utc``.

    Сутки считаются по часовому поясу пользователя: для ``Europe/Moscow`` момент
    21:30 UTC — это уже следующие сутки (00:30 по Москве). Из этой даты строятся
    границы суток (:func:`local_day_bounds_utc`) и прогноз на несколько дней
    (:func:`services.plan.upcoming_plan`).
    """
    return as_utc(moment_utc).astimezone(user.tzinfo).date()


def local_day_bounds_utc(user: User, local_day: date) -> tuple[datetime, datetime]:
    """Границы местных суток ``local_day`` в наивном UTC (как даты в БД).

    Возвращает ``(начало, конец)``: полночь выбранных суток по поясу пользователя
    и полночь следующих суток. Для ``Europe/Moscow`` 15 января — это 21:00 UTC
    14 января и 21:00 UTC 15 января.
    """
    local_start = datetime.combine(local_day, time.min, tzinfo=user.tzinfo)
    start = local_start.astimezone(timezone.utc).replace(tzinfo=None)
    end = (local_start + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
    return start, end


def local_day_start_utc(user: User, now_utc: datetime) -> datetime:
    """Начало текущих суток пользователя, в наивном UTC (как даты в БД).

    Сутки считаются по часовому поясу пользователя: для ``Europe/Moscow``
    «сегодня» начинается в 21:00 UTC предыдущего дня. Нужно, чтобы отличать
    сегодняшние отправки от более ранних — например, чтобы не присылать слово
    второй раз в тот же день и чтобы карточка без ответа, отправленная вчера, не
    исключала слово из очереди навсегда.
    """
    return local_day_bounds_utc(user, local_date(user, now_utc))[0]


def local_day_end_utc(user: User, now_utc: datetime) -> datetime:
    """Конец текущих суток пользователя, в наивном UTC (как даты в БД).

    Используется как граница «пора показать»: одним запросом отбираются слова,
    срок повторения которых наступает в течение сегодняшних суток пользователя
    (см. :func:`services.plan.words_left_today`).
    """
    return local_day_bounds_utc(user, local_date(user, now_utc))[1]


def as_utc(moment: datetime) -> datetime:
    """Приводит момент к aware-UTC: наивные даты считаются UTC (как в БД)."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def local_time_string(user: User, moment_utc: datetime) -> str:
    """Местное время пользователя в формате ``ЧЧ:ММ`` (для логов и сообщений)."""
    return as_utc(moment_utc).astimezone(user.tzinfo).strftime(config.TIME_FORMAT)

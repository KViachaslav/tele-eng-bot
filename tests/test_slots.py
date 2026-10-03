"""Юнит-тесты математики расписания (``services/slots.py``)."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

import config
from db.models import User
from services import slots


def make_user(
    *,
    timezone_name: str = config.DEFAULT_TIMEZONE,
    words_per_day: int = 10,
    window_start: str = "09:00",
    window_end: str = "21:00",
) -> User:
    """Пользователь в памяти — для проверок без обращения к БД."""
    return User(
        telegram_id=1,
        timezone=timezone_name,
        words_per_day=words_per_day,
        window_start=window_start,
        window_end=window_end,
        paused=False,
        reveal_mode=config.REVEAL_MODE_ON_BUTTON,
        pos_filter=config.POS_FILTER_ALL,
    )


# ---------------------------------------------------------------------------
# Разбор и форматирование
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9:00", time(9, 0)),
        ("09:00", time(9, 0)),
        ("9.00", time(9, 0)),
        ("0900", time(9, 0)),
        ("900", time(9, 0)),
        (" 21:30 ", time(21, 30)),
        ("24:00", None),
        ("12:60", None),
        ("12:5", None),
        ("полдень", None),
        ("", None),
    ],
)
def test_parse_time(raw: str, expected: time | None) -> None:
    """Время принимается в разных форматах, некорректное — отвергается."""
    assert slots.parse_time(raw) == expected


def test_time_round_trip_for_callback_data() -> None:
    """``encode_time``/``decode_time`` сохраняют время для callback-данных."""
    moment = time(21, 30)
    assert slots.format_time(moment) == "21:30"
    assert slots.encode_time(moment) == "2130"
    assert slots.decode_time("2130") == moment
    assert slots.decode_time("213") is None
    assert slots.decode_time("21300") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Europe/Moscow", "Europe/Moscow"),
        ("europe/moscow", "Europe/Moscow"),
        ("utc", "UTC"),
        ("GMT", "GMT"),
        ("Mars/Olympus", None),
        ("", None),
    ],
)
def test_parse_timezone(raw: str, expected: str | None) -> None:
    """Часовой пояс проверяется по справочнику IANA и приводится к канону."""
    assert slots.parse_timezone(raw) == expected


# ---------------------------------------------------------------------------
# Временное окно
# ---------------------------------------------------------------------------
def test_window_length_minutes() -> None:
    """Длина окна считается в минутах, «00:00» — конец суток."""
    assert slots.window_length_minutes(time(9, 0), time(21, 0)) == 12 * 60
    assert slots.window_length_minutes(time(12, 0), time(0, 0)) == 12 * 60
    assert slots.window_length_minutes(time(9, 0), time(9, 30)) == 30
    assert slots.window_length_minutes(time(21, 0), time(9, 0)) == -12 * 60


def test_min_window_minutes() -> None:
    """Минимум окна — по минуте на слово, но не меньше одной минуты."""
    assert slots.min_window_minutes(10) == 10 * config.MIN_SLOT_INTERVAL_MINUTES
    assert slots.min_window_minutes(0) == config.MIN_SLOT_INTERVAL_MINUTES


def test_check_window_accepts_valid_range() -> None:
    """Корректное окно проходит проверку."""
    result = slots.check_window(time(9, 0), time(21, 0), 10)
    assert result.ok is True
    assert result.reason is None
    assert result.current_minutes == 720


def test_check_window_rejects_reversed_range() -> None:
    """Конец раньше начала — окно не принимается."""
    result = slots.check_window(time(21, 0), time(9, 0), 10)
    assert result.ok is False
    assert result.reason == slots.WINDOW_REASON_END_BEFORE_START
    assert result.current_minutes == 0


def test_check_window_rejects_too_short_range() -> None:
    """Слишком короткое окно не вмещает нужное число слов."""
    result = slots.check_window(time(9, 0), time(9, 5), 10)
    assert result.ok is False
    assert result.reason == slots.WINDOW_REASON_TOO_SHORT
    assert (result.current_minutes, result.required_minutes) == (5, 10)



# ---------------------------------------------------------------------------
# Слоты
# ---------------------------------------------------------------------------
def test_slot_interval_divides_left_time_by_left_words() -> None:
    """Интервал — это оставшееся время окна, делённое на оставшиеся слова."""
    assert slots.slot_interval(12 * 60, 10) == timedelta(minutes=72)
    assert slots.slot_interval(9 * 60, 10) == timedelta(minutes=54)


def test_slot_interval_counts_repetitions_of_the_day() -> None:
    """Повторения входят в план дня, поэтому слова приходят чаще.

    15 запланированных слов и 15 слов, сброшенных на этап 0 вчерашним «не знаю»,
    при окне 10:00–21:00 (660 минут) — каждые 22 минуты; без повторений —
    каждые 44.
    """
    assert slots.slot_interval(11 * 60, 30) == timedelta(minutes=22)
    assert slots.slot_interval(11 * 60, 15) == timedelta(minutes=44)


def test_slot_interval_respects_minimum() -> None:
    """Слишком частые слоты не сжимаются ниже минимального интервала."""
    minimum = timedelta(minutes=config.MIN_SLOT_INTERVAL_MINUTES)

    assert slots.slot_interval(30, 50) == minimum
    assert slots.slot_interval(0, 1) == minimum


def test_window_minutes_left() -> None:
    """Оставшиеся минуты окна: до открытия — всё окно, после закрытия — ноль."""
    user = make_user(timezone_name="UTC", window_start="09:00", window_end="21:00")

    assert slots.window_minutes_left(user, datetime(2026, 1, 15, 8, 0)) == 12 * 60
    assert slots.window_minutes_left(user, datetime(2026, 1, 15, 12, 0)) == 9 * 60
    assert slots.window_minutes_left(user, datetime(2026, 1, 15, 21, 0)) == 0


def test_window_end_datetime_handles_midnight_close() -> None:
    """«00:00» в конце окна — это конец суток, а не их начало."""
    user = make_user(timezone_name="UTC", window_start="12:00", window_end="00:00")

    assert slots.window_end_datetime(user, date(2026, 1, 15)) == datetime(
        2026, 1, 16, 0, 0, tzinfo=user.tzinfo
    )


def test_next_slot_divides_left_time_and_words() -> None:
    """Новое слово приходит через «оставшееся время / оставшиеся слова»."""
    user = make_user(timezone_name="UTC", words_per_day=10)

    # 12:00, окончится в 21:00: 9 часов на 10 оставшихся слов — 54 минуты.
    moment = slots.next_slot(user, datetime(2026, 1, 15, 12, 0), 10)

    assert moment == datetime(2026, 1, 15, 12, 54, tzinfo=timezone.utc)


def test_next_slot_with_repetitions_of_the_day_comes_faster() -> None:
    """15 запланированных слов и 15 повторений — каждые 22 минуты."""
    user = make_user(
        timezone_name="UTC", words_per_day=15, window_start="10:00", window_end="21:00"
    )

    moment = slots.next_slot(user, datetime(2026, 1, 15, 10, 0), 30)

    assert moment == datetime(2026, 1, 15, 10, 22, tzinfo=timezone.utc)


def test_next_slot_returns_utc_moment() -> None:
    """До открытия окна первое слово дня уходит в ``window_start``."""
    user = make_user(timezone_name="Europe/Moscow", words_per_day=10)

    moment = slots.next_slot(user, datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc), 10)

    assert moment == datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)


def test_next_slot_skips_to_next_day() -> None:
    """Если окно уже закрыто, берётся начало окна следующего дня."""
    user = make_user(timezone_name="Europe/Moscow", words_per_day=10)

    moment = slots.next_slot(user, datetime(2026, 1, 15, 19, 0, tzinfo=timezone.utc), 10)

    assert moment == datetime(2026, 1, 16, 6, 0, tzinfo=timezone.utc)


def test_next_slot_with_empty_plan_moves_to_next_day() -> None:
    """Ноль слов в плане — сегодня слов больше не будет."""
    user = make_user(timezone_name="UTC", words_per_day=10)

    moment = slots.next_slot(user, datetime(2026, 1, 15, 12, 0), 0)

    assert moment == datetime(2026, 1, 16, 9, 0, tzinfo=timezone.utc)


def test_next_slot_puts_last_word_at_window_end() -> None:
    """Когда слово осталось одно, оно приходится на конец окна."""
    user = make_user(timezone_name="UTC", words_per_day=10)

    moment = slots.next_slot(user, datetime(2026, 1, 15, 12, 0), 1)

    assert moment == datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc)


def test_next_slot_does_not_go_past_window_end() -> None:
    """В последнюю минуту окна слот не выходит за его границу."""
    user = make_user(timezone_name="UTC", words_per_day=10)

    moment = slots.next_slot(user, datetime(2026, 1, 15, 20, 59, 30), 1)

    assert moment == datetime(2026, 1, 15, 21, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Проверка «внутри окна»
# ---------------------------------------------------------------------------
def test_is_within_window_with_tolerance() -> None:
    """Проверка «внутри окна» терпит небольшое опоздание задачи."""
    user = make_user(timezone_name="UTC")

    assert slots.is_within_window(user, datetime(2026, 1, 15, 12, 0)) is True
    assert slots.is_within_window(user, datetime(2026, 1, 15, 8, 0)) is False
    assert slots.is_within_window(user, datetime(2026, 1, 15, 21, 1)) is True
    assert slots.is_within_window(user, datetime(2026, 1, 15, 21, 5)) is False


def test_is_within_window_midnight_end() -> None:
    """Окно, заканчивающееся в 00:00, тянется до конца суток."""
    user = make_user(timezone_name="UTC", window_start="12:00", window_end="00:00")

    assert slots.is_within_window(user, datetime(2026, 1, 15, 23, 59)) is True
    assert slots.is_within_window(user, datetime(2026, 1, 15, 11, 0)) is False


def test_local_time_string_and_as_utc() -> None:
    """Местное время пользователя и приведение наивных дат к UTC."""
    user = make_user(timezone_name="Europe/Moscow")

    assert slots.local_time_string(user, datetime(2026, 1, 15, 6, 0)) == "09:00"
    naive = datetime(2026, 1, 15, 6, 0)
    assert slots.as_utc(naive) == datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)
    moscow = datetime(2026, 1, 15, 9, 0, tzinfo=timezone(timedelta(hours=3)))
    assert slots.as_utc(moscow) == datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)


def test_local_day_start_utc_counts_days_by_user_timezone() -> None:
    """Начало «сегодня» берётся по поясу пользователя (в UTC — предыдущий день)."""
    moscow = make_user(timezone_name="Europe/Moscow")

    # Полдень 15 января по Москве: сутки начались в 21:00 UTC 14 января.
    assert slots.local_day_start_utc(moscow, datetime(2026, 1, 15, 9, 0)) == datetime(
        2026, 1, 14, 21, 0
    )
    # Первый час тех же суток по Москве (00:30) — та же граница, а не сутки назад.
    assert slots.local_day_start_utc(moscow, datetime(2026, 1, 14, 21, 30)) == datetime(
        2026, 1, 14, 21, 0
    )

    utc_user = make_user(timezone_name="UTC")
    assert slots.local_day_start_utc(utc_user, datetime(2026, 1, 15, 9, 0)) == datetime(
        2026, 1, 15, 0, 0
    )


def test_local_day_end_utc_counts_days_by_user_timezone() -> None:
    """Конец «сегодня» — полночь по поясу пользователя (в UTC — вечер того же дня)."""
    moscow = make_user(timezone_name="Europe/Moscow")

    assert slots.local_day_end_utc(moscow, datetime(2026, 1, 15, 9, 0)) == datetime(
        2026, 1, 15, 21, 0
    )

    utc_user = make_user(timezone_name="UTC")
    assert slots.local_day_end_utc(utc_user, datetime(2026, 1, 15, 9, 0)) == datetime(
        2026, 1, 16, 0, 0
    )

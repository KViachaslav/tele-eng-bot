"""Тесты пакетного перевода словаря: сервис и скрипт ``translate_words``.

Сеть подменяется так же, как в ``tests/test_translator.py``: вместо MyMemory в
``translator.request_translation`` кладётся заготовка, поэтому тесты не зависят
ни от интернета, ни от суточного лимита сервиса. Проверяются выборка
недопереведённых слов, счётчики порции, остановка на исчерпанном лимите,
``--dry-run`` и разбор аргументов командной строки.
"""
from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import TRANSLATION_BOTH, Word, translation_field_pairs
from scripts import translate_words
from services import translator


@pytest.fixture(autouse=True)
def translation_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включает перевод: в ``conftest`` он для всего набора тестов выключен."""
    monkeypatch.setattr(config.get_settings(), "translation_enabled", True)
    translator.reset_cooldown()


def payload(text: str) -> dict[str, Any]:
    """Ответ MyMemory с переводом ``text``."""
    return {"responseStatus": 200, "responseData": {"translatedText": text}}


class RecordingRequest:
    """Заглушка ``translator.request_translation``: помнит запрошенные строки.

    Ответы выдаются по очереди; ``None`` означает «сервис не ответил» — так
    проверяется поведение порции при сбоях сети. Подпись та же, что у настоящей
    функции (с таймаутом запроса).
    """

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.queries: list[str] = []
        self.timeouts: list[float | None] = []

    async def __call__(self, text: str, timeout: float | None = None) -> dict[str, Any] | None:
        self.queries.append(text)
        self.timeouts.append(timeout)
        answer = self.answers.pop(0) if self.answers else None
        return None if answer is None else payload(answer)


class RepeatingRequest:
    """Заглушка ``translator.request_translation``: один перевод на любой запрос."""

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.queries: list[str] = []
        self.timeouts: list[float | None] = []

    async def __call__(self, text: str, timeout: float | None = None) -> dict[str, Any] | None:
        self.queries.append(text)
        self.timeouts.append(timeout)
        return payload(self.answer)


# ---------------------------------------------------------------------------
# Пары полей и аргументы скрипта
# ---------------------------------------------------------------------------
def test_translation_field_pairs_selects_fields() -> None:
    """Пары «английское поле → русское» выбираются по имени поля."""
    assert translation_field_pairs() == Word.TRANSLATION_FIELDS
    assert translation_field_pairs("example") == (("example", "russian_example"),)


def test_translation_field_pairs_rejects_unknown_field() -> None:
    """Неизвестное имя поля — ошибка, а не молчаливая пустая выборка."""
    with pytest.raises(ValueError, match="synonyms"):
        translation_field_pairs("synonyms")


def test_parse_args_defaults() -> None:
    """По умолчанию порция — суточный лимит MyMemory, поля — оба, без сети не гадаем."""
    args = translate_words.parse_args([])

    assert args.limit == config.TRANSLATION_BATCH_LIMIT
    assert args.field == TRANSLATION_BOTH
    assert args.sleep_seconds == config.TRANSLATION_BATCH_SLEEP_SECONDS
    assert args.max_failures == config.TRANSLATION_BATCH_MAX_FAILURES
    assert args.email is None
    assert args.dry_run is False
    assert args.learning_first is False


def test_parse_args_reads_options() -> None:
    """Все ключи скрипта разбираются."""
    args = translate_words.parse_args(
        [
            "--limit",
            "10",
            "--field",
            "example",
            "--sleep",
            "0",
            "--max-failures",
            "1",
            "--dry-run",
            "--learning-first",
        ]
    )

    assert (args.limit, args.field, args.sleep_seconds, args.max_failures) == (10, "example", 0.0, 1)
    assert args.dry_run is True
    assert args.learning_first is True


def test_apply_email_sets_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--email`` попадает в ``MYMEMORY_EMAIL``: с почтой лимит сервиса выше."""
    monkeypatch.delenv("MYMEMORY_EMAIL", raising=False)

    translate_words.apply_email("user@example.com")

    assert os.environ["MYMEMORY_EMAIL"] == "user@example.com"


def test_apply_email_keeps_environment_without_option(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ``--email`` почта из окружения (или ``.env``) не перетирается."""
    monkeypatch.setenv("MYMEMORY_EMAIL", "from-env@example.com")

    translate_words.apply_email(None)

    assert os.environ["MYMEMORY_EMAIL"] == "from-env@example.com"


def test_daily_char_limit_depends_on_email(monkeypatch: pytest.MonkeyPatch) -> None:
    """С указанной почтой суточный лимит в десять раз больше."""
    settings = config.get_settings()
    monkeypatch.setattr(settings, "mymemory_email", "")

    assert translate_words.daily_char_limit(settings) == config.MYMEMORY_DAILY_CHARS_ANONYMOUS

    monkeypatch.setattr(settings, "mymemory_email", "user@example.com")

    assert translate_words.daily_char_limit(settings) == config.MYMEMORY_DAILY_CHARS_WITH_EMAIL


# ---------------------------------------------------------------------------
# Отчёт о непереведённом остатке (``--dry-run``)
# ---------------------------------------------------------------------------
async def test_report_counts_words_and_chars(session: AsyncSession, words) -> None:
    """Отчёт считает слова и символы, ничего не спрашивая у сервиса."""
    pending = await translate_words.report(
        session, field=TRANSLATION_BOTH, settings=config.get_settings()
    )

    assert pending.words == len(words)
    assert pending.chars == sum(len(word.definition) + len(word.example) for word in words)


async def test_report_says_nothing_to_do_when_translated(session: AsyncSession, words) -> None:
    """Переведённый словарь: отчёт показывает ноль."""
    for word in words:
        await repository.save_word_translations(
            session, word, russian_definition="перевод", russian_example="перевод"
        )

    pending = await translate_words.report(
        session, field=TRANSLATION_BOTH, settings=config.get_settings()
    )

    assert (pending.words, pending.chars) == (0, 0)


# ---------------------------------------------------------------------------
# Порция перевода
# ---------------------------------------------------------------------------
async def test_translate_pending_words_translates_batch(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Порция переводит все недопереведённые слова и сохраняет их сразу."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    result = await translator.translate_pending_words(session, sleep_seconds=0)

    assert (result.words, result.definitions, result.examples) == (
        len(words),
        len(words),
        len(words),
    )
    assert (result.failed, result.left, result.quota_stopped) == (0, 0, False)
    assert request.queries == [text for word in words for text in (word.definition, word.example)]
    saved = await repository.get_word_by_id(session, words[0].id)
    assert saved is not None
    assert (saved.russian_definition, saved.russian_example) == ("перевод", "перевод")


async def test_translate_pending_words_does_not_repeat_translated(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Повторный прогон по тому же словарю не делает ни одного запроса."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    await translator.translate_pending_words(session, sleep_seconds=0)
    first_run_queries = len(request.queries)
    result = await translator.translate_pending_words(session, sleep_seconds=0)

    assert (result.words, result.left) == (0, 0)
    assert len(request.queries) == first_run_queries


async def test_translate_pending_words_respects_limit(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """``limit`` ограничивает порцию: остальное остаётся на следующий запуск."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    result = await translator.translate_pending_words(session, limit=1, sleep_seconds=0)

    assert (result.words, result.left) == (1, len(words) - 1)
    assert request.queries == [words[0].definition, words[0].example]


async def test_translate_pending_words_field_definition_only(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """``field=definition`` переводит только определения, примеры не трогает."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    result = await translator.translate_pending_words(session, field="definition", sleep_seconds=0)

    assert (result.words, result.definitions, result.examples, result.left) == (
        len(words),
        len(words),
        0,
        0,
    )
    assert request.queries == [word.definition for word in words]
    saved = await repository.get_word_by_id(session, words[0].id)
    assert saved is not None
    assert (saved.russian_definition, saved.russian_example) == ("перевод", None)


async def test_translate_pending_words_survives_single_failure(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Одиночный сбой сети порцию не убивает: слово остаётся на следующий запуск."""
    # Первому слову сервис не отвечает, дальше отвечает нормально.
    request = RecordingRequest(None, None, "перевод", "перевод", "перевод", "перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    result = await translator.translate_pending_words(session, sleep_seconds=0)

    assert (result.failed, result.words, result.quota_stopped) == (1, len(words) - 1, False)
    assert result.left == 1  # без перевода осталось только первое слово
    assert translator.in_cooldown() is False  # общей паузы пакетный перевод не ставит
    saved = await repository.get_word_by_id(session, words[0].id)
    assert saved is not None
    assert saved.russian_definition is None


async def test_translate_pending_words_stops_after_max_failures(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Сервис не отвечает совсем: порция останавливается, ничего не записав."""
    request = RecordingRequest(*[None] * 10)
    monkeypatch.setattr(translator, "request_translation", request)

    result = await translator.translate_pending_words(session, sleep_seconds=0, max_failures=2)

    assert (result.failed, result.words, result.quota_stopped) == (2, 0, True)
    assert result.left == len(words)
    assert request.queries == [
        words[0].definition,
        words[0].example,
        words[1].definition,
        words[1].example,
    ]


async def test_translate_pending_words_skips_during_cooldown(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Если после сбоя в карточке идёт пауза, порция даже не начинает работу."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)
    translator.start_cooldown()

    result = await translator.translate_pending_words(session, sleep_seconds=0)

    assert (result.words, result.failed, result.quota_stopped) == (0, 0, True)
    assert request.queries == []


async def test_translate_pending_words_waits_longer_than_card(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Пакетный перевод ждёт ответ сервиса дольше, чем карточка (там важен темп)."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    await translator.translate_pending_words(session, limit=1, sleep_seconds=0)

    assert request.timeouts == [config.TRANSLATION_BATCH_TIMEOUT_SECONDS] * 2
    assert config.TRANSLATION_BATCH_TIMEOUT_SECONDS > config.TRANSLATION_TIMEOUT_SECONDS


async def test_translate_pending_words_respects_switch(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """При ``TRANSLATION_ENABLED=false`` порция не делает ни одного запроса."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)
    monkeypatch.setattr(config.get_settings(), "translation_enabled", False)

    result = await translator.translate_pending_words(session, sleep_seconds=0)

    assert (result.words, result.failed, result.quota_stopped) == (0, 0, False)
    assert request.queries == []


async def test_script_translate_passes_args(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """``--limit 0`` — «без ограничения», ``--field`` доезжает до сервиса."""
    request = RepeatingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    args = translate_words.parse_args(["--limit", "0", "--field", "example", "--sleep", "0"])
    result = await translate_words.translate(session, args)

    assert (result.words, result.examples, result.definitions, result.left) == (
        len(words),
        len(words),
        0,
        0,
    )
    assert request.queries == [word.example for word in words]

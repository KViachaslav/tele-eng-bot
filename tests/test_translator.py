"""Тесты перевода определения и примера (:mod:`services.translator`).

Сеть подменяется: тесты не ходят в MyMemory, а кладут в ``request_translation``
заранее заготовленный ответ. Так проверяются разбор ответа, отсечение ошибок
сервиса, пауза после неудачи и запись переводов в базу.
"""
from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import config
from db import repository
from db.models import Word
from services import translator


@pytest.fixture(autouse=True)
def translation_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Включает перевод: в ``conftest`` он для всего набора тестов выключен."""
    monkeypatch.setattr(config.get_settings(), "translation_enabled", True)
    translator.reset_cooldown()


def payload(text: str, status: int | str = 200) -> dict[str, Any]:
    """Ответ MyMemory с переводом ``text``."""
    return {"responseStatus": status, "responseData": {"translatedText": text}}


class RecordingRequest:
    """Заглушка ``translator.request_translation``: помнит запрошенные строки.

    Ответы выдаются по очереди; ``None`` означает «сервис не ответил» — так
    проверяются отсечение ошибок и пауза после неудачи.
    """

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.queries: list[str] = []

    async def __call__(
        self, text: str, timeout: float | None = None
    ) -> dict[str, Any] | None:
        """Та же подпись, что у настоящего ``translator.request_translation``."""
        self.queries.append(text)
        answer = self.answers.pop(0) if self.answers else None
        return None if answer is None else payload(answer)


def test_langpair_is_english_to_russian() -> None:
    """Запрос уходит с парой языков ``en|ru``."""
    assert translator.langpair() == "en|ru"


def test_parse_translation_returns_trimmed_text() -> None:
    """Перевод из ответа сервиса отдаётся без лишних пробелов."""
    assert translator.parse_translation(payload(" круглый плод ")) == "круглый плод"


def test_parse_translation_accepts_string_status() -> None:
    """``responseStatus`` строкой (``"200"``) — тоже успешный ответ."""
    assert translator.parse_translation(payload("яблоко", status="200")) == "яблоко"


@pytest.mark.parametrize(
    "response",
    (
        None,
        {},
        {"responseStatus": 403, "responseData": {"translatedText": "x"}},
        {"responseStatus": 200, "responseData": "не словарь"},
        payload(""),
        payload("MYMEMORY WARNING: YOU USED ALL AVAILABLE FREE TRANSLATIONS"),
    ),
)
def test_parse_translation_rejects_non_translations(response: Any) -> None:
    """Ответ без перевода (ошибка, предупреждение сервиса, пустой текст) — ``None``."""
    assert translator.parse_translation(response) is None


async def test_translate_text_normalizes_whitespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Строка уходит в сервис одним пробелом между словами."""
    request = RecordingRequest("круглый плод с красной или зелёной кожурой")
    monkeypatch.setattr(translator, "request_translation", request)

    text = await translator.translate_text("  a round\nfruit   with red or green skin ")

    assert text == "круглый плод с красной или зелёной кожурой"
    assert request.queries == ["a round fruit with red or green skin"]


async def test_translate_text_skips_empty_and_long_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустые и слишком длинные строки не отправляются сервису."""
    request = RecordingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    assert await translator.translate_text("   ") is None
    assert await translator.translate_text("a" * (config.TRANSLATION_TEXT_LIMIT + 1)) is None
    assert request.queries == []


async def test_translate_text_starts_cooldown_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Сервис не ответил — перевода нет, но включается пауза для остальных карточек."""
    request = RecordingRequest(None)
    monkeypatch.setattr(translator, "request_translation", request)

    assert await translator.translate_text("a round fruit") is None
    assert request.queries == ["a round fruit"]
    assert translator.in_cooldown() is True


async def test_ensure_word_translations_saves_both_lines(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Переводы определения и примера сохраняются в самой статье слова."""
    word = words[0]
    request = RecordingRequest("круглый плод", "Она съела яблоко на завтрак.")
    monkeypatch.setattr(translator, "request_translation", request)

    assert await translator.ensure_word_translations(session, word) is True

    assert request.queries == [word.definition, word.example]
    saved = await repository.get_word_by_id(session, word.id)
    assert saved is not None
    assert saved.russian_definition == "круглый плод"
    assert saved.russian_example == "Она съела яблоко на завтрак."


async def test_ensure_word_translations_does_not_repeat_translated_word(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """У переведённого слова сервис второй раз не спрашивают."""
    word = words[0]
    request = RecordingRequest("круглый плод", "Она съела яблоко на завтрак.")
    monkeypatch.setattr(translator, "request_translation", request)

    assert await translator.ensure_word_translations(session, word) is True
    assert await translator.ensure_word_translations(session, word) is False

    assert len(request.queries) == 2


async def test_ensure_word_translations_skips_word_without_source_text(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession
) -> None:
    """Переводить нечего (нет ни определения, ни примера) — запросов нет."""
    request = RecordingRequest("перевод")
    monkeypatch.setattr(translator, "request_translation", request)

    assert await translator.ensure_word_translations(session, Word(word="apple")) is False
    assert request.queries == []


async def test_ensure_word_translations_skips_during_cooldown(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """Во время паузы после неудачи перевод не запрашивается вовсе."""
    request = RecordingRequest("круглый плод")
    monkeypatch.setattr(translator, "request_translation", request)
    translator.start_cooldown()

    assert await translator.ensure_word_translations(session, words[0]) is False
    assert request.queries == []


async def test_ensure_word_translations_stops_after_failure(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """После первой неудачи вторую строку не спрашивают, в базу ничего не пишется."""
    word = words[0]
    request = RecordingRequest(None, "Она съела яблоко на завтрак.")
    monkeypatch.setattr(translator, "request_translation", request)

    assert await translator.ensure_word_translations(session, word) is False

    assert request.queries == [word.definition]
    saved = await repository.get_word_by_id(session, word.id)
    assert saved is not None
    assert saved.russian_definition is None
    assert saved.russian_example is None


async def test_ensure_word_translations_respects_switch(
    monkeypatch: pytest.MonkeyPatch, session: AsyncSession, words
) -> None:
    """При ``TRANSLATION_ENABLED=false`` сервис не трогают."""
    request = RecordingRequest("круглый плод")
    monkeypatch.setattr(translator, "request_translation", request)
    monkeypatch.setattr(config.get_settings(), "translation_enabled", False)

    assert await translator.ensure_word_translations(session, words[0]) is False
    assert request.queries == []

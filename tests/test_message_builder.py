"""Тесты сборки текста карточки слова (services/message_builder.py).

Проверяется формат из раздела 5 ТЗ: слово, перевод (спойлер или по кнопке) и
экранирование всех значений для MarkdownV2.
"""

from __future__ import annotations

import config
import locales.ru as texts
from db.models import UserSettings, Word
from services.message_builder import (
    build_audio_caption,
    build_refresh_card,
    build_word_card,
    escape_text,
    spoiler,
    with_answer_result,
)

#: Набор полей «как по умолчанию» (см. раздел 5 ТЗ и миграцию user_settings).
DEFAULT_FIELDS = (
    config.FIELD_DEFINITION,
    config.FIELD_EXAMPLE,
    config.FIELD_SYNONYMS,
    config.FIELD_ANTONYMS,
    config.FIELD_COLLOCATIONS,
)


def make_word(**overrides: str | None) -> Word:
    """Словарная статья в памяти (без обращения к БД)."""
    values: dict[str, object] = {
        "word": "apple",
        "definition": "a round fruit",
        "russian_translation": "яблоко",
        "example": "She ate an apple.",
        "part_of_speech": "noun",
        "related_forms": "apples",
        "synonyms": "fruit",
        "antonyms": "vegetable",
        "collocations": "green apple",
    }
    values.update(overrides)
    return Word(**values)


def make_settings(*fields: str, **flags: bool) -> UserSettings:
    """Настройки карточки: перечисленные поля включены, остальные выключены."""
    values: dict[str, object] = {name: name in fields for name in config.FIELD_NAMES}
    values.update(flags)
    return UserSettings(**values)


def test_spoiler_mode_hides_translation_under_spoiler() -> None:
    """В режиме «всё сразу» перевод прячется спойлером Telegram."""
    card = build_word_card(
        make_word(), make_settings(*DEFAULT_FIELDS), reveal_mode=config.REVEAL_MODE_SPOILER
    )

    assert config.SPOILER_DELIMITER in card
    assert escape_text("яблоко") in card
    assert card.startswith(escape_text("apple"))


def test_button_mode_reveals_translation_without_spoiler() -> None:
    """В режиме «по кнопке» после показа перевода спойлера уже нет."""
    card = build_word_card(
        make_word(), make_settings(*DEFAULT_FIELDS), reveal_mode=config.REVEAL_MODE_ON_BUTTON
    )

    assert config.SPOILER_DELIMITER not in card
    assert escape_text("яблоко") in card


def test_button_mode_without_reveal_returns_only_word() -> None:
    """До нажатия «Показать» в сообщении только слово."""
    card = build_word_card(
        make_word(),
        make_settings(*DEFAULT_FIELDS),
        reveal_mode=config.REVEAL_MODE_ON_BUTTON,
        revealed=False,
    )

    assert card == escape_text("apple")


def test_special_characters_are_escaped() -> None:
    """Символы MarkdownV2 в слове и переводе экранируются."""
    word = make_word(word="rock'n'roll!", russian_translation="рок-н-ролл!")

    card = build_word_card(
        word, make_settings(*DEFAULT_FIELDS), reveal_mode=config.REVEAL_MODE_SPOILER
    )

    assert card.startswith("rock'n'roll\\!")
    assert "рок\\-н\\-ролл\\!" in card


def test_only_enabled_fields_are_shown() -> None:
    """Выключенные поля в карточку не попадают."""
    card = build_word_card(
        make_word(),
        make_settings(config.FIELD_DEFINITION),
        reveal_mode=config.REVEAL_MODE_SPOILER,
    )

    assert texts.LABEL_DEFINITION in card
    assert texts.LABEL_EXAMPLE not in card
    assert texts.LABEL_POS not in card
    assert texts.LABEL_RELATED not in card
    assert texts.LABEL_COLLOCATIONS not in card


def test_pos_and_related_forms_can_be_enabled() -> None:
    """Часть речи и Related Forms выводятся, если включены в настройках."""
    card = build_word_card(
        make_word(),
        make_settings(config.FIELD_POS, config.FIELD_RELATED, config.FIELD_DEFINITION),
        reveal_mode=config.REVEAL_MODE_SPOILER,
    )

    assert f"{texts.LABEL_POS} noun" in card
    assert f"{texts.LABEL_RELATED} apples" in card


def test_empty_optional_values_use_placeholder() -> None:
    """Пустые поля со заглушкой показывают ``—``, поля без заглушки скрываются."""
    card = build_word_card(
        make_word(synonyms=None, definition=None),
        make_settings(config.FIELD_SYNONYMS, config.FIELD_DEFINITION),
        reveal_mode=config.REVEAL_MODE_SPOILER,
    )

    assert f"{texts.LABEL_SYNONYMS} {config.EMPTY_VALUE_MARKER}" in card
    assert texts.LABEL_DEFINITION not in card


def test_word_without_translation_has_no_separator() -> None:
    """Если перевода нет, карточка начинается просто со слова."""
    card = build_word_card(
        make_word(russian_translation=None),
        make_settings(config.FIELD_DEFINITION),
        reveal_mode=config.REVEAL_MODE_SPOILER,
    )

    head, _, body = card.partition("\n\n")
    assert head == escape_text("apple")
    assert texts.LABEL_DEFINITION in body


def test_russian_lines_are_hidden_under_spoiler() -> None:
    """Русские переводы идут под своими английскими строками и всегда под спойлером.

    Спойлер остаётся даже в режиме «по кнопке», когда перевод слова уже показан:
    смысл определения сначала стоит вспомнить самому.
    """
    word = make_word(
        russian_definition="круглый плод",
        russian_example="Она съела яблоко.",
    )

    card = build_word_card(
        word, make_settings(*DEFAULT_FIELDS), reveal_mode=config.REVEAL_MODE_ON_BUTTON
    )

    definition = f"{texts.LABEL_RU} {spoiler(escape_text('круглый плод'))}"
    example = f"{texts.LABEL_RU} {spoiler(escape_text('Она съела яблоко.'))}"
    assert f"{texts.LABEL_DEFINITION} {escape_text(word.definition)}\n{definition}" in card
    assert f"{texts.LABEL_EXAMPLE} {escape_text(word.example)}\n{example}" in card


def test_russian_lines_are_absent_without_translation() -> None:
    """Пока перевода нет, карточка выглядит как раньше — без русских строк."""
    card = build_word_card(
        make_word(), make_settings(*DEFAULT_FIELDS), reveal_mode=config.REVEAL_MODE_ON_BUTTON
    )

    assert texts.LABEL_RU not in card


def test_russian_line_follows_its_own_field_switch() -> None:
    """Перевод примера не показывается, если само поле примера выключено."""
    word = make_word(
        russian_definition="круглый плод",
        russian_example="Она съела яблоко.",
    )

    card = build_word_card(
        word, make_settings(config.FIELD_EXAMPLE), reveal_mode=config.REVEAL_MODE_ON_BUTTON
    )

    assert escape_text("круглый плод") not in card
    assert escape_text("Она съела яблоко.") in card


def test_audio_caption_adds_spoiled_russian_translation() -> None:
    """В подписи к озвучке русский перевод идёт второй строкой под спойлером."""
    caption = build_audio_caption(
        "apple", config.AUDIO_ACCENT_UK, russian_translation="яблоко"
    )

    head = escape_text(texts.render_audio_caption("apple", config.AUDIO_ACCENT_UK))
    assert caption == f"{head}\n{texts.LABEL_RU} {spoiler(escape_text('яблоко'))}"


def test_audio_caption_without_translation_stays_short() -> None:
    """Без перевода подпись к озвучке — только слово и акцент."""
    caption = build_audio_caption("apple", config.AUDIO_ACCENT_UK)

    assert caption == escape_text(texts.render_audio_caption("apple", config.AUDIO_ACCENT_UK))
    assert texts.LABEL_RU not in caption


def test_refresh_card_adds_hint() -> None:
    """Карточка освежения отличается подсказкой про дневной лимит."""
    settings = make_settings(*DEFAULT_FIELDS)
    word = make_word()

    card = build_refresh_card(word, settings, reveal_mode=config.REVEAL_MODE_ON_BUTTON)

    assert card.endswith(escape_text(texts.REFRESH_HINT))
    assert card.startswith(
        build_word_card(word, settings, reveal_mode=config.REVEAL_MODE_ON_BUTTON)
    )


def test_refresh_card_without_reveal_has_no_hint() -> None:
    """До показа перевода освежение выглядит как обычное слово."""
    card = build_refresh_card(
        make_word(),
        make_settings(*DEFAULT_FIELDS),
        reveal_mode=config.REVEAL_MODE_ON_BUTTON,
        revealed=False,
    )

    assert card == escape_text("apple")


def test_with_answer_result_escapes_result_text() -> None:
    """Результат ответа дописывается отдельным абзацем и экранируется."""
    result = with_answer_result("card", "Ответ записан: через 1 дн. (этап 1).")

    assert result.startswith("card\n\n")
    assert result == f"card\n\n{escape_text('Ответ записан: через 1 дн. (этап 1).')}"

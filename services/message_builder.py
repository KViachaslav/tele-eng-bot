"""Сборка текста карточки слова (формат из раздела 5 ТЗ).

Перевод прячется спойлером Telegram (``||текст||``) или показывается по кнопке.
Все значения из CSV экранируются для MarkdownV2 — иначе символы вроде ``.``,
``-`` или ``!`` сломали бы разметку.
"""
from __future__ import annotations

from aiogram.utils.text_decorations import markdown_decoration

import config
import locales.ru as texts
from db.models import UserSettings, Word

#: Экранирование произвольного текста для MarkdownV2.
escape_text = markdown_decoration.quote

#: Обёртка спойлера Telegram: ``||текст||``.
spoiler = markdown_decoration.spoiler


def _line(label: str, value: str | None, *, placeholder: bool = False) -> str:
    """Строка карточки: ``label значение`` (или заглушка ``—``)."""
    if value:
        return f"{label} {escape_text(value)}"
    return f"{label} {config.EMPTY_VALUE_MARKER}" if placeholder else ""


def build_word_card(
    word: Word,
    settings: UserSettings,
    *,
    reveal_mode: str,
    revealed: bool = True,
) -> str:
    """Собирает текст сообщения со словом.

    :param word: словарная статья;
    :param settings: какие поля показывать пользователю;
    :param reveal_mode: ``spoiler`` (всё сразу) или ``on_button`` (слово, затем перевод);
    :param revealed: показывать ли перевод и остальные поля (для режима по кнопке);
    :return: готовый текст сообщения с разметкой MarkdownV2.
    """
    escaped_word = escape_text(word.word)

    if not revealed:
        # режим «сначала слово, потом перевод»: только слово и кнопка «Показать»
        return escaped_word

    separator = escape_text(config.WORD_TRANSLATION_SEPARATOR)
    if word.russian_translation:
        translation = escape_text(word.russian_translation)
        if reveal_mode == config.REVEAL_MODE_SPOILER:
            translation = spoiler(translation)
        head = f"{escaped_word}{separator}{translation}"
    else:
        head = escaped_word

    lines: list[str] = []
    if settings.show_definition:
        lines.append(_line(texts.LABEL_DEFINITION, word.definition))
    if settings.show_example:
        lines.append(_line(texts.LABEL_EXAMPLE, word.example))
    # Часть речи и Related Forms по умолчанию не выводятся (раздел 5 ТЗ),
    # но их можно включить в /settings — тогда строки добавляются.
    if settings.show_pos:
        lines.append(_line(texts.LABEL_POS, word.part_of_speech))
    if settings.show_related:
        lines.append(_line(texts.LABEL_RELATED, word.related_forms))
    if settings.show_collocations:
        lines.append(_line(texts.LABEL_COLLOCATIONS, word.collocations, placeholder=True))
    if settings.show_synonyms:
        lines.append(_line(texts.LABEL_SYNONYMS, word.synonyms, placeholder=True))
    if settings.show_antonyms:
        lines.append(_line(texts.LABEL_ANTONYMS, word.antonyms, placeholder=True))

    body = "\n".join(line for line in lines if line)
    return f"{head}\n\n{body}" if body else head


def build_refresh_card(
    word: Word,
    settings: UserSettings,
    *,
    reveal_mode: str,
    revealed: bool = True,
) -> str:
    """Карточка для освежения: тот же формат плюс пометка про дневной лимит."""
    card = build_word_card(word, settings, reveal_mode=reveal_mode, revealed=revealed)
    if not revealed:
        return card
    return f"{card}\n\n{escape_text(texts.REFRESH_HINT)}"


def with_answer_result(card: str, result_text: str) -> str:
    """Добавляет к карточке результат ответа (текст тоже экранируется)."""
    return f"{card}\n\n{escape_text(result_text)}"

"""Озвучка слов: поиск mp3 в ``data/<акцент>`` и отправка голосовым сообщением.

Словарь озвучен двумя акцентами (``data/uk`` и ``data/us``): на каждое слово по
файлу ``<слово>_<акцент>.mp3``. Кнопок в карточке тоже две — ``🇬🇧`` и ``🇺🇸``,
акцент приходит в ``callback_data`` (см. :class:`keyboards.callbacks.AudioCallback`).
Имена файлов не всегда совпадают с написанием слова в базе, поэтому каталог
акцента один раз сканируется и раскладывается в индекс по нормализованному ключу:

* регистр: поиск идёт по ключу в нижнем регистре, но если по ключу в каталоге
  есть несколько файлов, отличающихся регистром, побеждает тот, чьё написание
  совпадает со словом точь-в-точь. Это про коллизии имён: Windows не различает
  регистр, поэтому ``it_uk.mp3`` и ``IT_uk.mp3`` из архива распаковались как
  ``it_uk.mp3`` и ``IT_uk(1).mp3`` — слово «it» получает первый файл, а «March»,
  «May» и «IT» свои копии с хвостом «(1)»;
* разделители: пробел, дефис, подчёркивание и апостроф считаются одним и тем же
  («ice cream_uk.mp3» находится и как «ice-cream», и как ``ice_cream``), но
  число слов должно совпадать: «work out» — это не ``workout_uk.mp3``, а другое
  слово Oxford 3000 (у него в архиве своего файла нет).

Озвучен не весь словарь (в Oxford 3000 есть словосочетания и формы, которых в
архиве нет), поэтому :func:`find_audio` возвращает ``None`` — хендлер в этом
случае отвечает пользователю алертом, а чужое слово не подставляет.

Файл уходит голосовым сообщением (``sendVoice``): в чате видно сообщение с
волной, которое можно послушать сразу, не открывая вложение. Telegram сам
конвертирует mp3 из архива в формат голосовых (OGG/OPUS), поэтому ни ffmpeg, ни
второй копии файлов на диске не нужно; акцент указан в подписи. Если у слова уже
есть русский перевод, он добавляется в подпись второй строкой под спойлером
(``services/message_builder.build_audio_caption``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from aiogram.exceptions import TelegramAPIError
from aiogram.types import FSInputFile, MaybeInaccessibleMessage, Message
from loguru import logger

import config
from services.message_builder import build_audio_caption

#: Хвост «(1)», «(2)» в имени файла — так распаковка архива пометила повторы.
DUPLICATE_SUFFIX = re.compile(r"\(\d+\)$")

#: Разделители внутри слова (``T-shirt`` → ``t shirt``, ``ice_cream`` → ``ice cream``).
SEPARATORS = re.compile(r"[\s\-_'’]+")

#: Расширение файлов озвучки.
FILE_SUFFIX = ".mp3"


@dataclass(frozen=True)
class Entry:
    """Файл озвучки в каталоге акцента."""

    path: Path
    #: Написание слова в имени файла — может отличаться от базы регистром.
    spelling: str
    #: Имя пришло с хвостом «(1)»: обычное написание имеет приоритет.
    duplicate: bool


def normalize(name: str) -> str:
    """Ключ слова: нижний регистр и одинаковые разделители.

    Регистр и вид разделителей в именах файлов архива не совпадают с базой
    («Internet», «TV», «ice cream», «T-shirt»), поэтому и слово, и имя файла
    приводятся к одному виду.
    """
    return " ".join(SEPARATORS.sub(" ", name.lower()).split())


@lru_cache(maxsize=8)
def _index(directory: Path, accent: str) -> dict[str, tuple[Entry, ...]]:
    """Индекс «ключ → файлы каталога» для ``<directory>/<акцент>``.

    :param directory: корневой каталог озвучки (``data``), внутри — подкаталоги
        акцентов ``uk`` и ``us``.
    :param accent: имя подкаталога акцента.
    Каталог акцента содержит около 4956 файлов, поэтому список читается один раз
    и кэшируется: поиск по словарю идёт уже по индексу. По одному ключу может
    оказаться несколько файлов — так вышло с коллизиями регистра (``it`` /
    ``IT``, ``March`` / ``march``), поэтому сохраняются все варианты написания.
    """
    suffix = f"_{accent}"
    accent_dir = directory / accent
    index: dict[str, list[Entry]] = {}
    if not accent_dir.is_dir():
        logger.warning("Каталог озвучки {} ({}) не найден", accent_dir, accent)
        return {}

    for path in sorted(accent_dir.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.suffix != FILE_SUFFIX:
            continue
        # Хвост «(1)» сначала убирается из имени, иначе ``IT_uk(1).mp3``
        # не похоже на файл акцента ``IT_uk.mp3``.
        stem, marked = DUPLICATE_SUFFIX.subn("", path.stem)
        if not stem.endswith(suffix):
            continue
        word = stem[: -len(suffix)]
        if not word:
            continue
        entry = Entry(path=path, spelling=" ".join(word.split()), duplicate=bool(marked))
        index.setdefault(normalize(word), []).append(entry)

    logger.debug("Озвучка {}: {} ключей из {}", accent, len(index), accent_dir)
    return {key: tuple(entries) for key, entries in index.items()}


def find_audio(word: str, accent: str, *, directory: Path | None = None) -> Path | None:
    """Файл озвучки слова для конкретного акцента или ``None``, если его нет.

    Кандидаты по ключу перебираются по приоритету: сначала файл, написание
    которого совпадает со словом точь-в-точь (так «March» получает
    ``March_uk(1).mp3``, а «it» — ``it_uk.mp3``), затем обычное имя без хвоста
    «(1)», затем любая копия.

    :param directory: каталог с подкаталогами акцентов (по умолчанию
        :data:`config.AUDIO_DIR`).
    """
    entries = _index(directory or config.AUDIO_DIR, accent).get(normalize(word), ())
    if not entries:
        return None
    spelling = " ".join(word.split())
    # Сначала файлы с точным написанием слова: из-за коллизий регистра по одному
    # ключу лежат, например, ``it_uk.mp3`` и ``IT_uk(1).mp3``. Внутри группы и
    # дальше выигрывает имя без хвоста «(1)» — копию оставляем на случай, когда
    # обычного имени в архиве нет вовсе.
    exact = [entry for entry in entries if entry.spelling == spelling]
    for group in (exact, entries):
        for entry in group:
            if not entry.duplicate:
                return entry.path
        if group:
            return group[0].path
    return None


def clear_cache() -> None:
    """Сбрасывает кэш индексов (после добавления файлов или в тестах)."""
    _index.cache_clear()


async def send_word_audio(
    message: MaybeInaccessibleMessage | None,
    word: str,
    path: Path,
    accent: str,
    *,
    russian_translation: str | None = None,
) -> bool:
    """Отправляет озвучку голосовым сообщением в тот же чат, где нажали кнопку.

    Файл уходит как голосовое (``sendVoice``), а не как аудиофайл: у голосового
    нет полей ``title``/``performer``, зато оно играет прямо в чате. Telegram сам
    конвертирует mp3 в OGG/OPUS. Подпись сообщает акцент, потому что в самом
    сообщении больше нечего показать кроме волны; если у слова есть русский
    перевод, он идёт второй строкой под спойлером (см.
    :func:`services.message_builder.build_audio_caption`) — послушав слово, можно
    подсмотреть смысл, когда он нужен.

    :param russian_translation: русский перевод слова (или ``None``) — берётся из
        словарной статьи и не зависит от настроек карточки: перевод показывается
        всегда, в отличие от поля «определение».
    :return: ``False``, если сообщение недоступно или Telegram отклонил файл —
        тогда вызывающий код отвечает пользователю алертом.
    """
    if not isinstance(message, Message):
        logger.warning("Озвучка {!r}: сообщение недоступно для отправки", word)
        return False
    try:
        await message.answer_voice(
            FSInputFile(path),
            caption=build_audio_caption(word, accent, russian_translation=russian_translation),
            parse_mode=config.PARSE_MODE,
        )
    except TelegramAPIError:
        logger.exception("Не удалось отправить озвучку {!r} ({})", word, path)
        return False
    return True

"""Загрузка словаря Oxford 3000 из CSV.

Модуль устойчив к кодировкам (UTF-8, UTF-8-BOM, cp1251) и к запятым внутри
значений — разбор идёт штатным :mod:`csv` с поддержкой кавычек. Пустые значения
(включая строки-заглушки вроде ``none``) сохраняются как ``None`` и не выводятся
в сообщении.
"""
from __future__ import annotations

import codecs
import csv
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from loguru import logger

from database import session_scope
from db.repository import upsert_words

#: Соответствие колонок CSV и полей модели ``Word``.
CSV_FIELD_MAP: dict[str, str] = {
    "Word": "word",
    "Definition": "definition",
    "Russian Translation": "russian_translation",
    "Example Sentence": "example",
    "Part of Speech": "part_of_speech",
    "Related Forms": "related_forms",
    "Synonyms": "synonyms",
    "Antonyms": "antonyms",
    "Collocations": "collocations",
}
WORD_FIELD = "word"
OPTIONAL_FIELDS = tuple(name for name in CSV_FIELD_MAP.values() if name != WORD_FIELD)

#: Значения, которые считаются пустыми.
EMPTY_TOKENS = frozenset({"", "-", "—", "–", "none", "null", "nan", "n/a", "na"})

#: Кодировки, которые пробуются по порядку при чтении файла.
CANDIDATE_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")

#: Запасной вариант, если файл не удалось прочитать ни одной из кодировок.
FALLBACK_ENCODING = "cp1251"


class CsvFormatError(ValueError):
    """Файл не похож на ожидаемый CSV (нет колонки ``Word``)."""


@dataclass(frozen=True, slots=True)
class WordRecord:
    """Одна словарная статья, готовая к записи в БД."""

    word: str
    definition: str | None = None
    russian_translation: str | None = None
    example: str | None = None
    part_of_speech: str | None = None
    related_forms: str | None = None
    synonyms: str | None = None
    antonyms: str | None = None
    collocations: str | None = None

    def as_row(self) -> dict[str, object]:
        """Представление записи в виде словаря для INSERT."""
        return asdict(self)


@dataclass(slots=True)
class ParsedCsv:
    """Результат разбора файла."""

    records: list[WordRecord] = field(default_factory=list)
    total_rows: int = 0
    duplicates: int = 0
    invalid: int = 0
    encoding: str = ""

    @property
    def unique_count(self) -> int:
        """Количество уникальных слов в файле."""
        return len(self.records)


@dataclass(slots=True)
class ImportResult:
    """Итог импорта CSV в базу."""

    total_rows: int = 0
    parsed: int = 0
    imported: int = 0
    skipped: int = 0
    duplicates: int = 0
    invalid: int = 0
    encoding: str = ""


def normalize_value(value: str | None) -> str | None:
    """Приводит значение из CSV к ``None`` или нормализованной строке."""
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not cleaned or cleaned.casefold() in EMPTY_TOKENS:
        return None
    return cleaned


def detect_encoding(path: Path) -> str:
    """Определяет кодировку файла: UTF-8 (с BOM или без) либо cp1251."""
    with path.open("rb") as binary:
        head = binary.read(4)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"

    for encoding in CANDIDATE_ENCODINGS:
        try:
            with path.open("r", encoding=encoding, newline="") as handle:
                handle.read()
        except UnicodeDecodeError:
            continue
        return encoding

    logger.warning("Не удалось определить кодировку {} — беру {}", path.name, FALLBACK_ENCODING)
    return FALLBACK_ENCODING


def build_header_mapping(fieldnames: Sequence[str] | None) -> dict[str, str]:
    """Сопоставляет колонки файла с полями модели (регистр и BOM не важны)."""
    lookup = {name.casefold(): target for name, target in CSV_FIELD_MAP.items()}
    mapping: dict[str, str] = {}
    for header in fieldnames or ():
        if header is None:
            continue
        key = header.strip().lstrip("\ufeff").casefold()
        target = lookup.get(key)
        if target is not None:
            mapping[header] = target
    if WORD_FIELD not in mapping.values():
        raise CsvFormatError(
            "В CSV не найдена колонка 'Word'. Ожидаемые колонки: " + ", ".join(CSV_FIELD_MAP)
        )
    return mapping


def _read_row(raw_row: Mapping[str, str | None], mapping: Mapping[str, str]) -> dict[str, str | None]:
    """Преобразует строку CSV в поля модели ``Word``."""
    values: dict[str, str | None] = {}
    for header, target in mapping.items():
        values[target] = normalize_value(raw_row.get(header))
    return values


def iter_word_records(path: Path, encoding: str | None = None) -> Iterator[WordRecord]:
    """Итерирует валидные словарные статьи (дубли не отфильтровываются)."""
    selected = encoding or detect_encoding(path)
    with path.open("r", encoding=selected, newline="", errors="replace") as handle:
        reader = csv.DictReader(handle)
        mapping = build_header_mapping(reader.fieldnames)
        for raw_row in reader:
            values = _read_row(raw_row, mapping)
            word = values.get(WORD_FIELD)
            if not word:
                continue
            yield WordRecord(**values)  # type: ignore[arg-type]


def parse_csv_file(path: Path, encoding: str | None = None) -> ParsedCsv:
    """Разбирает CSV целиком: считает строки, дубли и записи без слова."""
    parsed = ParsedCsv(encoding=encoding or detect_encoding(path))
    seen: set[str] = set()

    with path.open("r", encoding=parsed.encoding, newline="", errors="replace") as handle:
        reader = csv.DictReader(handle)
        mapping = build_header_mapping(reader.fieldnames)
        for raw_row in reader:
            parsed.total_rows += 1
            values = _read_row(raw_row, mapping)
            word = values.get(WORD_FIELD)
            if not word:
                parsed.invalid += 1
                continue
            key = word.casefold()
            if key in seen:
                parsed.duplicates += 1
                continue
            seen.add(key)
            parsed.records.append(WordRecord(**values))  # type: ignore[arg-type]

    return parsed


async def import_csv_file(path: Path, encoding: str | None = None) -> ImportResult:
    """Импортирует файл в БД. Повторный импорт не создаёт дубликатов."""
    parsed = parse_csv_file(path, encoding)
    result = ImportResult(
        total_rows=parsed.total_rows,
        parsed=parsed.unique_count,
        duplicates=parsed.duplicates,
        invalid=parsed.invalid,
        encoding=parsed.encoding,
    )

    rows: list[Mapping[str, object]] = [record.as_row() for record in parsed.records]
    if not rows:
        logger.warning("В файле {} не найдено ни одного слова", path)
        return result

    async with session_scope() as session:
        upsert = await upsert_words(session, rows)

    result.imported = upsert.imported
    result.skipped = upsert.skipped
    logger.info(
        "Импорт {}: строк {}, добавлено {}, пропущено {}, дублей {}, пустых {}",
        path.name,
        result.total_rows,
        result.imported,
        result.skipped,
        result.duplicates,
        result.invalid,
    )
    return result


async def ensure_words_imported(csv_path: Path) -> ImportResult | None:
    """Автоматически загружает словарь, если таблица ``words`` пуста."""
    from db.repository import count_words

    async with session_scope() as session:
        words_in_db = await count_words(session)

    if words_in_db > 0:
        logger.info("В базе уже {} слов — автоимпорт не нужен", words_in_db)
        return None

    if not csv_path.exists():
        logger.error("Файл словаря не найден: {} — автоимпорт пропущен", csv_path)
        return None

    logger.info("Таблица words пуста — запускаю автоимпорт из {}", csv_path)
    return await import_csv_file(csv_path)

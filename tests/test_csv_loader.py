"""Тесты разбора и импорта словаря Oxford 3000 (services/csv_loader.py)."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

import pytest

import config
from db import repository
from services import csv_loader

#: Шапка CSV берётся из самого загрузчика, чтобы тест ловил смену формата.
HEADER_FIELDS = list(csv_loader.CSV_FIELD_MAP)

ROW_APPLE = [
    "apple",
    "a round fruit",
    "яблоко",
    "I ate an apple, quickly.",
    "noun",
    "apples",
    "fruit",
    "",
    "green apple",
]
ROW_QUICKLY = [
    "quickly",
    "at a fast speed",
    "быстро",
    "He ran quickly.",
    "adverb",
    "quick",
    "fast",
    "slowly",
    "",
]
ROW_WITHOUT_WORD = ["", "описание", "", "", "", "", "", "", ""]
ROW_DUPLICATE = ["Apple", "", "", "", "", "", "", "", ""]


def write_csv(
    dir_path: Path,
    rows: Sequence[Sequence[str]],
    *,
    encoding: str = "utf-8",
) -> Path:
    """Пишет файл с шапкой Oxford 3000 и переданными строками."""
    path = dir_path / "words.csv"
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADER_FIELDS)
        writer.writerows(rows)
    return path


@pytest.mark.parametrize("raw", ["", "   ", "-", "—", "none", "NONE", "n/a", "NaN"])
def test_normalize_value_treats_tokens_as_empty(raw: str) -> None:
    """Строки-заглушки из CSV превращаются в ``None``."""
    assert csv_loader.normalize_value(raw) is None


def test_normalize_value_none_and_whitespace() -> None:
    """``None`` остаётся ``None``, лишние пробелы схлопываются."""
    assert csv_loader.normalize_value(None) is None
    assert csv_loader.normalize_value("  a   round   fruit ") == "a round fruit"


def test_detect_encoding_utf8_bom(tmp_path: Path) -> None:
    """Файл с BOM распознаётся как ``utf-8-sig``."""
    path = write_csv(tmp_path, [ROW_APPLE], encoding="utf-8-sig")
    assert csv_loader.detect_encoding(path) == "utf-8-sig"


def test_detect_encoding_plain_utf8(tmp_path: Path) -> None:
    """Обычный UTF-8 читается одной из совместимых кодировок."""
    path = write_csv(tmp_path, [ROW_APPLE], encoding="utf-8")
    assert csv_loader.detect_encoding(path) in {"utf-8", "utf-8-sig"}


def test_detect_encoding_cp1251(tmp_path: Path) -> None:
    """Файл в cp1251 не декодируется как UTF-8 и определяется верно."""
    path = write_csv(tmp_path, [ROW_APPLE], encoding="cp1251")
    assert csv_loader.detect_encoding(path) == "cp1251"


def test_parse_csv_file_counts_rows(tmp_path: Path) -> None:
    """Считаются общие строки, дубли и строки без слова."""
    path = write_csv(tmp_path, [ROW_APPLE, ROW_DUPLICATE, ROW_WITHOUT_WORD])

    parsed = csv_loader.parse_csv_file(path)

    assert parsed.total_rows == 3
    assert parsed.duplicates == 1
    assert parsed.invalid == 1
    assert parsed.unique_count == 1
    assert parsed.encoding in {"utf-8", "utf-8-sig"}


def test_parse_csv_file_reads_quoted_values(tmp_path: Path) -> None:
    """Запятые внутри кавычек не ломают разбор, пустые значения дают ``None``."""
    path = write_csv(tmp_path, [ROW_APPLE])

    record = csv_loader.parse_csv_file(path).records[0]

    assert record.word == "apple"
    assert record.definition == "a round fruit"
    assert record.russian_translation == "яблоко"
    assert record.example == "I ate an apple, quickly."
    assert record.part_of_speech == "noun"
    assert record.related_forms == "apples"
    assert record.synonyms == "fruit"
    assert record.antonyms is None
    assert record.collocations == "green apple"
    assert record.as_row()["word"] == "apple"


def test_parse_csv_file_normalizes_whitespace(tmp_path: Path) -> None:
    """Многократные пробелы внутри значений схлопываются."""
    row = ["  green   apple ", " a  fruit ", "", "", "noun", "", "", "", ""]
    path = write_csv(tmp_path, [row])

    record = csv_loader.parse_csv_file(path).records[0]

    assert record.word == "green apple"
    assert record.definition == "a fruit"


def test_parse_csv_file_requires_word_column(tmp_path: Path) -> None:
    """Файл без колонки ``Word`` отвергается."""
    path = tmp_path / "bad.csv"
    path.write_text("Term,Translation\napple,яблоко\n", encoding="utf-8")

    with pytest.raises(csv_loader.CsvFormatError):
        csv_loader.parse_csv_file(path)


def test_iter_word_records_keeps_duplicates(tmp_path: Path) -> None:
    """Потоковый разбор отдаёт все строки со словом, включая дубли."""
    path = write_csv(tmp_path, [ROW_APPLE, ROW_DUPLICATE, ROW_WITHOUT_WORD])

    records = list(csv_loader.iter_word_records(path))

    assert [record.word for record in records] == ["apple", "Apple"]


def test_real_dictionary_file_parses() -> None:
    """Реальный CSV Oxford 3000 разбирается без «пустых» строк."""
    path = config.DEFAULT_CSV_PATH
    if not path.exists():
        pytest.skip(f"файл словаря не найден: {path}")

    parsed = csv_loader.parse_csv_file(path)

    assert parsed.total_rows > 3000
    assert parsed.duplicates == 0
    # В реальном словаре встречается одна строка без слова — она уходит в invalid.
    assert parsed.invalid == 1
    assert parsed.unique_count == parsed.total_rows - parsed.invalid - parsed.duplicates
    assert all(record.word for record in parsed.records)


async def test_import_csv_file_into_db(db, session, tmp_path: Path) -> None:
    """Импорт пишет слова в БД, повторный импорт их пропускает."""
    path = write_csv(tmp_path, [ROW_APPLE, ROW_QUICKLY])

    result = await csv_loader.import_csv_file(path)

    assert result.total_rows == 2
    assert result.parsed == 2
    assert (result.imported, result.skipped) == (2, 0)
    assert await repository.count_words(session) == 2

    again = await csv_loader.import_csv_file(path)

    assert (again.imported, again.skipped) == (0, 2)
    assert await repository.count_words(session) == 2


async def test_import_csv_file_without_valid_rows(db, session, tmp_path: Path) -> None:
    """Файл без слов не ломает импорт."""
    path = write_csv(tmp_path, [ROW_WITHOUT_WORD])

    result = await csv_loader.import_csv_file(path)

    assert (result.imported, result.parsed, result.invalid) == (0, 0, 1)
    assert await repository.count_words(session) == 0


async def test_ensure_words_imported_fills_empty_db(db, session, tmp_path: Path) -> None:
    """Автоимпорт срабатывает только на пустой таблице ``words``."""
    path = write_csv(tmp_path, [ROW_APPLE])

    first = await csv_loader.ensure_words_imported(path)

    assert first is not None
    assert first.imported == 1
    assert await csv_loader.ensure_words_imported(path) is None
    assert await repository.count_words(session) == 1


async def test_ensure_words_imported_skips_missing_file(db, session, tmp_path: Path) -> None:
    """Отсутствующий файл словаря — автоимпорт молча пропускается."""
    assert await csv_loader.ensure_words_imported(tmp_path / "missing.csv") is None
    assert await repository.count_words(session) == 0



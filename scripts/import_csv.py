"""Ручной импорт словаря Oxford 3000 из CSV в базу бота.

Запуск из корня проекта::

    python scripts/import_csv.py                     # файл из CSV_PATH (.env)
    python scripts/import_csv.py data/oxford3000.csv # свой файл
    python -m scripts.import_csv                     # то же самое

Импорт идемпотентен: уже загруженные слова пропускаются, дубликаты внутри файла
и строки без слова считаются отдельно.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Скрипт лежит в scripts/, поэтому корень проекта добавляем в sys.path вручную.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger  # noqa: E402

import config  # noqa: E402
import locales.ru as texts  # noqa: E402
from database import dispose_database, init_database  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from services import csv_loader  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description=texts.IMPORT_CLI_DESCRIPTION,
        epilog=texts.IMPORT_CLI_USAGE,
    )
    parser.add_argument("path", nargs="?", help=texts.IMPORT_CLI_PATH_HELP)
    parser.add_argument("--encoding", help=texts.IMPORT_CLI_ENCODING_HELP)
    return parser.parse_args(argv)


async def import_words(path: Path, encoding: str | None) -> int:
    """Импортирует файл и возвращает код возврата процесса."""
    if not path.exists():
        logger.error(texts.IMPORT_FILE_NOT_FOUND.format(path=path))
        return 1

    logger.info(texts.IMPORT_STARTED.format(path=path))
    try:
        result = await csv_loader.import_csv_file(path, encoding)
    except csv_loader.CsvFormatError as error:
        logger.error(texts.IMPORT_CLI_BAD_FORMAT.format(error=error))
        return 1

    logger.info(
        texts.IMPORT_FINISHED.format(
            total=result.total_rows,
            imported=result.imported,
            skipped=result.skipped,
            duplicates=result.duplicates,
            invalid=result.invalid,
        )
    )
    return 0


async def run(argv: list[str] | None = None) -> int:
    """Готовит настройки, базу и запускает импорт."""
    args = parse_args(argv)
    settings = config.get_settings()
    setup_logging(settings)
    await init_database(settings.database_path)
    try:
        path = Path(args.path).expanduser() if args.path else settings.dataset_path
        return await import_words(path, args.encoding)
    finally:
        await dispose_database()


def main() -> None:
    """Точка входа скрипта."""
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

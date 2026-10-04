"""Пакетный перевод словаря на русский: определения и примеры (MyMemory API).

Запуск из корня проекта::

    python scripts/translate_words.py --dry-run                  # сколько осталось
    python scripts/translate_words.py --limit 500 --email you@example.com
    python -m scripts.translate_words --limit 500                # то же самое

Зачем порциями: у бесплатного MyMemory суточный лимит считают в символах —
~5000 без указанной почты и ~50000 с ней (``MYMEMORY_EMAIL`` или ``--email``), а
словарю Oxford 3000 нужно ~318000 символов (3353 слова × ≈95 символов на слово).
Скрипт переводит только то, где русского текста ещё нет, сохраняет каждое слово
отдельно и останавливается сам, когда сервис сказал «на сегодня всё»: запускайте
его раз в день, пока ``--dry-run`` не покажет ноль.

Русские строки ложатся в ``words.russian_definition`` / ``words.russian_example``
и потом показываются в карточке под спойлером — в сеть бот за ними уже не ходит.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Скрипт лежит в scripts/, поэтому корень проекта добавляем в sys.path вручную.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

import config  # noqa: E402
import locales.ru as texts  # noqa: E402
from database import dispose_database, init_database, session_scope  # noqa: E402
from db import repository  # noqa: E402
from db.models import TRANSLATION_BOTH, TRANSLATION_FIELD_CHOICES  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from services import translator  # noqa: E402

#: Пользователь получил свою порцию переводов.
EXIT_OK = 0
#: Сервис не отвечает (обычно исчерпан суточный лимит) — запустить снова позже.
EXIT_QUOTA = 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбирает аргументы командной строки."""
    parser = argparse.ArgumentParser(
        description=texts.TRANSLATE_CLI_DESCRIPTION,
        epilog=texts.TRANSLATE_CLI_USAGE,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=config.TRANSLATION_BATCH_LIMIT,
        help=texts.TRANSLATE_CLI_LIMIT_HELP.format(limit=config.TRANSLATION_BATCH_LIMIT),
    )
    parser.add_argument(
        "--field",
        choices=TRANSLATION_FIELD_CHOICES,
        default=TRANSLATION_BOTH,
        help=texts.TRANSLATE_CLI_FIELD_HELP,
    )
    parser.add_argument(
        "--sleep",
        dest="sleep_seconds",
        type=float,
        default=config.TRANSLATION_BATCH_SLEEP_SECONDS,
        help=texts.TRANSLATE_CLI_SLEEP_HELP,
    )
    parser.add_argument("--email", help=texts.TRANSLATE_CLI_EMAIL_HELP)
    parser.add_argument(
        "--max-failures",
        dest="max_failures",
        type=int,
        default=config.TRANSLATION_BATCH_MAX_FAILURES,
        help=texts.TRANSLATE_CLI_MAX_FAILURES_HELP,
    )
    parser.add_argument(
        "--learning-first",
        action="store_true",
        help=texts.TRANSLATE_CLI_LEARNING_FIRST_HELP,
    )
    parser.add_argument("--dry-run", action="store_true", help=texts.TRANSLATE_CLI_DRY_RUN_HELP)
    return parser.parse_args(argv)


def apply_email(email: str | None) -> None:
    """Ставит почту для MyMemory из ``--email`` в переменные окружения.

    Параметр ``de`` поднимает суточный лимит сервиса с ~5000 до ~50000 символов;
    переменная окружения перекрывает значение из ``.env`` (см. :mod:`config`),
    поэтому её достаточно выставить до первого :func:`config.get_settings`.
    """
    if email:
        os.environ["MYMEMORY_EMAIL"] = email


def daily_char_limit(settings: config.Settings) -> int:
    """Суточный лимит MyMemory в символах: с указанной почтой он в десять раз больше."""
    if settings.mymemory_email:
        return config.MYMEMORY_DAILY_CHARS_WITH_EMAIL
    return config.MYMEMORY_DAILY_CHARS_ANONYMOUS


async def report(
    session: AsyncSession, *, field: str, settings: config.Settings
) -> repository.TranslationPending:
    """Печатает, сколько ещё переводить (режим ``--dry-run``), и отдаёт цифры."""
    pending = await repository.pending_translations(session, field=field)
    if not pending.words:
        logger.info(texts.TRANSLATE_NOTHING_TO_DO.format(field=field))
        return pending

    daily = daily_char_limit(settings)
    logger.info(
        texts.TRANSLATE_DRY_RUN.format(
            words=pending.words,
            field=field,
            chars=pending.chars,
            days=round(pending.chars / daily, 1),
            daily=daily,
        )
    )
    return pending


async def translate(
    session: AsyncSession, args: argparse.Namespace
) -> translator.TranslationBatchResult:
    """Переводит одну порцию слов и возвращает её итог.

    ``--limit 0`` означает «без ограничения» (весь остаток словаря).
    """
    limit = None if args.limit <= 0 else args.limit
    return await translator.translate_pending_words(
        session,
        limit=limit,
        field=args.field,
        sleep_seconds=args.sleep_seconds,
        max_failures=args.max_failures,
        learning_first=args.learning_first,
    )


async def run(argv: list[str] | None = None) -> int:
    """Готовит настройки, базу и запускает порцию пакетного перевода."""
    args = parse_args(argv)
    apply_email(args.email)
    settings = config.get_settings()
    setup_logging(settings)

    if not translator.is_enabled():
        logger.warning(texts.TRANSLATE_DISABLED)
        return EXIT_OK

    await init_database(settings.database_path)
    try:
        async with session_scope() as session:
            if args.dry_run:
                await report(session, field=args.field, settings=settings)
                return EXIT_OK
            result = await translate(session, args)
    finally:
        await dispose_database()

    if result.quota_stopped:
        logger.warning(texts.TRANSLATE_QUOTA_STOPPED)
        return EXIT_QUOTA

    logger.info(
        texts.TRANSLATE_FINISHED.format(
            words=result.words,
            definitions=result.definitions,
            examples=result.examples,
            failed=result.failed,
            left=result.left,
        )
    )
    return EXIT_OK


def main() -> None:
    """Точка входа скрипта."""
    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()

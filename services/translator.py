"""Перевод определения и примера на русский (MyMemory API).

Словарь Oxford 3000 англоязычный: в карточке определение и пример приходят на
английском. Чтобы под ними можно было показать русскую версию, а после ответа —
проверить себя, бот переводит эти строки через бесплатный MyMemory
(``https://api.mymemory.translated.net/get``) и складывает результат в
``words.russian_definition`` и ``words.russian_example``
(:func:`ensure_word_translations`). В карточке и в подписи к озвучке русские
строки всегда идут под спойлером Telegram (``||...||``): подсмотреть перевод
можно, но только осознанно — «сначала вспомни сам» сохраняется.

Сеть — внешняя зависимость, поэтому модуль устроен осторожно:

* перевод запрашивается только у того слова, где русского текста ещё нет
  (``definition`` / ``example`` заполнены, а их русские версии — нет): второй раз
  то же слово берётся из БД;
* ошибки наружу не поднимаются: недоступный сервис, HTTP-ошибка, ответ без
  перевода, слишком длинная строка — всё это ``None``, и карточка выходит как
  раньше, без русских строк, но с английскими;
* после первой неудачи включается пауза
  (:data:`config.TRANSLATION_FAILURE_COOLDOWN_SECONDS`), иначе каждая отправка
  ждала бы таймаута сети (например, у сервера нет доступа в интернет);
* отключается целиком через ``TRANSLATION_ENABLED=false`` в ``.env``.

Переводы двух строк одного слова идут последовательно: MyMemory — бесплатный
сервис с ограничением на частоту запросов, и параллельные обращения к нему
рискуют получить отказ.

Дневной лимит сервиса измеряется в символах (~5000 в сутки без указанной почты и
~50000 с ней), а словарю нужно несколько сотен тысяч символов. Поэтому кроме
перевода «на месте» есть пакетный режим (:func:`translate_pending_words` и
``scripts/translate_words.py``): он добирает русский текст порциями и
останавливается сам, когда сервис сказал «на сегодня всё».
"""
from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
import locales.ru as texts
from db import repository
from db.models import TRANSLATION_BOTH, Word, translation_field_pairs

#: Признаки ответа-ошибки: MyMemory кладёт предупреждение туда же, где перевод
#: (``responseData.translatedText``), поэтому текст проверяется дополнительно.
ERROR_MARKERS = (
    "MYMEMORY WARNING",
    "PLEASE SELECT TWO DISTINCT LANGUAGES",
    "INVALID LANGUAGE PAIR",
    "QUERY LENGTH LIMIT EXCEEDED",
)

#: Сколько символов строки показывать в логе (сами строки из словаря длинные).
LOG_TEXT_LIMIT = 80

#: Момент (монотонное время), до которого перевод не запрашивается.
_cooldown_until: float = 0.0


def is_enabled() -> bool:
    """Включён ли перевод (``TRANSLATION_ENABLED`` в ``.env``, по умолчанию — да)."""
    return bool(config.get_settings().translation_enabled)


def langpair() -> str:
    """Языковая пара для запроса MyMemory (``en|ru``)."""
    return f"{config.TRANSLATION_SOURCE_LANG}|{config.TRANSLATION_TARGET_LANG}"


def in_cooldown() -> bool:
    """Идёт ли пауза после неудачного запроса."""
    return time.monotonic() < _cooldown_until


def start_cooldown() -> None:
    """Ставит паузу: пока она идёт, сеть не трогаем."""
    global _cooldown_until
    _cooldown_until = time.monotonic() + config.TRANSLATION_FAILURE_COOLDOWN_SECONDS


def reset_cooldown() -> None:
    """Сбрасывает паузу (нужна тестам и ручной проверке перевода)."""
    global _cooldown_until
    _cooldown_until = 0.0


async def request_translation(text: str, *, timeout: float | None = None) -> dict[str, Any] | None:
    """Запрашивает перевод строки у MyMemory.

    :param timeout: сколько секунд ждать ответ; ``None`` — карточный лимит
        :data:`config.TRANSLATION_TIMEOUT_SECONDS` (пакетный перевод ждёт дольше,
        см. :data:`config.TRANSLATION_BATCH_TIMEOUT_SECONDS`).
    :return: разобранный JSON или ``None``, если сервис недоступен (сеть,
        таймаут, ответ не 200 или не JSON) — причина попадает в лог.
    """
    settings = config.get_settings()
    params: dict[str, str] = {"q": text, "langpair": langpair()}
    if settings.mymemory_email:
        # С указанной почтой сервис даёт больше переводов в сутки.
        params["de"] = settings.mymemory_email

    limit = config.TRANSLATION_TIMEOUT_SECONDS if timeout is None else timeout
    client_timeout = aiohttp.ClientTimeout(total=limit)
    try:
        async with aiohttp.ClientSession(timeout=client_timeout) as client:
            async with client.get(config.TRANSLATION_API_URL, params=params) as response:
                if response.status != 200:
                    logger.warning("MyMemory ответил кодом {}", response.status)
                    return None
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError) as error:
        logger.warning("MyMemory недоступен: {} ({})", error, type(error).__name__)
        return None
    return payload if isinstance(payload, dict) else None


def parse_translation(payload: Mapping[str, Any] | None) -> str | None:
    """Перевод из ответа MyMemory или ``None``, если перевода в ответе нет.

    Ответ без перевода (``responseStatus`` не 200, пустой ``translatedText``,
    предупреждение сервиса вместо текста) переводом не считается.
    """
    if not payload:
        return None
    if payload.get("responseStatus") not in (200, "200"):
        return None
    data = payload.get("responseData")
    if not isinstance(data, Mapping):
        return None
    text = str(data.get("translatedText") or "").strip()
    if not text:
        return None
    if any(marker in text.upper() for marker in ERROR_MARKERS):
        logger.warning("MyMemory вернул предупреждение вместо перевода: {!r}", text)
        return None
    return text


async def translate_text(
    source: str | None,
    *,
    timeout: float | None = None,
    cooldown: bool = True,
) -> str | None:
    """Переводит одну строку.

    :param timeout: сколько секунд ждать ответ сервиса; ``None`` — карточный
        лимит :data:`config.TRANSLATION_TIMEOUT_SECONDS` (пакетный перевод ждёт
        дольше: :data:`config.TRANSLATION_BATCH_TIMEOUT_SECONDS`).
    :param cooldown: ставить ли общую паузу после неудачи. Карточке она нужна
        (иначе каждая отправка ждала бы таймаута сети), а пакетному переводу —
        нет: он сам останавливается после нескольких неудач подряд, и одиночный
        сбой сети не должен стоить всей порции.
    :return: русский текст или ``None``, если переводить нечего (пустая строка,
        строка длиннее :data:`config.TRANSLATION_TEXT_LIMIT`) либо сервис не
        ответил — тогда включается пауза (см. :func:`start_cooldown`).
    """
    text = " ".join((source or "").split())
    if not text:
        return None
    if len(text) > config.TRANSLATION_TEXT_LIMIT:
        logger.warning(
            "Строка в {} символов не влезает в запрос MyMemory: {!r}",
            len(text),
            text[:LOG_TEXT_LIMIT],
        )
        return None

    translated = parse_translation(await request_translation(text, timeout=timeout))
    if translated is None:
        logger.warning("Перевод не получен: {!r}", text[:LOG_TEXT_LIMIT])
        if cooldown:
            start_cooldown()
        return None
    return translated


async def _translate_word(
    session: AsyncSession,
    word: Word,
    field: str = TRANSLATION_BOTH,
    *,
    timeout: float | None = None,
    cooldown: bool = True,
) -> dict[str, str]:
    """Переводит незаполненные русские поля слова и сохраняет их в БД.

    :param field: ``both`` — определение и пример (так карточка показывает оба),
        иначе одно поле: ``definition`` или ``example``.
    :param timeout: сколько секунд ждать ответ сервиса (см. :func:`translate_text`).
    :param cooldown: ставить ли общую паузу после неудачи (см. :func:`translate_text`).
    :return: имена записанных полей (``{"russian_definition": "..."}``); пустой
        словарь — если переводить нечего (поля пустые или переводы уже есть) или
        сервис не ответил.
    """
    targets: list[tuple[str, str]] = []
    for source, target in translation_field_pairs(field):
        source_text = getattr(word, source)
        if source_text and not getattr(word, target):
            targets.append((target, source_text))
    if not targets:
        return {}

    fields: dict[str, str] = {}
    for name, source in targets:
        translated = await translate_text(source, timeout=timeout, cooldown=cooldown)
        if translated:
            fields[name] = translated
        if in_cooldown():
            # Сервис не ответил — вторую строку уже не спрашиваем.
            break
    if not fields:
        return {}

    await repository.save_word_translations(session, word, **fields)
    logger.info(
        "Слово {!r}: добавлены русские переводы ({})",
        word.word,
        ", ".join(sorted(fields)),
    )
    return fields


async def ensure_word_translations(session: AsyncSession, word: Word) -> bool:
    """Дозаполняет русские переводы определения и примера и сохраняет их в БД.

    Проверка «уже переведено» идёт по самому слову, поэтому вызывать функцию
    можно перед каждой сборкой карточки: лишних запросов к MyMemory не будет.

    :return: ``True``, если в базу записан хотя бы один перевод. ``False`` —
        если переводить нечего (поля пустые или переводы уже есть), перевод
        выключен в ``.env`` или идёт пауза после неудачного запроса.
    """
    if not is_enabled() or in_cooldown():
        return False
    return bool(await _translate_word(session, word))


@dataclass(slots=True)
class TranslationBatchResult:
    """Итог одной порции пакетного перевода (:func:`translate_pending_words`)."""

    #: Сколько слов получили хотя бы один перевод.
    words: int = 0
    #: Сколько записано русских определений.
    definitions: int = 0
    #: Сколько записано русских примеров.
    examples: int = 0
    #: Сколько слов не поддалось (сервис не ответил).
    failed: int = 0
    #: Сколько слов словаря ещё ждут перевода после этой порции.
    left: int = 0
    #: ``True``, если прогон остановлен: сервис не отвечает — обычно лимит на сутки.
    quota_stopped: bool = False


async def translate_pending_words(
    session: AsyncSession,
    *,
    limit: int | None = None,
    field: str = TRANSLATION_BOTH,
    sleep_seconds: float = config.TRANSLATION_BATCH_SLEEP_SECONDS,
    max_failures: int = config.TRANSLATION_BATCH_MAX_FAILURES,
    learning_first: bool = False,
) -> TranslationBatchResult:
    """Переводит очередную порцию слов, у которых русского текста ещё нет.

    Пакетный режим нужен из-за суточного лимита MyMemory: он измеряется в
    символах, а словарь — в сотнях тысяч символов, поэтому за один запуск всё не
    перевести. Функция трогает только незаполненные поля и коммитит каждое слово
    отдельно, так что прерывание прогона (в том числе на середине слова)
    ничего не теряет: следующий запуск продолжит с того же места.

    Остановка: сервис подряд не ответил ``max_failures`` раз — обычно это значит
    «на сегодня лимит исчерпан» (MyMemory отвечает предупреждением вместо
    перевода). Одиночный сбой сети порцию не убивает: слово просто останется на
    следующий запуск. Запросы идут с длинным таймаутом
    (:data:`config.TRANSLATION_BATCH_TIMEOUT_SECONDS`) и без общей паузы после
    неудачи — паузой пользуются карточки (там важно не тормозить отправку), а
    фоновому скрипту важен результат.

    :param limit: сколько слов взять за прогон (``None`` — все).
    :param field: какие поля переводить (``both``, ``definition``, ``example``):
        лимит в символах можно растянуть, переводя примеры отдельным прогоном.
    :param sleep_seconds: пауза между запросами к сервису.
    :param max_failures: сколько неудач подряд считать исчерпанным лимитом.
    :param learning_first: сначала слова, которые уже есть в планах изучения.
    """
    result = TranslationBatchResult()
    if not is_enabled():
        return result

    pending = await repository.get_words_without_translations(
        session, limit=limit, field=field, learning_first=learning_first
    )
    if not pending:
        logger.info(texts.TRANSLATE_BATCH_EMPTY.format(field=field))
        return result
    logger.info(texts.TRANSLATE_BATCH_STARTED.format(count=len(pending), field=field))

    consecutive_failures = 0
    for word in pending:
        if in_cooldown():
            # Пауза осталась от перевода в карточке: сервис только что не ответил,
            # и порцию разумнее отложить, чем ждать таймаута на каждом слове.
            result.quota_stopped = True
            break
        fields = await _translate_word(
            session,
            word,
            field,
            timeout=config.TRANSLATION_BATCH_TIMEOUT_SECONDS,
            cooldown=False,
        )
        if fields:
            result.words += 1
            if "russian_definition" in fields:
                result.definitions += 1
            if "russian_example" in fields:
                result.examples += 1
            consecutive_failures = 0
        else:
            result.failed += 1
            consecutive_failures += 1
            if consecutive_failures >= max_failures:
                result.quota_stopped = True
                break
        # Порция может оборваться в любой момент — сохраняем сразу по слову.
        await session.commit()
        await asyncio.sleep(sleep_seconds)

    result.left = (await repository.pending_translations(session, field=field)).words
    return result

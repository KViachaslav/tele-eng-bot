"""Тесты хендлеров: фильтры инлайн-кнопок и сценарии отправки/ответа на слово.

``Bot`` заменяется заглушкой :class:`FakeBot`: она пишет вызовы Telegram-API в
списки, поэтому тесты не ходят в сеть и не требуют токена.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from aiogram import Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendAudio,
    SendMessage,
    SendVoice,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from loguru import logger

import config
import locales.ru as texts
from db import repository
from handlers import settings as settings_handlers
from handlers import start as start_handlers
from handlers import word_actions
from keyboards import inline as keyboards
from keyboards.callbacks import AnswerCallback, AudioCallback, SettingsCallback, ShowCallback
from services import audio, delivery
from services.message_builder import escape_text


class FakeBot:
    """Заглушка ``Bot``: вместо HTTP-запросов собирает аргументы вызовов.

    Отправки озвучки попадают в :attr:`voices`: бот шлёт голосовые сообщения
    (``SendVoice``), а не файлы mp3 (``SendAudio``) — тесты это различают.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.voices: list[dict[str, Any]] = []
        self.methods: list[Any] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: Any) -> bool:
        """Аналог ``Bot.send_message`` (может «упасть», если бот заблокирован)."""
        if self.error is not None:
            raise self.error
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        return True

    async def edit_message_text(self, **kwargs: Any) -> bool:
        """Аналог ``Bot.edit_message_text`` (прямой вызов метода API)."""
        self.edits.append(kwargs)
        return True

    async def __call__(self, method: Any, *args: Any, **kwargs: Any) -> bool:
        """Сюда приходят ``await callback.answer()`` и ``message.edit_text(...)``.

        ``Message.edit_reply_markup`` и ``Message.edit_text`` из aiogram не
        вызывают методы бота напрямую, а ждут, пока бот выполнит объект метода
        (``EditMessageText`` и т.п.), поэтому правки сообщения собираются здесь.
        """
        self.methods.append(method)
        if isinstance(method, EditMessageText):
            self.edits.append(
                {
                    "chat_id": method.chat_id,
                    "message_id": method.message_id,
                    "text": method.text,
                    "parse_mode": method.parse_mode,
                    "reply_markup": method.reply_markup,
                }
            )
        if isinstance(method, SendVoice):
            self.voices.append(
                {
                    "chat_id": method.chat_id,
                    "voice": method.voice,
                    "caption": method.caption,
                }
            )
        return True


def make_callback(data: str, bot: FakeBot, *, user_id: int = 1001) -> CallbackQuery:
    """Собирает callback-query так, как его видит хендлер (с привязанным ботом)."""
    message = Message(
        message_id=1,
        date=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
        chat=Chat(id=user_id, type="private"),
        text="word",
    ).as_(bot)
    callback = CallbackQuery(
        id="callback-1",
        from_user=TgUser(id=user_id, is_bot=False, first_name="Tester"),
        chat_instance="chat-instance",
        message=message,
        data=data,
    )
    return callback.as_(bot)


async def matched_handlers(router: Router, callback: CallbackQuery) -> set[str]:
    """Имена callback-хендлеров роутера, чьи фильтры совпали с нажатием кнопки."""
    names: set[str] = set()
    for handler in router.callback_query.handlers:
        is_match, _ = await handler.check(callback)
        if is_match:
            names.add(handler.callback.__name__)
    return names


@pytest.mark.parametrize(
    ("action", "value", "expected"),
    [
        (config.ACTION_TOGGLE, config.FIELD_DEFINITION, {"on_field_toggle"}),
        (config.ACTION_TOGGLE, config.FIELD_COLLOCATIONS, {"on_field_toggle"}),
        (config.ACTION_TOGGLE, config.POS_NOUN, {"on_pos_toggle"}),
        (config.ACTION_TOGGLE, config.POS_FILTER_ALL, {"on_pos_toggle"}),
        (config.ACTION_TOGGLE, "nonsense", set()),
        (config.ACTION_LEARNING_LIMIT, "", {"on_learning_limit_selected"}),
        (config.ACTION_LEARNING_LIMIT, config.ACTION_MANUAL, {"on_learning_limit_selected"}),
        (config.ACTION_LEARNING_LIMIT, "100", {"on_learning_limit_selected"}),
        (config.ACTION_FIELDS, "", {"on_fields"}),
        (config.ACTION_POS, "", {"on_pos"}),
        (config.ACTION_CLOSE, "", {"on_close"}),
        (config.ACTION_BACK, "", {"on_back"}),
    ],
)
async def test_settings_filters_hit_exact_handler(
    action: str, value: str, expected: set[str]
) -> None:
    """Каждое действие настроек обрабатывает ровно один хендлер.

    Регрессия: при составном фильтре (``filter(a, b)`` вместо ``a & b``) кнопки
    меню настроек попадали сразу в оба «переключающих» хендлера.
    """
    callback = make_callback(SettingsCallback(action=action, value=value).pack(), FakeBot())

    assert await matched_handlers(settings_handlers.router, callback) == expected


async def test_word_actions_filters_are_disjoint() -> None:
    """«Показать», озвучка и ответы ведут в разные хендлеры."""
    bot = FakeBot()
    show = make_callback(ShowCallback(word_id=1, delivery_id=2).pack(), bot)
    audio_click = make_callback(
        AudioCallback(word_id=1, delivery_id=2, accent=config.AUDIO_ACCENT_UK).pack(), bot
    )
    answer = make_callback(
        AnswerCallback(answer=config.ANSWER_KNOW, word_id=1, delivery_id=2).pack(), bot
    )

    assert await matched_handlers(word_actions.router, show) == {"on_show"}
    assert await matched_handlers(word_actions.router, audio_click) == {"on_audio"}
    assert await matched_handlers(word_actions.router, answer) == {"on_answer"}


def test_answer_keyboard_has_audio_buttons() -> None:
    """Во второй строке клавиатуры — по кнопке озвучки на каждый акцент."""
    markup = keyboards.answer_keyboard(word_id=7, delivery_id=9)
    row = markup.inline_keyboard[1]

    assert [button.text for button in markup.inline_keyboard[0]] == [
        texts.BTN_KNOW,
        texts.BTN_DONT_KNOW,
    ]
    assert [button.text for button in row] == [
        texts.render_audio_button(config.AUDIO_ACCENT_UK),
        texts.render_audio_button(config.AUDIO_ACCENT_US),
    ]
    assert AudioCallback.unpack(row[0].callback_data) == AudioCallback(
        word_id=7, delivery_id=9, accent=config.AUDIO_ACCENT_UK
    )
    assert AudioCallback.unpack(row[1].callback_data) == AudioCallback(
        word_id=7, delivery_id=9, accent=config.AUDIO_ACCENT_US
    )


def test_audio_keyboard_keeps_only_audio_row() -> None:
    """Клавиатура после ответа: только озвучка, без кнопок «Знаю» / «Не знаю»."""
    markup = keyboards.audio_keyboard(word_id=7, delivery_id=9)

    assert len(markup.inline_keyboard) == 1
    row = markup.inline_keyboard[0]
    assert [button.text for button in row] == [
        texts.render_audio_button(config.AUDIO_ACCENT_UK),
        texts.render_audio_button(config.AUDIO_ACCENT_US),
    ]
    assert [AudioCallback.unpack(button.callback_data).accent for button in row] == list(
        config.AUDIO_ACCENTS
    )


def show_data_from(bot: FakeBot) -> ShowCallback:
    """Достаёт данные кнопки «Показать» из первого отправленного сообщения."""
    markup = bot.sent[0]["reply_markup"]
    return ShowCallback.unpack(markup.inline_keyboard[0][0].callback_data)


@pytest.fixture
def audio_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Каталог озвучки вместо ``data``: файлы создаёт сам тест.

    Реальные ``data/uk`` и ``data/us`` — 192 МБ mp3, поэтому хендлер проверяется
    на пустом каталоге: имя файла для слова тест пишет сам.
    """
    directory = tmp_path / "audio"
    monkeypatch.setattr(config, "AUDIO_DIR", directory)
    audio.clear_cache()
    yield directory
    audio.clear_cache()


def make_mp3(audio_dir: Path, accent: str, word: str) -> Path:
    """Создаёт пустой файл озвучки ``<слово>_<акцент>.mp3``."""
    directory = audio_dir / accent
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{word}_{accent}.mp3"
    path.write_bytes(b"ID3\x03")
    return path


def alerts_from(bot: FakeBot) -> list[AnswerCallbackQuery]:
    """Ответы на нажатия (``callback.answer``) в порядке вызова."""
    return [method for method in bot.methods if isinstance(method, AnswerCallbackQuery)]


async def test_deliver_main_word_creates_record_and_show_button(session, user, words) -> None:
    """Первый показ слова: карточка, кнопка «Показать» и запись в журнале."""
    bot = FakeBot()

    assert await delivery.deliver_main_word(bot, session, user) is True

    assert len(bot.sent) == 1
    message = bot.sent[0]
    assert message["chat_id"] == user.telegram_id
    assert message["parse_mode"] == config.PARSE_MODE
    # Слово выбирается случайно, поэтому проверяем любую карточку из словаря.
    shown = [word.word for word in words if escape_text(word.word) in message["text"]]
    assert len(shown) == 1

    button = message["reply_markup"].inline_keyboard[0][0]
    assert button.text == texts.BTN_SHOW

    show_data = ShowCallback.unpack(button.callback_data)
    delivery_log = await repository.get_delivery(session, show_data.delivery_id)
    assert delivery_log is not None
    assert delivery_log.word_id == show_data.word_id
    assert delivery_log.answer is None
    assert delivery_log.answered_at is None


async def test_deliver_main_word_without_words(session, user) -> None:
    """Пустая таблица слов — отправлять нечего."""
    bot = FakeBot()

    assert await delivery.deliver_main_word(bot, session, user) is False
    assert bot.sent == []


async def test_spoiler_mode_sends_answer_buttons(session, user, words) -> None:
    """В режиме «всё сразу» перевод под спойлером, кнопки «Знаю»/«Не знаю»."""
    await repository.update_user(session, user, reveal_mode=config.REVEAL_MODE_SPOILER)
    bot = FakeBot()

    assert await delivery.deliver_main_word(bot, session, user) is True

    assert config.SPOILER_DELIMITER in bot.sent[0]["text"]
    buttons = bot.sent[0]["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == [texts.BTN_KNOW, texts.BTN_DONT_KNOW]
    answers = {AnswerCallback.unpack(button.callback_data).answer for button in buttons}
    assert answers == {config.ANSWER_KNOW, config.ANSWER_DONT_KNOW}


async def test_deliver_refresh_word_marks_and_hints(session, user, words) -> None:
    """Слово на освежение помечается ``is_refresh`` и приходит с подсказкой."""
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=datetime(2020, 1, 1, 0, 0, 0),
        last_reviewed_at=datetime(2019, 10, 1, 0, 0, 0),
        times_correct=6,
        times_wrong=0,
        is_refresh=False,
    )
    bot = FakeBot()

    assert await delivery.deliver_refresh_word(bot, session, user) is True

    assert progress.is_refresh is True
    assert escape_text(texts.REFRESH_HINT) in bot.sent[0]["text"]
    assert bot.sent[0]["text"].startswith(escape_text(words[0].word))


async def test_deliver_slot_sends_main_and_refresh(session, user, words) -> None:
    """Слот отправляет основное слово и, если срок подошёл, слово на освежение."""
    progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        progress,
        stage=config.SRS_MAX_STAGE,
        status=config.STATUS_LEARNED,
        next_review_at=datetime(2020, 1, 1, 0, 0, 0),
        last_reviewed_at=datetime(2019, 10, 1, 0, 0, 0),
        times_correct=6,
        times_wrong=0,
        is_refresh=False,
    )
    bot = FakeBot()

    assert await delivery.deliver_slot(bot, session, user) == 2
    assert len(bot.sent) == 2


async def test_unreachable_user_is_paused(session, user, words) -> None:
    """Если бот заблокирован, рассылка ставится на паузу."""
    error = TelegramForbiddenError(method=None, message="bot was blocked by the user")
    bot = FakeBot(error=error)

    assert await delivery.deliver_main_word(bot, session, user) is False
    assert user.paused is True
async def test_on_answer_updates_srs_and_message(session, user, words) -> None:
    """Ответ «Знаю» двигает этап SRS, пишет журнал и оставляет только озвучку."""
    bot = FakeBot()
    assert await delivery.deliver_main_word(bot, session, user) is True
    show_data = show_data_from(bot)
    answer_data = AnswerCallback(
        answer=config.ANSWER_KNOW, word_id=show_data.word_id, delivery_id=show_data.delivery_id
    )

    await word_actions.on_answer(
        callback=make_callback(answer_data.pack(), bot), callback_data=answer_data, session=session
    )

    progress = await repository.get_user_word(session, user.id, show_data.word_id)
    assert progress is not None
    assert progress.stage == 1
    assert progress.status == config.STATUS_LEARNING
    assert (progress.times_correct, progress.times_wrong) == (1, 0)
    assert progress.next_review_at is not None
    assert progress.last_reviewed_at is not None

    delivery_log = await repository.get_delivery(session, show_data.delivery_id)
    assert delivery_log is not None
    assert delivery_log.answer == config.ANSWER_KNOW
    assert delivery_log.answered_at is not None

    edited = bot.edits[-1]
    rows = edited["reply_markup"].inline_keyboard
    # Строка ответа убрана: повторный клик изменил бы статистику. Озвучка осталась.
    assert len(rows) == 1
    assert [button.text for button in rows[0]] == [
        texts.render_audio_button(config.AUDIO_ACCENT_UK),
        texts.render_audio_button(config.AUDIO_ACCENT_US),
    ]
    assert AudioCallback.unpack(rows[0][0].callback_data) == AudioCallback(
        word_id=show_data.word_id,
        delivery_id=show_data.delivery_id,
        accent=config.AUDIO_ACCENT_UK,
    )
    assert escape_text(delivery_log.word.word) in edited["text"]
    assert bot.methods


async def test_on_answer_rejects_second_click(session, user, words) -> None:
    """Повторный клик по той же кнопке не меняет статистику."""
    bot = FakeBot()
    assert await delivery.deliver_main_word(bot, session, user) is True
    show_data = show_data_from(bot)
    answer_data = AnswerCallback(
        answer=config.ANSWER_KNOW, word_id=show_data.word_id, delivery_id=show_data.delivery_id
    )

    await word_actions.on_answer(
        callback=make_callback(answer_data.pack(), bot), callback_data=answer_data, session=session
    )
    await word_actions.on_answer(
        callback=make_callback(answer_data.pack(), bot), callback_data=answer_data, session=session
    )

    progress = await repository.get_user_word(session, user.id, show_data.word_id)
    assert progress is not None
    assert progress.times_correct == 1
    assert len(bot.edits) == 1

async def test_on_answer_ignores_unknown_answer(session, user, words) -> None:
    """Значение ответа вне ``config.ANSWERS`` игнорируется."""
    bot = FakeBot()
    assert await delivery.deliver_main_word(bot, session, user) is True
    show_data = show_data_from(bot)
    answer_data = AnswerCallback(
        answer="maybe", word_id=show_data.word_id, delivery_id=show_data.delivery_id
    )

    await word_actions.on_answer(
        callback=make_callback(answer_data.pack(), bot), callback_data=answer_data, session=session
    )

    assert await repository.get_user_word(session, user.id, show_data.word_id) is None
    assert bot.edits == []


async def test_on_answer_reports_unknown_delivery(session, user, words) -> None:
    """Кнопка от неизвестной отправки не ломает хендлер."""
    bot = FakeBot()
    answer_data = AnswerCallback(answer=config.ANSWER_KNOW, word_id=1, delivery_id=999)

    await word_actions.on_answer(
        callback=make_callback(answer_data.pack(), bot), callback_data=answer_data, session=session
    )

    assert bot.edits == []
    assert bot.methods


async def test_on_show_reveals_card_with_answer_buttons(session, user, words) -> None:
    """Кнопка «Показать» правит сообщение: перевод и кнопки «Знаю»/«Не знаю»."""
    bot = FakeBot()
    assert await delivery.deliver_main_word(bot, session, user) is True
    show_data = show_data_from(bot)

    await word_actions.on_show(
        callback=make_callback(show_data.pack(), bot), callback_data=show_data, session=session
    )

    delivery_log = await repository.get_delivery(session, show_data.delivery_id)
    assert delivery_log is not None
    edited = bot.edits[-1]
    assert texts.LABEL_DEFINITION in edited["text"]
    assert escape_text(delivery_log.word.russian_translation or "") in edited["text"]
    buttons = edited["reply_markup"].inline_keyboard[0]
    assert [button.text for button in buttons] == [texts.BTN_KNOW, texts.BTN_DONT_KNOW]


async def test_on_show_reports_unknown_delivery(session, user, words) -> None:
    """«Показать» для неизвестной отправки не правит сообщение."""
    bot = FakeBot()
    show_data = ShowCallback(word_id=1, delivery_id=999)

    await word_actions.on_show(
        callback=make_callback(show_data.pack(), bot), callback_data=show_data, session=session
    )

    assert bot.edits == []
    assert bot.methods


@contextmanager
def captured_warnings() -> Iterator[list[str]]:
    """Собирает предупреждения loguru — так проверяются строки про кнопки."""
    messages: list[str] = []
    sink_id = logger.add(messages.append, level="WARNING", format="{message}")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


async def test_unknown_delivery_writes_reason_to_log(session, user, words) -> None:
    """Причина «отправки нет» попадает в лог, а не только в алерт пользователю.

    Без этой строки такой случай не отличить от опечатки в кнопке: пользователю
    показывается общий алерт «Не нашёл это слово в истории отправок».
    """
    bot = FakeBot()
    answer_data = AnswerCallback(
        answer=config.ANSWER_KNOW, word_id=words[0].id, delivery_id=999
    )

    with captured_warnings() as messages:
        await word_actions.on_answer(
            callback=make_callback(answer_data.pack(), bot),
            callback_data=answer_data,
            session=session,
        )

    log_text = "".join(messages)
    assert "Кнопка без отправки" in log_text
    assert "delivery_id=999" in log_text
    assert "пользователь 1001" in log_text
    assert "В журнале 0 записей, максимальный id=0" in log_text
    assert str(config.get_settings().database_path) in log_text


async def test_unknown_delivery_reports_journal_size(session, user, words) -> None:
    """В логе видно, что клик пришёл из другой базы: id больше максимального.

    Так выглядит работа двух экземпляров бота с разными базами: нажатие
    обрабатывает тот, у кого журнала отправок с этим ``delivery_id`` нет.
    """
    sent = await repository.create_delivery(session, user.id, words[0].id)
    bot = FakeBot()
    answer_data = AnswerCallback(
        answer=config.ANSWER_KNOW, word_id=words[0].id, delivery_id=999
    )

    with captured_warnings() as messages:
        await word_actions.on_answer(
            callback=make_callback(answer_data.pack(), bot),
            callback_data=answer_data,
            session=session,
        )

    log_text = "".join(messages)
    assert "delivery_id=999" in log_text
    assert f"В журнале 1 записей, максимальный id={sent.id}" in log_text


async def test_foreign_delivery_writes_reason_to_log(session, user, words) -> None:
    """Кнопка из чужого чата (пересланное сообщение): причина в логе.

    Инлайн-кнопки пересылаются вместе с сообщением, поэтому нажать карточку
    может аккаунт, которому отправка не принадлежит.
    """
    other = await repository.create_user(session, telegram_id=2002)
    foreign = await repository.create_delivery(session, other.id, words[0].id)
    bot = FakeBot()
    show_data = ShowCallback(word_id=words[0].id, delivery_id=foreign.id)

    with captured_warnings() as messages:
        await word_actions.on_show(
            callback=make_callback(show_data.pack(), bot),
            callback_data=show_data,
            session=session,
        )

    log_text = "".join(messages)
    assert "Кнопка от чужой отправки" in log_text
    assert f"delivery_id={foreign.id}" in log_text
    assert f"(user_id={other.id})" in log_text
    assert "нажал 1001" in log_text
    assert bot.edits == []


async def test_mismatched_word_writes_reason_to_log(session, user, words) -> None:
    """Слово в кнопке разошлось с журналом отправок: причина в логе."""
    own = await repository.create_delivery(session, user.id, words[0].id)
    bot = FakeBot()
    answer_data = AnswerCallback(
        answer=config.ANSWER_KNOW, word_id=words[1].id, delivery_id=own.id
    )

    with captured_warnings() as messages:
        await word_actions.on_answer(
            callback=make_callback(answer_data.pack(), bot),
            callback_data=answer_data,
            session=session,
        )

    log_text = "".join(messages)
    assert "Кнопка от другого слова" in log_text
    assert f"в отправке {own.id} слово {words[0].id}" in log_text
    assert await repository.get_user_word(session, user.id, words[1].id) is None


# ---------------------------------------------------------------------------
# Озвучка: кнопки «🔊 🇬🇧 UK» и «🔊 🇺🇸 US»
# ---------------------------------------------------------------------------
async def test_on_audio_sends_pronunciation(session, user, words, audio_dir) -> None:
    """Кнопка озвучки присылает слово отдельным голосовым сообщением."""
    sent = await repository.create_delivery(session, user.id, words[0].id)
    data = AudioCallback(
        word_id=words[0].id, delivery_id=sent.id, accent=config.AUDIO_ACCENT_UK
    )
    file = make_mp3(audio_dir, config.AUDIO_ACCENT_UK, words[0].word)
    bot = FakeBot()

    await word_actions.on_audio(
        callback=make_callback(data.pack(), bot), callback_data=data, session=session
    )

    assert len(bot.voices) == 1
    message = bot.voices[0]
    assert message["chat_id"] == user.telegram_id
    assert Path(message["voice"].path) == file
    assert message["caption"] == texts.render_audio_caption(words[0].word, config.AUDIO_ACCENT_UK)
    # Озвучка — не ответ: карточка не правится, статистика слова не меняется.
    assert bot.edits == []
    assert await repository.get_user_word(session, user.id, words[0].id) is None
    assert not alerts_from(bot)[-1].show_alert


async def test_on_audio_sends_voice_not_file(session, user, words, audio_dir) -> None:
    """Озвучка уходит голосовым сообщением (``SendVoice``), а не аудиофайлом.

    Telegram сам превращает mp3 в формат голосовых (OGG/OPUS), поэтому в чате
    видно сообщение с волной: оно играет сразу, без открытия вложения и без
    пустых полей «исполнитель»/«название».
    """
    sent = await repository.create_delivery(session, user.id, words[0].id)
    data = AudioCallback(
        word_id=words[0].id, delivery_id=sent.id, accent=config.AUDIO_ACCENT_UK
    )
    make_mp3(audio_dir, config.AUDIO_ACCENT_UK, words[0].word)
    bot = FakeBot()

    await word_actions.on_audio(
        callback=make_callback(data.pack(), bot), callback_data=data, session=session
    )

    audio_methods = [
        method for method in bot.methods if isinstance(method, (SendAudio, SendVoice))
    ]
    assert [type(method).__name__ for method in audio_methods] == ["SendVoice"]


async def test_on_audio_works_after_answer(session, user, words, audio_dir) -> None:
    """Кнопка озвучки из карточки после ответа по-прежнему присылает голосовое.

    Кнопки акцентов остаются в сообщении после «Знаю» / «Не знаю»: правя карточку
    без ``reply_markup``, бот потерял бы всю клавиатуру — Telegram снимает её.
    """
    bot = FakeBot()
    assert await delivery.deliver_main_word(bot, session, user) is True
    show_data = show_data_from(bot)
    answer_data = AnswerCallback(
        answer=config.ANSWER_DONT_KNOW,
        word_id=show_data.word_id,
        delivery_id=show_data.delivery_id,
    )

    await word_actions.on_answer(
        callback=make_callback(answer_data.pack(), bot),
        callback_data=answer_data,
        session=session,
    )

    word = await repository.get_word_by_id(session, show_data.word_id)
    assert word is not None
    file = make_mp3(audio_dir, config.AUDIO_ACCENT_UK, word.word)
    button = bot.edits[-1]["reply_markup"].inline_keyboard[0][0]
    audio_data = AudioCallback.unpack(button.callback_data)

    await word_actions.on_audio(
        callback=make_callback(audio_data.pack(), bot), callback_data=audio_data, session=session
    )

    assert len(bot.voices) == 1
    assert Path(bot.voices[0]["voice"].path) == file
    assert bot.voices[0]["caption"] == texts.render_audio_caption(
        word.word, config.AUDIO_ACCENT_UK
    )


async def test_on_audio_sends_american_accent(session, user, words, audio_dir) -> None:
    """Кнопка «🇺🇸 US» присылает американскую озвучку, а не британскую."""
    sent = await repository.create_delivery(session, user.id, words[0].id)
    data = AudioCallback(
        word_id=words[0].id, delivery_id=sent.id, accent=config.AUDIO_ACCENT_US
    )
    make_mp3(audio_dir, config.AUDIO_ACCENT_UK, words[0].word)
    file = make_mp3(audio_dir, config.AUDIO_ACCENT_US, words[0].word)
    bot = FakeBot()

    await word_actions.on_audio(
        callback=make_callback(data.pack(), bot), callback_data=data, session=session
    )

    assert Path(bot.voices[0]["voice"].path) == file
    assert bot.voices[0]["caption"] == texts.render_audio_caption(
        words[0].word, config.AUDIO_ACCENT_US
    )


async def test_on_audio_does_not_fall_back_to_other_accent(
    session, user, words, audio_dir
) -> None:
    """Файла запрошенного акцента нет: алерт, а не озвучка другого акцента."""
    sent = await repository.create_delivery(session, user.id, words[0].id)
    data = AudioCallback(
        word_id=words[0].id, delivery_id=sent.id, accent=config.AUDIO_ACCENT_US
    )
    make_mp3(audio_dir, config.AUDIO_ACCENT_UK, words[0].word)
    bot = FakeBot()

    await word_actions.on_audio(
        callback=make_callback(data.pack(), bot), callback_data=data, session=session
    )

    assert bot.voices == []
    alert = alerts_from(bot)[-1]
    assert alert.text == texts.render_audio_not_found(config.AUDIO_ACCENT_US)
    assert alert.show_alert is True


async def test_on_audio_reports_missing_file(session, user, words, audio_dir) -> None:
    """Слова нет в архиве озвучки: алерт, сообщение и статистика не меняются."""
    sent = await repository.create_delivery(session, user.id, words[0].id)
    data = AudioCallback(
        word_id=words[0].id, delivery_id=sent.id, accent=config.AUDIO_ACCENT_UK
    )
    bot = FakeBot()

    await word_actions.on_audio(
        callback=make_callback(data.pack(), bot), callback_data=data, session=session
    )

    assert bot.voices == []
    alert = alerts_from(bot)[-1]
    assert alert.text == texts.render_audio_not_found(config.AUDIO_ACCENT_UK)
    assert alert.show_alert is True


async def test_on_audio_reports_unknown_accent(session, user, words, audio_dir) -> None:
    """Акцент в ``callback_data`` неизвестен: алерт, каталог озвучки не ищется."""
    sent = await repository.create_delivery(session, user.id, words[0].id)
    make_mp3(audio_dir, config.AUDIO_ACCENT_UK, words[0].word)
    data = AudioCallback(word_id=words[0].id, delivery_id=sent.id, accent="de")
    bot = FakeBot()

    with captured_warnings() as messages:
        await word_actions.on_audio(
            callback=make_callback(data.pack(), bot), callback_data=data, session=session
        )

    assert bot.voices == []
    alert = alerts_from(bot)[-1]
    assert alert.text == texts.render_audio_not_found("de")
    assert alert.show_alert is True
    assert "Каталог озвучки" in "".join(messages)


async def test_on_audio_reports_unknown_delivery(session, user, words, audio_dir) -> None:
    """Кнопка из карточки без записи в журнале: подсказка «отправь /word»."""
    make_mp3(audio_dir, config.AUDIO_ACCENT_UK, words[0].word)
    data = AudioCallback(word_id=words[0].id, delivery_id=999, accent=config.AUDIO_ACCENT_UK)
    bot = FakeBot()

    await word_actions.on_audio(
        callback=make_callback(data.pack(), bot), callback_data=data, session=session
    )

    assert bot.voices == []
    alert = alerts_from(bot)[-1]
    assert alert.text == texts.render_delivery_not_found()
    assert alert.show_alert is True


async def test_on_audio_ignores_foreign_delivery(session, user, words, audio_dir) -> None:
    """Чужая отправка (пересланная карточка): озвучка не отправляется."""
    other = await repository.create_user(session, telegram_id=2003)
    foreign = await repository.create_delivery(session, other.id, words[0].id)
    make_mp3(audio_dir, config.AUDIO_ACCENT_UK, words[0].word)
    data = AudioCallback(
        word_id=words[0].id, delivery_id=foreign.id, accent=config.AUDIO_ACCENT_UK
    )
    bot = FakeBot()

    with captured_warnings() as messages:
        await word_actions.on_audio(
            callback=make_callback(data.pack(), bot), callback_data=data, session=session
        )

    assert bot.voices == []
    assert "Кнопка от чужой отправки" in "".join(messages)


# ---------------------------------------------------------------------------
# Ручной ввод: кнопка «✍️ Ввести вручную» и шаги ввода значения
# ---------------------------------------------------------------------------
class StubScheduler:
    """Заглушка ``SchedulerService``: проверяются настройки, а не расписание."""

    def schedule_user(self, user: object) -> None:
        """Ничего не планирует."""
        return None

    def unschedule_user(self, user_id: int) -> None:
        """Ничего не снимает."""
        return None


#: Шаг → клавиатура выбора значения (у каждой есть кнопка ручного ввода).
STEP_KEYBOARDS = {
    config.ACTION_TIMEZONE: keyboards.timezone_keyboard,
    config.ACTION_WORDS_PER_DAY: keyboards.words_per_day_keyboard,
    config.ACTION_LEARNING_LIMIT: keyboards.learning_limit_keyboard,
    config.ACTION_WINDOW: keyboards.window_keyboard,
}


def make_state(chat_id: int = 1001) -> FSMContext:
    """Контекст FSM в памяти — так хендлер видит состояние шага."""
    storage = MemoryStorage()
    return FSMContext(
        storage=storage, key=StorageKey(bot_id=1, chat_id=chat_id, user_id=chat_id)
    )


def make_message(text: str, bot: FakeBot, *, user_id: int = 1001) -> Message:
    """Сообщение с текстом пользователя (ответ на вопрос бота)."""
    message = Message(
        message_id=2,
        date=datetime(2026, 1, 15, 9, 5, tzinfo=timezone.utc),
        chat=Chat(id=user_id, type="private"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Tester"),
        text=text,
    )
    return message.as_(bot)


def sent_texts(bot: FakeBot) -> list[str]:
    """Тексты, отправленные через ``message.answer(...)``."""
    return [method.text for method in bot.methods if isinstance(method, SendMessage)]


def manual_button_data(keyboard: InlineKeyboardMarkup) -> str:
    """``callback_data`` кнопки «✍️ Ввести вручную» из клавиатуры."""
    for row in keyboard.inline_keyboard:
        for button in row:
            if button.text == texts.BTN_MANUAL:
                return button.callback_data
    raise AssertionError("в клавиатуре нет кнопки ручного ввода")


@pytest.mark.parametrize(
    ("router", "step", "expected"),
    [
        (start_handlers.router, config.ACTION_TIMEZONE, "on_timezone_chosen"),
        (start_handlers.router, config.ACTION_WORDS_PER_DAY, "on_words_chosen"),
        (start_handlers.router, config.ACTION_WINDOW, "on_window_chosen"),
        (settings_handlers.router, config.ACTION_TIMEZONE, "on_timezone_selected"),
        (settings_handlers.router, config.ACTION_WORDS_PER_DAY, "on_words_selected"),
        (settings_handlers.router, config.ACTION_LEARNING_LIMIT, "on_learning_limit_selected"),
        (settings_handlers.router, config.ACTION_WINDOW, "on_window_selected"),
    ],
)
async def test_manual_button_reaches_exactly_one_handler(
    router: Router, step: str, expected: str
) -> None:
    """«✍️ Ввести вручную» доходит до хендлера своего шага.

    Регрессия: кнопка уходила с ``callback_data`` вида ``settings:manual:`` —
    ``manual`` стояло в *действии* вместо *значения*, поэтому её не совпадал ни
    один фильтр и ручной ввод времени/числа слов молча не работал.
    """
    pack = (
        keyboards.settings_callback
        if router is settings_handlers.router
        else keyboards.registration_callback
    )
    data = manual_button_data(STEP_KEYBOARDS[step](pack=pack, back=True))

    assert data != pack(config.ACTION_MANUAL)

    assert await matched_handlers(router, make_callback(data, FakeBot())) == {expected}


async def test_settings_manual_window_saves_window(session, user, words) -> None:
    """Ручной ввод окна: кнопка → «09:00» → «22:00» сохраняет окно в профиль."""
    bot = FakeBot()
    state = make_state(user.telegram_id)
    data = SettingsCallback(action=config.ACTION_WINDOW, value=config.ACTION_MANUAL)

    await settings_handlers.on_window_selected(
        callback=make_callback(data.pack(), bot),
        callback_data=data,
        state=state,
        session=session,
        scheduler=StubScheduler(),
    )

    assert await state.get_state() == settings_handlers.SettingsStates.window_start.state
    assert bot.edits[-1]["text"] == texts.ASK_WINDOW_START

    await settings_handlers.on_window_start_text(
        message=make_message("09:00", bot), state=state, session=session
    )

    assert await state.get_state() == settings_handlers.SettingsStates.window_end.state
    assert sent_texts(bot)[-1] == texts.ASK_WINDOW_END.format(start="09:00")

    await settings_handlers.on_window_end_text(
        message=make_message("22:00", bot),
        state=state,
        session=session,
        scheduler=StubScheduler(),
    )

    assert (user.window_start, user.window_end) == ("09:00", "22:00")
    assert await state.get_state() is None


async def test_settings_manual_window_rejects_invalid_time(session, user, words) -> None:
    """Неверный формат времени не меняет окно и оставляет шаг активным."""
    bot = FakeBot()
    state = make_state(user.telegram_id)
    await state.set_state(settings_handlers.SettingsStates.window_start)

    await settings_handlers.on_window_start_text(
        message=make_message("25:99", bot), state=state, session=session
    )

    assert sent_texts(bot) == [texts.WINDOW_INVALID]
    assert await state.get_state() == settings_handlers.SettingsStates.window_start.state
    assert (user.window_start, user.window_end) == (
        config.WINDOW_START_DEFAULT,
        config.WINDOW_END_DEFAULT,
    )


async def test_settings_manual_words_are_saved(session, user, words) -> None:
    """Ручной ввод количества слов: кнопка → «7» сохраняет лимит."""
    bot = FakeBot()
    state = make_state(user.telegram_id)
    data = SettingsCallback(action=config.ACTION_WORDS_PER_DAY, value=config.ACTION_MANUAL)

    await settings_handlers.on_words_selected(
        callback=make_callback(data.pack(), bot),
        callback_data=data,
        state=state,
        session=session,
        scheduler=StubScheduler(),
    )

    assert await state.get_state() == settings_handlers.SettingsStates.words_per_day.state
    assert bot.edits[-1]["text"] == texts.ASK_WORDS_PER_DAY_CHANGE.format(
        min_words=config.WORDS_PER_DAY_MIN, max_words=config.WORDS_PER_DAY_MAX
    )

    await settings_handlers.on_words_text(
        message=make_message("7", bot),
        state=state,
        session=session,
        scheduler=StubScheduler(),
    )

    assert user.words_per_day == 7
    assert await state.get_state() is None


# ---------------------------------------------------------------------------
# Запрос слова вне расписания: разные слова вместо одного и того же
# ---------------------------------------------------------------------------
def word_ids_from(bot: FakeBot) -> list[int]:
    """Идентификаторы слов из отправленных карточек (по кнопке «Показать»)."""
    return [
        ShowCallback.unpack(message["reply_markup"].inline_keyboard[0][0].callback_data).word_id
        for message in bot.sent
    ]


async def test_on_demand_delivers_different_words(session, user, words) -> None:
    """``/word`` подряд присылает разные слова, а не одно и то же.

    Регрессия: у слова с наступившим сроком повторения этот срок остаётся
    наступившим, поэтому каждое нажатие снова выбирало его же.
    """
    bot = FakeBot()

    for _ in range(len(words)):
        assert await delivery.deliver_on_demand(bot, session, user) is True

    assert len(set(word_ids_from(bot))) == len(words)


async def test_on_demand_skips_word_from_chat(session, user, words) -> None:
    """Слово из висящей в чате карточки ручной запрос не повторяет."""
    bot = FakeBot()
    assert await delivery.deliver_main_word(bot, session, user) is True
    waiting_id = word_ids_from(bot)[0]

    assert await delivery.deliver_on_demand(bot, session, user) is True

    assert word_ids_from(bot)[1] != waiting_id


async def test_on_demand_repeats_only_without_alternatives(session, user, words) -> None:
    """Повтор возможен, только когда других слов в очереди не осталось.

    Фильтр по части речи оставляет пользователю одно слово, поэтому после ответа
    «не знаю» (этап 0 — слово снова «на сегодня») выбор падает на него же.
    """
    await repository.set_pos_filter(session, user, {config.POS_NOUN})
    bot = FakeBot()
    assert await delivery.deliver_on_demand(bot, session, user) is True

    show_data = show_data_from(bot)
    answer_data = AnswerCallback(
        answer=config.ANSWER_DONT_KNOW,
        word_id=show_data.word_id,
        delivery_id=show_data.delivery_id,
    )
    await word_actions.on_answer(
        callback=make_callback(answer_data.pack(), bot), callback_data=answer_data, session=session
    )

    assert await delivery.deliver_on_demand(bot, session, user) is True

    assert word_ids_from(bot) == [show_data.word_id, show_data.word_id]


# ---------------------------------------------------------------------------
# Лимит слов в изучении: настройки и блокировка новых слов
# ---------------------------------------------------------------------------
async def test_settings_learning_limit_opens_submenu(session, user, words) -> None:
    """«📚 Лимит в изучении» открывает подэкран с пресетами и ручным вводом."""
    bot = FakeBot()
    data = SettingsCallback(action=config.ACTION_LEARNING_LIMIT, value="")

    await settings_handlers.on_learning_limit_selected(
        callback=make_callback(data.pack(), bot),
        callback_data=data,
        state=make_state(user.telegram_id),
        session=session,
    )

    keyboard = bot.edits[-1]["reply_markup"]
    values = {
        SettingsCallback.unpack(button.callback_data).value
        for row in keyboard.inline_keyboard
        for button in row
    }
    assert {str(value) for value in config.LEARNING_LIMIT_PRESETS} <= values
    assert str(config.LEARNING_LIMIT_UNLIMITED) in values
    # Кнопка ручного ввода несёт действие шага — иначе она молча не работает.
    assert config.ACTION_MANUAL in values


async def test_settings_learning_limit_preset_is_saved(session, user, words) -> None:
    """Нажатие пресета применяет лимит сразу и возвращает в меню настроек."""
    bot = FakeBot()
    data = SettingsCallback(action=config.ACTION_LEARNING_LIMIT, value="50")

    await settings_handlers.on_learning_limit_selected(
        callback=make_callback(data.pack(), bot),
        callback_data=data,
        state=make_state(user.telegram_id),
        session=session,
    )

    assert user.learning_limit == 50
    assert bot.edits[-1]["text"].startswith(texts.SETTING_SAVED.format(value="50"))
    assert texts.render_learning_limit(user.learning_limit) in bot.edits[-1]["text"]


async def test_settings_manual_learning_limit_is_saved(session, user, words) -> None:
    """Ручной ввод лимита: кнопка → вопрос → «0» отключает ограничение."""
    bot = FakeBot()
    state = make_state(user.telegram_id)
    data = SettingsCallback(action=config.ACTION_LEARNING_LIMIT, value=config.ACTION_MANUAL)

    await settings_handlers.on_learning_limit_selected(
        callback=make_callback(data.pack(), bot),
        callback_data=data,
        state=state,
        session=session,
    )

    assert await state.get_state() == settings_handlers.SettingsStates.learning_limit.state
    assert bot.edits[-1]["text"] == texts.ASK_LEARNING_LIMIT_CHANGE.format(
        min_words=config.LEARNING_LIMIT_MIN,
        max_words=config.LEARNING_LIMIT_MAX,
        unlimited=texts.LEARNING_LIMIT_UNLIMITED_TITLE,
    )

    await settings_handlers.on_learning_limit_text(
        message=make_message("0", bot), state=state, session=session
    )

    assert user.learning_limit == config.LEARNING_LIMIT_UNLIMITED
    assert user.learning_limit_enabled is False
    assert await state.get_state() is None


@pytest.mark.parametrize("raw", ["5", "5000", "abc", "", "10.5"])
async def test_settings_learning_limit_rejects_invalid(session, user, raw: str) -> None:
    """Недопустимый лимит не сохраняется, шаг ввода остаётся активным."""
    bot = FakeBot()
    state = make_state(user.telegram_id)
    await state.set_state(settings_handlers.SettingsStates.learning_limit)

    await settings_handlers.on_learning_limit_text(
        message=make_message(raw, bot), state=state, session=session
    )

    assert sent_texts(bot) == [
        texts.LEARNING_LIMIT_INVALID.format(
            min_words=config.LEARNING_LIMIT_MIN,
            max_words=config.LEARNING_LIMIT_MAX,
        )
    ]
    assert user.learning_limit == config.LEARNING_LIMIT_DEFAULT
    assert await state.get_state() == settings_handlers.SettingsStates.learning_limit.state


async def test_learning_limit_stops_new_words(session, user, words) -> None:
    """Лимит изучения закрывает новые слова; без лимита (0) они снова приходят."""
    await repository.update_user(session, user, learning_limit=1)
    in_progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        in_progress,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=datetime(2099, 1, 1, 0, 0, 0),
        last_reviewed_at=datetime(2026, 1, 1, 0, 0, 0),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )
    bot = FakeBot()

    assert await delivery.deliver_main_word(bot, session, user) is False
    assert bot.sent == []

    await repository.update_user(session, user, learning_limit=config.LEARNING_LIMIT_UNLIMITED)
    assert await delivery.deliver_main_word(bot, session, user) is True

    delivered = word_ids_from(bot)
    assert delivered and delivered[0] != words[0].id


async def test_learning_limit_keeps_revisions_coming(session, user, words) -> None:
    """Повторения уже начатых слов лимит изучения не останавливает."""
    await repository.update_user(session, user, learning_limit=1)
    revision = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        revision,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=datetime(2020, 1, 1, 0, 0, 0),
        last_reviewed_at=datetime(2019, 12, 31, 0, 0, 0),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )
    bot = FakeBot()

    assert await delivery.deliver_main_word(bot, session, user) is True
    assert word_ids_from(bot) == [words[0].id]


async def test_cmd_word_reports_learning_limit(session, user, words) -> None:
    """``/word`` при достигнутом лимите объясняет причину, а не «слово не найдено»."""
    await repository.update_user(session, user, learning_limit=1)
    in_progress = await repository.get_or_create_user_word(session, user.id, words[0].id)
    await repository.save_user_word(
        session,
        in_progress,
        stage=1,
        status=config.STATUS_LEARNING,
        next_review_at=datetime(2099, 1, 1, 0, 0, 0),
        last_reviewed_at=datetime(2026, 1, 1, 0, 0, 0),
        times_correct=1,
        times_wrong=0,
        is_refresh=False,
    )
    bot = FakeBot()

    await word_actions.cmd_word(message=make_message(texts.CMD_WORD, bot), session=session, bot=bot)

    assert sent_texts(bot) == [
        texts.NEW_WORDS_LIMIT_REACHED.format(learning=1, limit=1, stats=texts.CMD_STATS)
    ]


async def test_cmd_word_without_dictionary(session, user) -> None:
    """Пустой словарь: подсказка про импорт, а не «нет подходящего слова»."""
    bot = FakeBot()

    await word_actions.cmd_word(message=make_message(texts.CMD_WORD, bot), session=session, bot=bot)

    assert sent_texts(bot) == [texts.NO_WORDS_IN_DATABASE]





"""Слова: команда ``/word``, показ перевода, озвучка и ответы «Знаю» / «Не знаю».

Ответ пользователя меняет состояние слова в SRS (:mod:`services.srs`) и
фиксируется в ``delivery_log``; после ответа у сообщения убираются кнопки
«Знаю» / «Не знаю», чтобы повторный клик не изменил статистику. Строка озвучки
«🔊 🇬🇧 UK» и «🔊 🇺🇸 US» остаётся: Telegram снимает клавиатуру, если править
текст без ``reply_markup``, поэтому после ответа карточка получает
:func:`keyboards.inline.audio_keyboard`. Озвучка присылает слово из
``data/<акцент>`` голосовым сообщением (:mod:`services.audio`) — на прогресс она
не влияет.
"""
from __future__ import annotations

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
import locales.ru as texts
from db import repository
from db.models import DeliveryLog, User, Word, utcnow
from handlers import common
from keyboards.callbacks import AnswerCallback, AudioCallback, ShowCallback
from keyboards.inline import answer_keyboard, audio_keyboard
from services import audio, delivery, slots, srs, translator
from services.message_builder import build_refresh_card, build_word_card, with_answer_result
from services.scheduler import SchedulerService

router = Router(name="word_actions")


async def _card_text(
    session: AsyncSession,
    user: User,
    word: Word,
    *,
    revealed: bool = True,
) -> str:
    """Карточка слова с учётом полей пользователя и режима показа.

    Перед сборкой текста дозаполняются русские переводы определения и примера
    (:mod:`services.translator`) — в карточке они идут под английскими строками
    под спойлером. Если словарь уже переведён, лишних запросов не будет.
    """
    settings = await repository.get_or_create_user_settings(session, user)
    user_word = await repository.get_user_word(session, user.id, word.id)
    is_refresh = user_word is not None and bool(user_word.is_refresh)
    await translator.ensure_word_translations(session, word)
    builder = build_refresh_card if is_refresh else build_word_card
    return builder(word, settings, reveal_mode=user.reveal_mode, revealed=revealed)


async def _fetch_delivery(
    session: AsyncSession,
    user: User,
    word_id: int,
    delivery_id: int,
) -> DeliveryLog | None:
    """Отправка, если она существует и принадлежит этому пользователю.

    ``callback_data`` собирается из ``delivery_log`` в момент отправки карточки,
    поэтому запись обязана найтись: строки из этого журнала нигде не удаляются.
    Если её нет, причина почти всегда внешняя — базу пересоздали или перенесли
    (например, скопировали ``bot.db`` без журнала ``bot.db-wal``, где лежали
    свежие отправки) либо кнопку нажал чужой аккаунт (пересланное сообщение).
    Причина пишется в лог: пользователю достаётся только общий алерт, и без
    этой строки такой случай не отличить от опечатки в кнопке. В строке есть
    размер журнала и путь к базе: если ``delivery_id`` больше максимального
    id в журнале, работает не та база (свежая или без журнала WAL).

    :return: запись журнала отправок или ``None`` — тогда хендлер отвечает
        пользователю подсказкой «отправь /word».
    """
    delivery_log = await repository.get_delivery(session, delivery_id)
    if delivery_log is None:
        stats = await repository.get_delivery_log_stats(session)
        logger.warning(
            "Кнопка без отправки: delivery_id={} нет в delivery_log "
            "(пользователь {}, слово {}). В журнале {} записей, максимальный id={} "
            "(база {}). База пересоздана или перенесена без WAL?",
            delivery_id,
            user.telegram_id,
            word_id,
            stats.count,
            stats.max_id,
            config.get_settings().database_path,
        )
        return None
    if delivery_log.user_id != user.id:
        logger.warning(
            "Кнопка от чужой отправки: delivery_id={} принадлежит другому "
            "пользователю (user_id={}), нажал {} (слово {})",
            delivery_id,
            delivery_log.user_id,
            user.telegram_id,
            word_id,
        )
        return None
    if delivery_log.word_id != word_id:
        logger.warning(
            "Кнопка от другого слова: в отправке {} слово {}, в кнопке {}",
            delivery_id,
            delivery_log.word_id,
            word_id,
        )
        return None
    return delivery_log


def _next_step(state: srs.SrsState) -> str:
    """Что сообщить пользователю о следующем показе слова."""
    if state.status == config.STATUS_LEARNED:
        return texts.NEXT_REVIEW_LEARNED.format(days=srs.interval_days(state.stage))
    if state.stage == config.SRS_FIRST_STAGE:
        return texts.NEXT_REVIEW_TODAY
    return texts.NEXT_REVIEW_IN_DAYS.format(
        days=srs.interval_days(state.stage),
        stage=state.stage,
    )


@router.message(common.command_filter(texts.CMD_WORD))
@router.message(F.text == texts.BTN_MENU_WORD)
async def cmd_word(message: Message, session: AsyncSession, bot: Bot, scheduler: SchedulerService) -> None:
    """Присылает слово вне расписания — слова плана дня, затем повтор дня.

    План тот же, что и у расписания: повторения со сроком до конца местных суток
    пользователя и новые слова по общим правилам. Поэтому кнопкой «🎲 Слово» день
    можно пройти досрочно — пройденное расписание больше не пришлёт, — а после
    выполнения плана очередь отдаёт слова дня, которые уже приходили и получили
    ответ (см. :func:`services.delivery.deliver_on_demand`). Если показать нечего,
    причину подсказывает :func:`handlers.common.no_word_text`. Отданное слово
    расходует дневной план, поэтому после отправки расписание пересчитывается.
    """
    user = await common.load_user(session, message)
    if user is None:
        return

    if await delivery.deliver_on_demand(bot, session, user):
        await scheduler.schedule_user(session, user)
        return

    await message.answer(
        await common.no_word_text(session, user),
        reply_markup=common.menu_markup(user),
    )


@router.callback_query(ShowCallback.filter())
async def on_show(
    callback: CallbackQuery,
    callback_data: ShowCallback,
    session: AsyncSession,
) -> None:
    """Показывает перевод и остальные поля по кнопке «Показать»."""
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    delivery_log = await _fetch_delivery(
        session, user, callback_data.word_id, callback_data.delivery_id
    )
    if delivery_log is None:
        await callback.answer(texts.render_delivery_not_found(), show_alert=True)
        return

    card = await _card_text(session, user, delivery_log.word)
    await common.safe_edit_text(
        callback.message,
        card,
        answer_keyboard(delivery_log.word_id, delivery_log.id),
        parse_mode=config.PARSE_MODE,
    )
    await callback.answer()


@router.callback_query(AudioCallback.filter())
async def on_audio(
    callback: CallbackQuery,
    callback_data: AudioCallback,
    session: AsyncSession,
) -> None:
    """Отправляет озвучку слова по кнопкам «🔊 🇬🇧 UK» и «🔊 🇺🇸 US».

    Акцент приходит в ``callback_data`` кнопки, файл ищется в ``data/<акцент>``
    (см. :mod:`services.audio`): озвучен не весь словарь, поэтому для слова без
    файла приходит алерт, а сообщение с карточкой не меняется — ответ на слово
    по-прежнему можно дать. В подпись к голосовому сообщению добавляется русский
    перевод слова под спойлером — послушав слово, можно проверить, помнишь ли ты
    его смысл.
    """
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    delivery_log = await _fetch_delivery(
        session, user, callback_data.word_id, callback_data.delivery_id
    )
    if delivery_log is None:
        await callback.answer(texts.render_delivery_not_found(), show_alert=True)
        return

    word = delivery_log.word
    accent = callback_data.accent
    path = audio.find_audio(word.word, accent)
    if path is None:
        logger.info("Озвучки {!r} для слова {!r} нет в {}", accent, word.word, config.AUDIO_DIR)
        await callback.answer(texts.render_audio_not_found(accent), show_alert=True)
        return

    # В подписи русский перевод слова — тот же, что и в шапке карточки. Настройки
    # карточки его не касаются: поля «определение» можно скрыть, а перевод нет.
    if not await audio.send_word_audio(
        callback.message,
        word.word,
        path,
        accent,
        russian_translation=word.russian_translation,
    ):
        await callback.answer(texts.render_audio_not_found(accent), show_alert=True)
        return

    logger.info(
        "Пользователю {} отправлена озвучка слова {!r} ({})",
        user.telegram_id,
        word.word,
        accent,
    )
    await callback.answer()


@router.callback_query(AnswerCallback.filter())
async def on_answer(
    callback: CallbackQuery,
    callback_data: AnswerCallback,
    session: AsyncSession,
    scheduler: SchedulerService,
) -> None:
    """Обрабатывает ответ «Знаю» / «Не знаю»: SRS, журнал отправок, сообщение.

    Клавиатура правится на :func:`keyboards.inline.audio_keyboard`: строку
    «Знаю» / «Не знаю» убираем (ответ уже записан), а озвучку оставляем — слово
    можно дослушать после ответа. Без ``reply_markup`` Telegram снял бы всю
    клавиатуру вместе с кнопками акцентов.

    Ответ меняет дневной план («знаю» убирает слово из сегодняшних, «не знаю»
    возвращает его на этап 0), поэтому после ответа расписание пересчитывается.
    """
    user = await common.load_user_from_callback(session, callback)
    if user is None:
        return

    if callback_data.answer not in config.ANSWERS:
        logger.warning("Неизвестный ответ в callback: {!r}", callback_data.answer)
        await callback.answer()
        return

    delivery_log = await _fetch_delivery(
        session, user, callback_data.word_id, callback_data.delivery_id
    )
    if delivery_log is None:
        await callback.answer(texts.render_delivery_not_found(), show_alert=True)
        return

    if delivery_log.answer is not None:
        await callback.answer(texts.ANSWER_ALREADY_PROCESSED, show_alert=True)
        return

    word = delivery_log.word
    now = utcnow()
    user_word = await repository.get_or_create_user_word(session, user.id, word.id)
    current = srs.SrsState(
        stage=user_word.stage,
        status=user_word.status,
        next_review_at=user_word.next_review_at,
        times_correct=user_word.times_correct,
        times_wrong=user_word.times_wrong,
        is_refresh=user_word.is_refresh,
    )
    # Срок повторения сразу сдвигается внутрь окна рассылки: ответ приходит и после
    # закрытия окна, а слово со сроком «за окном» не может показать ни один слот —
    # оно висело в плане дня, но слова не приходили (см. services.slots.align_to_window).
    updated = srs.apply_answer(
        current,
        callback_data.answer,
        now,
        window=slots.WindowBounds.of(user),
    )

    await repository.save_user_word(
        session,
        user_word,
        stage=updated.stage,
        status=updated.status,
        next_review_at=updated.next_review_at,
        last_reviewed_at=now,
        times_correct=updated.times_correct,
        times_wrong=updated.times_wrong,
        is_refresh=updated.is_refresh,
    )
    await repository.mark_delivery_answered(
        session, delivery_log, callback_data.answer, answered_at=now
    )
    logger.info(
        "Пользователь {} ответил «{}» на слово {!r}: этап {}",
        user.telegram_id,
        callback_data.answer,
        word.word,
        updated.stage,
    )
    await scheduler.schedule_user(session, user)

    template = (
        texts.ANSWER_KNOW_RESULT
        if callback_data.answer == config.ANSWER_KNOW
        else texts.ANSWER_DONT_KNOW_RESULT
    )
    result_text = template.format(next_step=_next_step(updated))
    card = await _card_text(session, user, word)
    await common.safe_edit_text(
        callback.message,
        with_answer_result(card, result_text),
        audio_keyboard(delivery_log.word_id, delivery_log.id),
        parse_mode=config.PARSE_MODE,
    )
    await callback.answer()


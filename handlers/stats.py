"""Статистика пользователя: команда ``/stats`` и кнопка reply-меню.

Тексты живут в ``locales/ru.py``, данные собирает
:func:`db.repository.collect_user_stats`.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy.ext.asyncio import AsyncSession

import locales.ru as texts
from db import repository
from db.repository import UserStats
from handlers import common

router = Router(name="stats")


def problems_text(stats: UserStats) -> str:
    """Список самых проблемных слов (или пометка, что ошибок ещё нет)."""
    if not stats.problems:
        return texts.STATS_NO_PROBLEMS
    return "\n".join(
        texts.STATS_PROBLEM_LINE.format(
            index=index,
            word=user_word.word.word,
            wrong=user_word.times_wrong,
            correct=user_word.times_correct,
        )
        for index, user_word in enumerate(stats.problems, start=1)
    )


def render_stats(stats: UserStats, *, limit: int) -> str:
    """Собирает текст экрана статистики из данных репозитория.

    :param limit: лимит слов в изучении (``0`` — без ограничения).
    """
    return texts.STATS_TEXT.format(
        total_words=stats.total_words,
        learned=stats.learned,
        learning=stats.learning,
        new=stats.new,
        limit=texts.render_learning_limit(limit),
        accuracy=texts.STATS_ACCURACY_TEMPLATE.format(value=stats.accuracy),
        correct=stats.correct,
        wrong=stats.wrong,
        streak=stats.streak,
        problems=problems_text(stats),
    )


@router.message(common.command_filter(texts.CMD_STATS))
@router.message(F.text == texts.BTN_MENU_STATS)
async def cmd_stats(message: Message, session: AsyncSession) -> None:
    """Показывает статистику обучения."""
    user = await common.load_user(session, message)
    if user is None:
        return

    user_stats = await repository.collect_user_stats(session, user)
    await message.answer(
        render_stats(user_stats, limit=user.learning_limit),
        reply_markup=common.menu_markup(user),
    )

"""Планировщик рассылки: один ``AsyncIOScheduler`` на всё приложение.

Для каждого пользователя держится отдельная задача с датой следующего слова
(``DateTrigger``). После каждой отправки следующий слот рассчитывается заново,
поэтому смена окна, числа слов, часового пояса и пауза вступают в силу сразу.

Интервал между словами считается от текущего момента: оставшиеся минуты окна
делятся на оставшиеся слова дня — ``users.words_per_day`` плюс повторения, срок
которых наступает сегодня (см. :mod:`services.plan` и :func:`services.slots.next_slot`).
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

import config
from database import session_scope
from db import repository
from db.models import User, utcnow
from services import plan, slots
from services.delivery import deliver_slot


def job_id_for(user_id: int) -> str:
    """Идентификатор задачи планировщика для пользователя."""
    return config.SCHEDULER_JOB_ID_TEMPLATE.format(user_id=user_id)


class SchedulerService:
    """Планировщик отправки слов."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)
        self._logger = logger.bind(service="scheduler")

    @property
    def scheduler(self) -> AsyncIOScheduler:
        """Нижележащий APScheduler (например, для ``/stats`` и отладки)."""
        return self._scheduler

    def start(self) -> None:
        """Запускает планировщик."""
        if not self._scheduler.running:
            self._scheduler.start()
            self._logger.info("Планировщик запущен")

    async def shutdown(self) -> None:
        """Останавливает планировщик без ожидания выполнения задач."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            self._logger.info("Планировщик остановлен")

    async def restore_all(self) -> int:
        """Восстанавливает расписания всех активных пользователей (после рестарта)."""
        scheduled = 0
        async with session_scope() as session:
            for user in await repository.list_users(session, only_active=True):
                if await self.schedule_user(session, user) is not None:
                    scheduled += 1
        self._logger.info("Восстановлено расписаний: {}", scheduled)
        return scheduled

    async def schedule_user(
        self,
        session: AsyncSession,
        user: User,
        *,
        now: datetime | None = None,
    ) -> datetime | None:
        """(Пере)планирует следующее слово пользователя.

        Перед расчётом слота берётся дневной план пользователя
        (:func:`services.plan.words_left_today`): чем меньше слов осталось на
        сегодня, тем реже они приходят, поэтому после каждой отправки, ответа и
        ручного запроса «дай слово» расписание считается заново.

        :param now: момент расчёта (по умолчанию — текущее время); параметр нужен
            тестам, чтобы расписание не зависело от времени суток.
        :return: момент следующей отправки в UTC или ``None``, если пользователь
            на паузе либо слот рассчитать не удалось.
        """
        if user.paused:
            self.unschedule_user(user.id)
            return None

        moment_now = now or utcnow()
        words_left = await plan.words_left_today(session, user, moment_now)
        moment = slots.next_slot(user, moment_now, words_left)
        if moment is None:
            self._logger.warning("Не удалось рассчитать слот для пользователя {}", user.id)
            return None

        self._scheduler.add_job(
            self._deliver_due_word,
            trigger=DateTrigger(run_date=moment),
            args=[user.id],
            id=job_id_for(user.id),
            name=f"{config.SCHEDULER_JOB_PREFIX}:{user.id}",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            # если бот был выключен дольше допуска — слот не «догоняем» задним числом
            misfire_grace_time=config.WINDOW_TOLERANCE_SECONDS,
        )
        self._logger.debug(
            "Пользователь {}: слов на сегодня осталось {}, следующее слово в {} (местное {})",
            user.id,
            words_left,
            moment.isoformat(),
            slots.local_time_string(user, moment),
        )
        return moment

    def unschedule_user(self, user_id: int) -> None:
        """Убирает задачу пользователя (пауза или удаление)."""
        job = self._scheduler.get_job(job_id_for(user_id))
        if job is not None:
            job.remove()
            self._logger.debug("Задача пользователя {} снята", user_id)

    async def _deliver_due_word(self, user_id: int) -> None:
        """Тело задачи: отправить слово слота и запланировать следующий слот."""
        try:
            async with session_scope() as session:
                user = await repository.get_user_by_id(session, user_id)
                if user is None:
                    self.unschedule_user(user_id)
                    return
                if user.paused:
                    self._logger.debug("Пользователь {} на паузе — слот пропущен", user_id)
                    return

                now = utcnow()
                if not slots.is_within_window(user, now):
                    self._logger.warning(
                        "Слот пользователя {} ({}) вне окна рассылки — пропуск",
                        user_id,
                        slots.local_time_string(user, now),
                    )
                    return

                delivered = await deliver_slot(self._bot, session, user)
                if delivered == 0:
                    self._logger.debug(
                        "Пользователь {}: подходящих слов нет — ждём следующий слот", user_id
                    )
                next_moment = await self.schedule_user(session, user)

            if next_moment is not None:
                self._logger.info(
                    "Пользователь {}: слов отправлено {}, следующее — {} UTC",
                    user_id,
                    delivered,
                    next_moment.isoformat(),
                )
        except Exception:
            # одна плохая отправка не должна ломать планировщик
            self._logger.exception("Ошибка при отправке слова пользователю {}", user_id)

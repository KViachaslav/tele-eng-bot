"""Планировщик рассылки: один ``AsyncIOScheduler`` на всё приложение.

Для каждого пользователя держится отдельная задача с датой следующего слова
(``DateTrigger``). После каждой отправки следующий слот рассчитывается заново,
поэтому смена окна, числа слов, часового пояса и пауза вступают в силу сразу.

Интервал между словами считается от текущего момента: оставшиеся минуты окна
делятся на оставшиеся слова дня — ``users.words_per_day`` плюс повторения, срок
которых наступает сегодня (см. :mod:`services.plan` и :func:`services.slots.next_slot`).

Задача пользователя одна и живёт в памяти процесса, поэтому её мало поставить:
следующая ставится после каждой отправки, ответа, ручного запроса «дай слово» и на
любом выходе самой задачи (:meth:`SchedulerService._reschedule_after_slot`), а раз в
``config.SCHEDULER_WATCHDOG_SECONDS`` страж (:meth:`SchedulerService.watchdog`)
проверяет, что у каждого активного пользователя есть задача на будущее. Без стража
потерянная задача оставляла пользователя без слов вовсе: в базе 09.10.2026 «план дня
2», «слов отправлено 0» и ни одной задачи в планировщике.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
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
        """Запускает планировщик и ставит стража расписаний (:meth:`watchdog`)."""
        if not self._scheduler.running:
            self._scheduler.start()
            self._logger.info("Планировщик запущен")
        self._scheduler.add_job(
            self.watchdog,
            trigger=IntervalTrigger(seconds=config.SCHEDULER_WATCHDOG_SECONDS),
            id=config.SCHEDULER_WATCHDOG_JOB_ID,
            name=config.SCHEDULER_WATCHDOG_JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self._logger.info(
            "Страж расписаний поставлен: проверка каждые {} с",
            config.SCHEDULER_WATCHDOG_SECONDS,
        )

    async def shutdown(self) -> None:
        """Останавливает планировщик и снимает стража.

        Задачи пользователей убирает сам ``shutdown``; страж снимается отдельно,
        чтобы после остановки не осталось задачи, которая переставляет расписания
        (см. :meth:`watchdog`).
        """
        if self._scheduler.running:
            self._scheduler.remove_all_jobs()
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
            # Опоздавшая в пределах допуска задача всё-таки выполняется (один раз,
            # coalesce): раньше допуск равнялся ``WINDOW_TOLERANCE_SECONDS`` (90 с), и
            # короткий рестарт бота «стоил» слова — слот пропадал совсем.
            misfire_grace_time=config.SCHEDULER_MISFIRE_GRACE_SECONDS,
        )
        self._logger.info(
            "Пользователь {}: план дня {}, интервал {} мин, следующее слово в {} (местное {})",
            user.id,
            words_left,
            round((moment - slots.as_utc(moment_now)).total_seconds() / 60, 1),
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
        """Тело задачи: отправить слово слота и запланировать следующий слот.

        Следующий слот ставится на **любом** выходе, включая ошибку
        (:meth:`_reschedule_after_slot`): задача уже сработала, и без
        перепланирования расписание пользователя исчезло бы — в базе 09.10.2026 так и
        вышло («план дня 2», ни одной задачи в планировщике). Если перепланировать не
        удалось и это, расписание вернёт страж (:meth:`watchdog`).
        """
        delivered = 0
        try:
            async with session_scope() as session:
                user = await repository.get_user_by_id(session, user_id)
                if user is None:
                    self.unschedule_user(user_id)
                    return
                if user.paused:
                    self._logger.debug("Пользователь {} на паузе — слот пропущен", user_id)
                    self.unschedule_user(user_id)
                    return

                now = utcnow()
                if not slots.is_within_window(user, now):
                    self._logger.warning(
                        "Слот пользователя {} ({}) вне окна рассылки — слово перенесено "
                        "на следующий слот",
                        user_id,
                        slots.local_time_string(user, now),
                    )
                    return

                delivered = await deliver_slot(self._bot, session, user)
                if delivered == 0:
                    # Слот сработал и остался пустым: без этого отчёта в логе видно
                    # только «слов отправлено 0» (см. services.plan.describe_empty_queue).
                    self._logger.info(
                        "Пользователь {}: подходящих слов нет — {}",
                        user_id,
                        await plan.describe_empty_queue(session, user, now),
                    )
        except Exception:
            # одна плохая отправка не должна ломать планировщик
            self._logger.exception("Ошибка при отправке слова пользователю {}", user_id)
        finally:
            await self._reschedule_after_slot(user_id, delivered=delivered)

    async def _reschedule_after_slot(self, user_id: int, *, delivered: int) -> None:
        """Ставит следующий слот пользователя — на любом выходе из задачи.

        Берётся своя сессия: та, в которой шла отправка, уже закрыта. Ошибку расчёта
        слота здесь не пробрасываем — это уже не отправит слово, зато у пользователя
        останется шанс, что расписание вернёт страж (:meth:`watchdog`).
        """
        try:
            async with session_scope() as session:
                user = await repository.get_user_by_id(session, user_id)
                if user is None:
                    self.unschedule_user(user_id)
                    return
                moment = await self.schedule_user(session, user)
            if moment is not None:
                self._logger.info(
                    "Пользователь {}: слов отправлено {}, следующее — {} UTC",
                    user_id,
                    delivered,
                    moment.isoformat(),
                )
        except Exception:
            self._logger.exception(
                "Не удалось запланировать следующий слот пользователя {} — подхватит страж",
                user_id,
            )

    async def watchdog(self) -> None:
        """Страж: возвращает потерянные расписания и пишет «пульс».

        Задача пользователя живёт в памяти процесса, поэтому она теряется от ручного
        снятия, ошибки и перезапуска: в базе 09.10.2026 у пользователя было «план дня
        2» и ни одной задачи в планировщике — слова не приходили до вмешательства
        извне. Раз в ``config.SCHEDULER_WATCHDOG_SECONDS`` страж проходит по активным
        пользователям и ставит задачу заново, если её нет или её срок уже прошёл.

        Сам страж ставится с идентификатором
        :data:`config.SCHEDULER_WATCHDOG_JOB_ID` (:meth:`start`), а его «пульс» в логе
        показывает, что расписания под присмотром.
        """
        now = utcnow()
        restored = 0
        async with session_scope() as session:
            for user in await repository.list_users(session, only_active=True):
                if self._job_is_planned(user.id, now):
                    continue
                if await self.schedule_user(session, user, now=now) is not None:
                    restored += 1
                    self._logger.warning(
                        "Страж вернул расписание пользователю {}: задачи не было или её "
                        "срок уже прошёл",
                        user.id,
                    )
        job = self._scheduler.get_job(config.SCHEDULER_WATCHDOG_JOB_ID)
        self._logger.info(
            "Страж расписаний {}: задач {}, восстановлено {}, следующая проверка {}",
            config.SCHEDULER_WATCHDOG_JOB_ID,
            len(self._scheduler.get_jobs()),
            restored,
            job.next_run_time.isoformat()
            if job is not None and job.next_run_time is not None
            else "—",
        )

    def _job_is_planned(self, user_id: int, now: datetime) -> bool:
        """Есть ли у пользователя задача на будущее (иначе страж её переставит).

        У незапущенного планировщика (например, в тестах) ``next_run_time`` ещё не
        рассчитан — тогда задачу считаем потерянной и ставим заново:
        ``replace_existing`` делает это безопасным.
        """
        job = self._scheduler.get_job(job_id_for(user_id))
        next_run = getattr(job, "next_run_time", None)
        if next_run is None:
            return False
        return next_run > now.replace(tzinfo=timezone.utc)

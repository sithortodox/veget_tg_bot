"""Планировщик (APScheduler): таймауты заказов и предзаказов.

Отвечает только за расписание. Бизнес-действия по таймауту выполняют
сервисы заказов/предзаказов через `TimeoutRunner` — планировщик их не знает.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class SchedulerService:
    """Обёртка над APScheduler с одноразовыми jobs и их отменой."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone=timezone.utc)

    def start(self) -> None:
        """Запускает планировщик."""
        if not self._scheduler.running:
            self._scheduler.start()
            logger.info("Планировщик запущен")

    def shutdown(self) -> None:
        """Останавливает планировщик (при завершении бота)."""
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Планировщик остановлен")

    def schedule_once(
        self,
        job_id: str,
        delay_seconds: float,
        func: Callable[..., Awaitable[Any]],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Ставит одноразовую задачу через delay_seconds.

        Если job_id уже есть — заменяет (например, таймер перезапустили).
        """
        run_date = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        self._scheduler.add_job(
            func,
            trigger=DateTrigger(run_date=run_date),
            id=job_id,
            args=args,
            kwargs=kwargs,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=60,
        )

    def cancel(self, job_id: str) -> None:
        """Отменяет задачу, если она есть. Ошибки нет — job мог уже отработать."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001 — JobLookupError и прочее
            pass

    def cancel_prefix(self, prefix: str) -> None:
        """Отменяет все задачи с префиксом (например, 'order:12:')."""
        for job in self._scheduler.get_jobs():
            if job.id.startswith(prefix):
                try:
                    job.remove()
                except Exception:  # noqa: BLE001
                    pass


class TimeoutRunner:
    """Исполнитель таймаутов: открывает сессию БД и зовёт сервисы.

    Отделён от SchedulerService, чтобы планировщик не зависел от БД/сервисов.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory
        # Сервисы подставляются позже (из реестра), чтобы избежать циклов
        self.order_service: Any = None
        self.preorder_service: Any = None

    def bind(self, order_service: Any, preorder_service: Any) -> None:
        """Привязывает сервисы после создания всех объектов."""
        self.order_service = order_service
        self.preorder_service = preorder_service

    async def _run(self, method_name: str, *args: Any) -> None:
        """Универсальный запуск метода сервиса в новой сессии."""
        method = getattr(self.order_service, method_name, None)
        if method is None:
            method = getattr(self.preorder_service, method_name, None)
        if method is None:
            logger.error("Метод таймаута '%s' не найден", method_name)
            return
        try:
            async with self._session_factory() as session:
                await method(session, *args)
                await session.commit()
        except Exception:  # noqa: BLE001 — таймаут не должен ронять планировщик
            logger.exception("Ошибка выполнения таймаута %s", method_name)

    # --- Явные обёртки для job-функций (айди и аргументы типизированы) ---

    async def on_seller_confirm_timeout(self, order_id: int, shop_id: int) -> None:
        """Ответ магазина не пришёл за TIMEOUT_SELLER_CONFIRM — исключаем его."""
        await self._run("handle_seller_confirm_timeout", order_id, shop_id)

    async def on_client_approve_timeout(self, order_id: int) -> None:
        """Покупатель не утвердил предзаказ за TIMEOUT_CLIENT_APPROVE."""
        await self._run("handle_client_approve_timeout", order_id)

    async def on_assemble_timeout(self, order_id: int, sub_order_id: int) -> None:
        """Магазин не собрал за TIMEOUT_SELLER_ASSEMBLE — напоминаем/отменяем."""
        await self._run("handle_assemble_timeout", order_id, sub_order_id)

    async def on_pickup_timeout(self, order_id: int) -> None:
        """Покупатель не забрал/не оплатил за TIMEOUT_CLIENT_PICKUP — бан-логика."""
        await self._run("handle_pickup_timeout", order_id)

    async def on_courier_pickup_timeout(self, order_id: int) -> None:
        """Курьер не забрал за TIMEOUT_COURIER_PICKUP."""
        await self._run("handle_courier_pickup_timeout", order_id)

    async def on_courier_deliver_timeout(self, order_id: int) -> None:
        """Курьер не доставил за TIMEOUT_COURIER_DELIVER."""
        await self._run("handle_courier_deliver_timeout", order_id)

    # Предзаказные события (делегируются PreorderService)
    async def on_preorder_reminder(self, order_id: int) -> None:
        await self._run("handle_reminder", order_id)

    async def on_preorder_confirm_deadline(self, order_id: int) -> None:
        await self._run("handle_confirm_deadline", order_id)

    async def on_preorder_search_other(self, order_id: int) -> None:
        await self._run("handle_search_other", order_id)

    async def on_preorder_client_deadline(self, order_id: int) -> None:
        await self._run("handle_client_deadline", order_id)


def job_id_seller_confirm(order_id: int, shop_id: int) -> str:
    """Идентификатор job: таймер ответа продавца."""
    return f"order:{order_id}:seller_confirm:{shop_id}"


def job_id_client_approve(order_id: int) -> str:
    """Идентификатор job: таймер утверждения покупателем."""
    return f"order:{order_id}:client_approve"


def job_id_assemble(order_id: int, sub_order_id: int) -> str:
    """Идентификатор job: таймер сборки."""
    return f"order:{order_id}:assemble:{sub_order_id}"


def job_id_pickup(order_id: int) -> str:
    """Идентификатор job: таймер получения/оплаты."""
    return f"order:{order_id}:pickup"


def job_id_courier_pickup(order_id: int) -> str:
    """Идентификатор job: таймер забора курьером."""
    return f"order:{order_id}:courier_pickup"


def job_id_courier_deliver(order_id: int) -> str:
    """Идентификатор job: таймер доставки."""
    return f"order:{order_id}:courier_deliver"


def job_id_preorder(order_id: int, event: str) -> str:
    """Идентификатор job для событий предзаказа."""
    return f"order:{order_id}:preorder:{event}"

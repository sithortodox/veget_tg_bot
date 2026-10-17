"""Сервис предзаказов на дату (§7 ТЗ). Модель B:
магазин видит предзаказ сразу, но подтверждает за 12 ч до даты;
если не подтвердил за 2 ч — ищем другой; за 1 ч — отмена.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import (
    OrderStatus,
    OrderType,
    PreorderEvent,
    SubOrderStatus,
)
from app.db.models import Order, PreorderLog, SubOrder
from app.services.geo import GeoService
from app.services.notifier import Notifier
from app.services.scheduler import (
    SchedulerService,
    job_id_preorder,
)

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    """Текущее время UTC."""
    return datetime.now(timezone.utc)


class PreorderService:
    """Жизненный цикл предзаказа."""

    def __init__(
        self,
        settings: Settings,
        notifier: Notifier,
        geo: GeoService,
        scheduler: SchedulerService,
        order_service: object,
    ) -> None:
        self._settings = settings
        self._notifier = notifier
        self._geo = geo
        self._scheduler = scheduler
        self._orders = order_service  # OrderService (избегаем цикла при импорте)
        self._timeout_runner: object = None

    def set_timeout_runner(self, runner: object) -> None:
        """Привязывает исполнитель таймаутов."""
        self._timeout_runner = runner

    # ------------------------------------------------------------------
    # Планирование событий
    # ------------------------------------------------------------------

    async def schedule_preorder_jobs(self, session: AsyncSession, order: Order) -> None:
        """Ставит все задачи предзаказа относительно даты выполнения (§7.6)."""
        if not order.scheduled_at:
            return
        when = order.scheduled_at
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        now = utcnow()
        jobs: list[tuple[str, datetime]] = [
            (
                "reminder",
                when - timedelta(hours=self._settings.preorder_reminder_hours),
            ),
            (
                "confirm_deadline",
                when - timedelta(hours=self._settings.preorder_seller_confirm_hours),
            ),
            (
                "search_other",
                when - timedelta(hours=self._settings.preorder_search_other_hours),
            ),
            (
                "client_deadline",
                when - timedelta(hours=self._settings.preorder_client_confirm_hours),
            ),
        ]
        for event, run_at in jobs:
            delay = (run_at - now).total_seconds()
            if delay <= 0:
                # Дата уже близко — запускаем сразу
                delay = 1
            runner = self._timeout_runner
            method = {
                "reminder": runner.on_preorder_reminder,  # type: ignore[attr-defined]
                "confirm_deadline": runner.on_preorder_confirm_deadline,  # type: ignore[attr-defined]
                "search_other": runner.on_preorder_search_other,  # type: ignore[attr-defined]
                "client_deadline": runner.on_preorder_client_deadline,  # type: ignore[attr-defined]
            }[event]
            self._scheduler.schedule_once(
                job_id_preorder(order.id, event), delay, method, order.id
            )

    def cancel_preorder_jobs(self, order_id: int) -> None:
        """Отменяет все задачи предзаказа."""
        for event in ("reminder", "confirm_deadline", "search_other", "client_deadline"):
            self._scheduler.cancel(job_id_preorder(order_id, event))

    async def _log_event(
        self,
        session: AsyncSession,
        order_id: int,
        event: PreorderEvent,
        shop_id: Optional[int] = None,
    ) -> None:
        """Пишет событие в preorder_log."""
        session.add(
            PreorderLog(order_id=order_id, event=event.value, shop_id=shop_id)
        )
        await session.flush()

    # ------------------------------------------------------------------
    # Обработчики планировщика
    # ------------------------------------------------------------------

    async def handle_reminder(self, session: AsyncSession, order_id: int) -> None:
        """За PREORDER_REMINDER_HOURS до даты — напоминаем магазинам (§7.2 п.5)."""
        orders = self._orders
        order = await orders.get_order(session, order_id)  # type: ignore[attr-defined]
        if order is None or order.order_type != OrderType.PREORDER.value:
            return
        if order.status not in (
            OrderStatus.PREORDER_PENDING.value,
            OrderStatus.PREORDER_REMINDED.value,
        ):
            return

        from app.keyboards.seller_kb import preorder_reminder_keyboard

        for sub in order.sub_orders:
            if sub.status != SubOrderStatus.PENDING.value or not sub.shop:
                continue
            lines = [
                "⏰ Напоминание: предзаказ на "
                f"{order.scheduled_at:%d.%m %H:%M}" if order.scheduled_at else "⏰ Напоминание",
            ]
            for item in sub.items:
                unit = item.unit or ""
                lines.append(f"• {item.raw_name} — {item.qty or 0:g} {unit}")
            lines.append("Подтвердите наличие:")
            await self._notifier.notify_shop(
                session,
                sub.shop,
                "\n".join(lines),
                keyboard=preorder_reminder_keyboard(sub.id),
            )

        order.status = OrderStatus.PREORDER_REMINDED.value
        order.reminder_sent_at = utcnow()
        await session.flush()
        await self._log_event(session, order.id, PreorderEvent.REMINDER_SENT)
        # Покупателю — статус «ждём подтверждения»
        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"⏰ Напоминание: {order.scheduled_at:%d.%m, %H:%M} ваш предзаказ.\n"
            "Магазины подтверждают наличие...",
        )

    async def handle_confirm_deadline(self, session: AsyncSession, order_id: int) -> None:
        """За PREORDER_SELLER_CONFIRM_HOURS до даты — кто не подтвердил, помечаем."""
        orders = self._orders
        order = await orders.get_order(session, order_id)  # type: ignore[attr-defined]
        if order is None or order.order_type != OrderType.PREORDER.value:
            return
        if order.status not in (
            OrderStatus.PREORDER_PENDING.value,
            OrderStatus.PREORDER_REMINDED.value,
        ):
            return
        # Проверяем подтверждения: подтверждённый подзаказ должен быть CONFIRMED
        has_unconfirmed = any(
            s.status in (SubOrderStatus.PENDING.value, SubOrderStatus.REJECTED.value)
            for s in order.sub_orders
        )
        if has_unconfirmed:
            order.search_other_at = utcnow()
            await session.flush()

    async def handle_search_other(self, session: AsyncSession, order_id: int) -> None:
        """За PREORDER_SEARCH_OTHER_HOURS — ищем замену неподтвердившим (§7.2 п.7)."""
        orders = self._orders
        order = await orders.get_order(session, order_id)  # type: ignore[attr-defined]
        if order is None or order.order_type != OrderType.PREORDER.value:
            return
        if order.status not in (
            OrderStatus.PREORDER_PENDING.value,
            OrderStatus.PREORDER_REMINDED.value,
            OrderStatus.PREORDER_SEARCHING.value,
        ):
            return

        await self._log_event(session, order.id, PreorderEvent.SEARCH_OTHER)
        order.status = OrderStatus.PREORDER_SEARCHING.value
        await session.flush()

        # Позиции неподтвердивших магазинов
        orphan_items = []
        for sub in order.sub_orders:
            if sub.status in (SubOrderStatus.PENDING.value, SubOrderStatus.REJECTED.value):
                if sub.shop:
                    await self._notifier.notify_shop(
                        session,
                        sub.shop,
                        f"⚠️ Вы не подтвердили предзаказ #{order.id}. "
                        "Он передан другому магазину.",
                    )
                orphan_items.extend(sub.items)
                sub.status = SubOrderStatus.CANCELLED.value
        await session.flush()

        if not orphan_items:
            # Все подтвердили — возвращаем к подтверждённому
            order.status = OrderStatus.PREORDER_CONFIRMED.value
            await session.flush()
            return

        # Ищем замену: пересобираем запрос из orphan_items по тем же названиям
        client = order.client
        if client is None:
            return
        lines = []
        for item in orphan_items:
            unit = item.unit or ""
            qty = item.qty or 0
            lines.append(f"{item.raw_name} {qty:g} {unit}".strip())
        text = ", ".join(lines)

        # Отменяем старые job-таймеры и создаём новый заказ (на ту же дату)
        new_order, msg = await orders.create_from_text(  # type: ignore[attr-defined]
            session,
            client,
            text,
            scheduled_at=order.scheduled_at,
        )
        if new_order is not None:
            # Переносим ссылку: старый заказ отменяем
            self.cancel_preorder_jobs(order.id)
            order.status = OrderStatus.PREORDER_CANCELLED.value
            await self._log_event(session, order.id, PreorderEvent.CANCELLED)
            await session.flush()
            await self.schedule_preorder_jobs(session, new_order)
            await self._notifier.send(
                session,
                client.telegram_id,
                f"⚠️ Магазин не подтвердил предзаказ #{order.id}.\n"
                f"Ищем другой магазин...\n✅ Нашли: заказ #{new_order.id} оформлен у других продавцов.\n{msg}",
            )
        else:
            await self._notifier.send(
                session,
                client.telegram_id,
                f"⚠️ Магазин не подтвердил предзаказ #{order.id}, "
                f"и запасной не нашёлся.\n{msg}",
            )

    async def handle_client_deadline(self, session: AsyncSession, order_id: int) -> None:
        """За PREORDER_CLIENT_CONFIRM_HOURS — если покупатель не подтвердил, отмена."""
        orders = self._orders
        order = await orders.get_order(session, order_id)  # type: ignore[attr-defined]
        if order is None or order.order_type != OrderType.PREORDER.value:
            return
        if order.status not in (
            OrderStatus.PREORDER_PENDING.value,
            OrderStatus.PREORDER_REMINDED.value,
            OrderStatus.PREORDER_CONFIRMED.value,
            OrderStatus.PREORDER_SEARCHING.value,
        ):
            return
        # Если все подзаказы подтверждены — предзаказ живёт до дня X; тут
        # отменяем только если магазины так и не подтвердили состав
        all_confirmed = all(
            s.status == SubOrderStatus.CONFIRMED.value for s in order.sub_orders
        )
        if all_confirmed:
            order.status = OrderStatus.PREORDER_CONFIRMED.value
            await session.flush()
            return
        await orders.client_cancel(  # type: ignore[attr-defined]
            session, order, reason="Не подтверждено вовремя"
        )
        await self._log_event(session, order.id, PreorderEvent.CANCELLED)

    # ------------------------------------------------------------------
    # Ответы магазина по предзаказу
    # ------------------------------------------------------------------

    async def seller_confirmed_sub(
        self, session: AsyncSession, sub_order: SubOrder
    ) -> None:
        """Магазин подтвердил предзаказ досрочно или по напоминанию (§7.5)."""
        orders = self._orders
        order = await orders.get_order(session, sub_order.order_id)  # type: ignore[attr-defined]
        if order is None:
            return
        await self._log_event(
            session, order.id, PreorderEvent.SELLER_CONFIRMED, shop_id=sub_order.shop_id
        )
        # Когда все подтверждены — статус confirmed, уведомляем покупателя
        if all(
            s.status == SubOrderStatus.CONFIRMED.value for s in order.sub_orders
        ):
            order.status = OrderStatus.PREORDER_CONFIRMED.value
            order.seller_confirmed_at = utcnow()
            await session.flush()
            shops = ", ".join(
                f"«{s.shop.name}», {s.shop.address_text}"
                for s in order.sub_orders
                if s.shop
            )
            when = (
                f"{order.scheduled_at:%d.%m, %H:%M}" if order.scheduled_at else ""
            )
            await self._notifier.send(
                session,
                order.client.telegram_id,
                f"✅ Магазины подтвердили предзаказ{f' на {when}' if when else ''}.\n"
                f"📍 Заберите: {shops}.\n"
                "В день заказа напомню о сборке.",
            )

    async def seller_rejected_sub(
        self, session: AsyncSession, sub_order: SubOrder
    ) -> None:
        """Магазин отказал по предзаказу — логируем."""
        order = await self._orders.get_order(session, sub_order.order_id)  # type: ignore[attr-defined]
        if order is None:
            return
        await self._log_event(
            session, order.id, PreorderEvent.SELLER_REJECTED, shop_id=sub_order.shop_id
        )
        if sub_order.shop:
            await self._notifier.notify_shop(
                session,
                sub_order.shop,
                f"❌ Вы отклонили предзаказ #{order.id}. "
                "Если передумаете — предзаказ можно переоформить.",
            )

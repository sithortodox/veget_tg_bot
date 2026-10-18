"""Хендлеры курьера (K1–K4)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import OrderStatus, Role
from app.db.models import Order, SubOrder, User
from app.keyboards.courier_kb import courier_order_keyboard
from app.services.order import OrderService
from app.services.registry import ServiceRegistry

logger = logging.getLogger(__name__)
router = Router(name="courier")


@router.message(F.text == "🛵 Мои доставки")
async def menu_deliveries(message: Message, data: dict) -> None:
    """Список доставок курьера."""
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    result = await session.execute(
        select(Order)
        .where(Order.courier_id == db_user.id)
        .where(
            Order.status.in_(
                [OrderStatus.DELIVERING.value, OrderStatus.READY.value]
            )
        )
        .options(
            selectinload(Order.sub_orders).selectinload(SubOrder.shop),
            selectinload(Order.client),
        )
        .order_by(Order.created_at.desc())
    )
    orders = list(result.scalars().all())
    if not orders:
        await message.answer("Пока доставок нет.")
        return
    for order in orders:
        lines = [f"🛵 Заказ #{order.id} · {order.status}"]
        for sub in order.sub_orders:
            if sub.shop:
                lines.append(f"🏪 «{sub.shop.name}», {sub.shop.address_text}")
        client_addr = order.client.address_text if order.client else "—"
        lines.append(f"🏠 {order.delivery_address or client_addr}")
        await message.answer(
            "\n".join(lines), keyboard=courier_order_keyboard(order.id)
        )


@router.callback_query(F.data.startswith("k:up:"))
async def courier_pickup(cb: CallbackQuery, data: dict) -> None:
    """Курьер забрал у магазина (K3)."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    db_user: User = data["db_user"]
    order_id = int(cb.data.split(":")[2])
    order = await orders.get_order(session, order_id)
    if order is None or order.courier_id != db_user.id:
        await cb.answer("Это не ваша доставка", show_alert=True)
        return
    await orders.courier_picked_up(session, order, changed_by=cb.from_user.id)
    await cb.answer("✅ Забрал")
    await cb.message.edit_text(
        f"Заказ #{order_id}: забран у магазина 🏪→🏠 Не забудьте доставить!"
    )


@router.callback_query(F.data.startswith("k:dn:"))
async def courier_delivered(cb: CallbackQuery, data: dict) -> None:
    """Курьер доставил покупателю (K4)."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    db_user: User = data["db_user"]
    order_id = int(cb.data.split(":")[2])
    order = await orders.get_order(session, order_id)
    if order is None or order.courier_id != db_user.id:
        await cb.answer("Это не ваша доставка", show_alert=True)
        return
    await orders.courier_delivered(session, order, changed_by=cb.from_user.id)
    await cb.answer("🏠 Доставлено")
    await cb.message.edit_text(
        f"Заказ #{order_id}: доставлен ✅ Ждём подтверждения оплаты от покупателя."
    )


@router.message(F.text == "👤 Профиль")
async def courier_profile(message: Message, data: dict) -> None:
    """Профиль курьера."""
    db_user: User = data["db_user"]
    if db_user.role != Role.COURIER.value:
        return
    await message.answer(
        f"🛵 {db_user.name or '—'}\n📱 {db_user.phone or '—'}"
    )

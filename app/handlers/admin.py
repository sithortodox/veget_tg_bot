"""Хендлеры администратора (§3.5): статистика, баны, споры."""

from __future__ import annotations

import asyncio
import html

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import OrderStatus, Role
from app.db.models import (
    BanHistory,
    Order,
    OrderItem,
    OrderStatusLog,
    Product,
    Shop,
    SubOrder,
    User,
)
from app.services.ban import BanService
from app.services.registry import ServiceRegistry

router = Router(name="admin")


def _is_admin(data: dict) -> bool:
    """Проверка прав админа: ID из конфига или роль в БД."""
    settings: Settings = data["app_settings"]
    user: User | None = data.get("db_user")
    if user is not None and user.role == Role.ADMIN.value:
        return True
    return bool(user and user.telegram_id in settings.admin_ids)


@router.message(Command("admin"))
async def cmd_admin(message: Message, data: dict) -> None:
    """Панель администратора."""
    if not _is_admin(data):
        await message.answer("Команда доступна только администратору.")
        return
    await message.answer(
        "🛡 Админ-панель\n"
        "/astats — статистика\n"
        "/ausers — пользователи\n"
        "/aban &lt;telegram_id&gt; — бан\n"
        "/aunban &lt;telegram_id&gt; — разбан\n"
        "/acancel &lt;order_id&gt; [причина] — отмена заказа\n"
        "/amass &lt;текст&gt; — рассылка всем\n"
        "/alogs — санкции и инциденты"
    )


@router.message(Command("astats"))
async def cmd_stats(message: Message, data: dict) -> None:
    """A2: статистика заказов и сумм."""
    if not _is_admin(data):
        return
    session: AsyncSession = data["session"]
    orders_total = await session.execute(select(func.count(Order.id)))
    revenue = await session.execute(
        select(func.coalesce(func.sum(Order.total_actual), 0)).where(
            Order.status == "done"
        )
    )
    shops = await session.execute(select(func.count(Shop.id)))
    users = await session.execute(select(func.count(User.id)))
    top_stmt = (
        select(
            func.coalesce(Product.name, OrderItem.raw_name).label("name"),
            OrderItem.unit,
            func.count(OrderItem.id).label("cnt"),
        )
        .outerjoin(Product, Product.id == OrderItem.product_id)
        .join(SubOrder, SubOrder.id == OrderItem.sub_order_id)
        .join(Order, Order.id == SubOrder.order_id)
        .where(
            Order.status.notin_(
                [
                    OrderStatus.CANCELLED.value,
                    OrderStatus.PREORDER_CANCELLED.value,
                    OrderStatus.NO_SHOW.value,
                    OrderStatus.REFUSED_PAYMENT.value,
                ]
            )
        )
        .group_by(func.coalesce(Product.name, OrderItem.raw_name), OrderItem.unit)
        .order_by(func.count(OrderItem.id).desc())
        .limit(5)
    )
    top = list((await session.execute(top_stmt)).all())
    lines = [
        "📊 Статистика\n"
        f"Заказов: {orders_total.scalar()}\n"
        f"Выручка (закрытые): {float(revenue.scalar() or 0):.0f} ₽\n"
        f"Магазинов: {shops.scalar()}\n"
        f"Пользователей: {users.scalar()}"
    ]
    if top:
        lines.append("\n🏆 Топ позиций:")
        for i, (name, unit, cnt) in enumerate(top, start=1):
            lines.append(f"{i}. {name} ({unit or 'шт'}) — {cnt} шт.")
    await message.answer("\n".join(lines))


@router.message(Command("ausers"))
async def cmd_users(message: Message, data: dict) -> None:
    """A1: список пользователей по ролям."""
    if not _is_admin(data):
        return
    session: AsyncSession = data["session"]
    result = await session.execute(
        select(User).order_by(User.created_at.desc()).limit(30)
    )
    users = list(result.scalars().all())
    lines = ["👥 Последние пользователи:"]
    for u in users:
        ban = " 🚫" if u.is_banned else ""
        lines.append(
            f"• {u.telegram_id} · {u.role} · {u.name or '—'}{ban}"
        )
    await message.answer("\n".join(lines)[:4000])


@router.message(Command("aban"))
async def cmd_ban(message: Message, data: dict) -> None:
    """A3: ручной бан по telegram_id (уровень повышается на 1)."""
    if not _is_admin(data):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /aban &lt;telegram_id&gt; [причина]")
        return
    session: AsyncSession = data["session"]
    services: ServiceRegistry = data["services"]
    ban: BanService = services["ban"]
    telegram_id = int(parts[1])
    reason = " ".join(parts[2:]) or "Бан администратором"
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    level = await ban.register_violation(session, user, reason=reason)
    await message.answer(f"🚫 Пользователь {telegram_id} забанен (уровень {level}).")


@router.message(Command("aunban"))
async def cmd_unban(message: Message, data: dict) -> None:
    """A3: разбан."""
    if not _is_admin(data):
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /aunban &lt;telegram_id&gt;")
        return
    session: AsyncSession = data["session"]
    services: ServiceRegistry = data["services"]
    ban: BanService = services["ban"]
    telegram_id = int(parts[1])
    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        await message.answer("Пользователь не найден.")
        return
    await ban.unban(session, user)
    await message.answer(f"✅ Пользователь {telegram_id} разбанен.")


@router.message(Command("acancel"))
async def cmd_acancel(message: Message, data: dict) -> None:
    """A4: принудительная отмена заказа на любом этапе."""
    if not _is_admin(data):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /acancel &lt;order_id&gt; [причина]")
        return
    session: AsyncSession = data["session"]
    services: ServiceRegistry = data["services"]
    order_id = int(parts[1])
    reason = parts[2] if len(parts) > 2 else ""
    order = await session.get(Order, order_id)
    if order is None:
        await message.answer(f"Заказ #{order_id} не найден.")
        return
    admin_id = message.from_user.id if message.from_user else 0
    ok = await services["orders"].admin_cancel(
        session, order, admin_id=admin_id, reason=reason
    )
    if ok:
        await message.answer(f"🛑 Заказ #{order_id} отменён администратором.")
    else:
        await message.answer(
            f"Заказ #{order_id} уже закрыт ({order.status}) — отмена не нужна."
        )


@router.message(Command("amass"))
async def cmd_amass(message: Message, data: dict) -> None:
    """A5: рассылка всем незабаненным пользователям."""
    if not _is_admin(data):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer("Использование: /amass &lt;текст сообщения&gt;")
        return
    session: AsyncSession = data["session"]
    services: ServiceRegistry = data["services"]
    notifier = services["notifier"]
    payload = html.escape(parts[1].strip())
    result = await session.execute(
        select(User.telegram_id).where(User.is_banned.is_(False))
    )
    recipients = [row[0] for row in result.all()]
    sent = 0
    for telegram_id in recipients:
        if await notifier.send(session, telegram_id, payload):
            sent += 1
        await asyncio.sleep(0.05)
    await message.answer(f"📤 Рассылка: {sent} из {len(recipients)} доставлено.")


@router.message(Command("alogs"))
async def cmd_logs(message: Message, data: dict) -> None:
    """A6: последние баны/инциденты."""
    if not _is_admin(data):
        return
    session: AsyncSession = data["session"]
    result = await session.execute(
        select(BanHistory).order_by(BanHistory.created_at.desc()).limit(10)
    )
    rows = list(result.scalars().all())
    lines = ["📜 Санкции:"]
    if rows:
        for r in rows:
            lines.append(
                f"• user {r.user_id} · уровень {r.level} · {r.reason or '—'} · "
                f"{r.created_at:%d.%m %H:%M}"
            )
    else:
        lines.append("— нет —")
    incident_stmt = (
        select(OrderStatusLog)
        .where(
            OrderStatusLog.status.in_(
                [
                    OrderStatus.CANCELLED.value,
                    OrderStatus.PREORDER_CANCELLED.value,
                    OrderStatus.NO_SHOW.value,
                    OrderStatus.REFUSED_PAYMENT.value,
                ]
            )
        )
        .order_by(OrderStatusLog.created_at.desc())
        .limit(10)
    )
    incidents = list((await session.execute(incident_stmt)).scalars().all())
    lines.append("\n⚠️ Инциденты:")
    if incidents:
        for r in incidents:
            who = f"admin {r.changed_by}" if r.changed_by else "система"
            lines.append(
                f"• заказ #{r.order_id} · {r.status} · {who} · "
                f"{r.created_at:%d.%m %H:%M}"
            )
    else:
        lines.append("— нет —")
    await message.answer("\n".join(lines)[:4000])

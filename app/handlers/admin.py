"""Хендлеры администратора (§3.5): статистика, баны, споры."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import Role
from app.db.models import BanHistory, Order, Shop, User
from app.handlers.common import _services
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
        "/aban <telegram_id> — бан\n"
        "/aunban <telegram_id> — разбан"
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
    await message.answer(
        "📊 Статистика\n"
        f"Заказов: {orders_total.scalar()}\n"
        f"Выручка (закрытые): {float(revenue.scalar() or 0):.0f} ₽\n"
        f"Магазинов: {shops.scalar()}\n"
        f"Пользователей: {users.scalar()}"
    )


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
        await message.answer("Использование: /aban <telegram_id> [причина]")
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
        await message.answer("Использование: /aunban <telegram_id>")
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


@router.message(Command("alogs"))
async def cmd_logs(message: Message, data: dict) -> None:
    """A6: последние баны/инциденты."""
    if not _is_admin(data):
        return
    session: AsyncSession = data["session"]
    result = await session.execute(
        select(BanHistory).order_by(BanHistory.created_at.desc()).limit(15)
    )
    rows = list(result.scalars().all())
    if not rows:
        await message.answer("Логов пока нет.")
        return
    lines = ["📜 Последние санкции:"]
    for r in rows:
        lines.append(
            f"• user {r.user_id} · уровень {r.level} · {r.reason or '—'} · "
            f"{r.created_at:%d.%m %H:%M}"
        )
    await message.answer("\n".join(lines))

"""Middleware: загрузка/создание пользователя, проверка бана, сервисы.

Роль пользователя определяется по telegram_id в таблице users (§2).
Забаненный пользователь (кроме админов) получает сообщение и дальше не проходит.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import Role
from app.db.models import User
from app.services.ban import BanService
from app.services.registry import ServiceRegistry


def _extract_telegram_id(event: TelegramObject) -> int | None:
    """Достаёт telegram_id из Message/CallbackQuery."""
    if isinstance(event, (Message, CallbackQuery)):
        if event.from_user:
            return event.from_user.id
    return None


class UserMiddleware:
    """Фабрика middleware: подставляет user, services, проверяет бан."""

    def __init__(self, registry: ServiceRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings

    def __call__(self) -> "UserLoadingMiddleware":
        return UserLoadingMiddleware(self._registry, self._settings)


class UserLoadingMiddleware(BaseMiddleware):
    """Непосредственная загрузка пользователя."""

    def __init__(self, registry: ServiceRegistry, settings: Settings) -> None:
        self._registry = registry
        self._settings = settings

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        telegram_id = _extract_telegram_id(event)
        session: AsyncSession | None = data.get("session")
        if session is None or telegram_id is None:
            return await handler(event, data)

        # Реестр сервисов доступен всем хендлерам
        data["services"] = self._registry
        data["app_settings"] = self._settings

        # Ищем пользователя; создаём заглушку до завершения регистрации
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, role=Role.CLIENT.value)
            session.add(user)
            await session.flush()

        # Снимаем истёкший бан автоматически
        await BanService.clear_if_expired(session, user)
        data["db_user"] = user

        # Проверка активного бана (админы не блокируются)
        is_admin = telegram_id in self._settings.admin_ids or user.role == Role.ADMIN.value
        if not is_admin and BanService.is_currently_banned(user):
            if isinstance(event, Message):
                until = (
                    "бессрочно"
                    if user.banned_until is None
                    else f"до {user.banned_until:%d.%m.%Y %H:%M} UTC"
                )
                await event.answer(
                    f"🚫 Доступ ограничен ({until}).\nПричина: {user.ban_reason or 'нарушение правил'}."
                )
            return None  # не пропускаем дальше

        return await handler(event, data)

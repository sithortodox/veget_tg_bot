"""Система банов (§5.4 ТЗ): 4 уровня санкций за неявку/неоплату.

Считается нарушением:
- заказ собран, покупатель не оплатил/не пришёл за TIMEOUT_CLIENT_PICKUP;
- отказ от оплаты после сборки.

НЕ считается: магазин не собрал; отмена до сборки; техсбой; форс-мажор.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import BAN_LEVELS, Role
from app.db.models import BanHistory, User
from app.services.notifier import Notifier

logger = logging.getLogger(__name__)


class BanService:
    """Начисление нарушений и применение банов."""

    def __init__(self, settings: Settings, notifier: Notifier) -> None:
        self._settings = settings
        self._notifier = notifier

    async def register_violation(
        self,
        session: AsyncSession,
        user: User,
        reason: str,
        order_id: int | None = None,
    ) -> int:
        """Фиксирует нарушение: увеличивает счётчик и применяет санкцию.

        Возвращает применённый уровень (1..4).
        """
        user.ban_count = (user.ban_count or 0) + 1
        level = min(user.ban_count, 4)
        info = BAN_LEVELS[level]
        days = int(info["days"])
        now = datetime.now(timezone.utc)

        if days == -1:
            # Бессрочный бан
            user.is_banned = True
            user.banned_until = None
        elif days > 0:
            user.is_banned = True
            user.banned_until = now + timedelta(days=days)
        else:
            # Уровень 1 — только предупреждение, бан не ставим
            user.is_banned = False
            user.banned_until = None

        user.ban_reason = reason
        user.banned_at = now

        session.add(
            BanHistory(
                user_id=user.id,
                level=level,
                reason=reason,
                banned_until=user.banned_until,
                order_id=order_id,
            )
        )
        await session.flush()

        # Сообщаем пользователю о санкции
        ban_text = str(info["text"])
        if level == 1:
            text = f"⚠️ {ban_text}\nПричина: {reason}"
        else:
            until = (
                "бессрочно"
                if days == -1
                else f"до {user.banned_until:%d.%m.%Y %H:%M} UTC"
            )
            text = f"🚫 {ban_text}\nСрок: {until}\nПричина: {reason}"
        await self._notifier.send(session, user.telegram_id, text)

        logger.info(
            "Нарушение уровня %s для user=%s: %s", level, user.telegram_id, reason
        )
        return level

    async def unban(self, session: AsyncSession, user: User, reason: str = "") -> None:
        """Снимает бан (админ)."""
        user.is_banned = False
        user.banned_until = None
        user.ban_reason = reason or "Разбан администратором"
        await session.flush()
        await self._notifier.send(
            session, user.telegram_id, "✅ Бан снят. Добро пожаловать!"
        )

    @staticmethod
    def is_currently_banned(user: User) -> bool:
        """Проверяет, активен ли бан прямо сейчас."""
        if not user.is_banned:
            return False
        if user.banned_until is None:
            return True  # бессрочный
        return user.banned_until > datetime.now(timezone.utc)

    @staticmethod
    async def clear_if_expired(session: AsyncSession, user: User) -> bool:
        """Автоматически снимает истёкший бан. Возвращает True, если бан снят."""
        if not user.is_banned or user.banned_until is None:
            return False
        if user.banned_until <= datetime.now(timezone.utc):
            user.is_banned = False
            user.ban_reason = "Срок бана истёк"
            await session.flush()
            return True
        return False

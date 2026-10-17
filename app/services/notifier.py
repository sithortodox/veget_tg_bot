"""Центральный сервис уведомлений.

Все исходящие сообщения проходят через Notifier: логируются в message_log,
ошибки доставки не роняют бизнес-процесс. Хендлеры не шлют сообщения напрямую.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageLog

logger = logging.getLogger(__name__)

Keyboard = Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup]


class Notifier:
    """Отправка сообщений пользователям с логированием."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def send(
        self,
        session: AsyncSession,
        telegram_id: int,
        text: str,
        keyboard: Keyboard = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Отправляет сообщение и пишет его в message_log.

        Возвращает False, если доставка невозможна (бан/блокировка/ошибка).
        """
        try:
            await self._bot.send_message(
                chat_id=telegram_id,
                text=text,
                reply_markup=keyboard,
                parse_mode=parse_mode,
            )
            ok = True
        except TelegramRetryAfter as exc:  # флуд-лимит: повторяем один раз
            logger.warning("Flood control for %s, retry in %s", telegram_id, exc.retry_after)
            try:
                await self._bot.send_message(
                    chat_id=telegram_id,
                    text=text,
                    reply_markup=keyboard,
                    parse_mode=parse_mode,
                )
                ok = True
            except Exception:  # noqa: BLE001
                logger.exception("Повторная отправка %s не удалась", telegram_id)
                ok = False
        except TelegramForbiddenError:
            # Пользователь заблокировал бота
            logger.info("Пользователь %s заблокировал бота", telegram_id)
            ok = False
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка отправки сообщения %s", telegram_id)
            ok = False

        session.add(
            MessageLog(user_id=telegram_id, direction="out", text=text[:4000])
        )
        await session.flush()
        return ok

    async def notify_shop(
        self,
        session: AsyncSession,
        shop: Any,
        text: str,
        keyboard: Keyboard = None,
    ) -> bool:
        """Уведомить продавца магазина."""
        if not shop.telegram_id:
            logger.warning("У магазина %s нет telegram_id", shop.id)
            return False
        return await self.send(session, shop.telegram_id, text, keyboard)

    async def notify_admins(
        self,
        session: AsyncSession,
        admin_ids: list[int],
        text: str,
    ) -> None:
        """Оповестить всех администраторов (спорные ситуации, инциденты)."""
        for admin_id in admin_ids:
            await self.send(session, admin_id, text)

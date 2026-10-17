"""Фикстуры сервисного слоя для интеграционных тестов.

Собирает реальные сервисы с:
- реальной БД (из conftest);
- MockNotifier — записывает сообщения вместо отправки в Telegram;
- FakeScheduler — записывает запланированные jobs, но не исполняет их
  (таймауты вызываются в тестах напрямую).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest_asyncio
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardMarkup
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.services.ban import BanService
from app.services.delivery import DeliveryService
from app.services.geo import GeoService, build_geo_service
from app.services.matcher import ProductMatcher
from app.services.notifier import Notifier
from app.services.order import OrderService
from app.services.parser import build_parser
from app.services.preorder import PreorderService
from app.services.registry import ServiceRegistry
from app.services.scheduler import SchedulerService, TimeoutRunner


class FakeScheduler(SchedulerService):
    """Планировщик-заглушка: только журнал, без APScheduler."""

    def __init__(self) -> None:
        self.scheduled: list[tuple[str, float]] = []
        self.cancelled: list[str] = []

    def start(self) -> None:
        """Ничего не делает — реальный планировщик не нужен."""

    def shutdown(self) -> None:
        """Ничего не делает."""

    def schedule_once(self, job_id, delay_seconds, func, *args, **kwargs) -> None:
        """Записывает факт планирования."""
        self.scheduled.append((job_id, float(delay_seconds)))

    def cancel(self, job_id: str) -> None:
        """Записывает факт отмены."""
        self.cancelled.append(job_id)

    def cancel_prefix(self, prefix: str) -> None:
        """Записывает отмену префикса."""
        self.cancelled.append(prefix)


class MockNotifier(Notifier):
    """Записывает все исходящие сообщения в журнал."""

    def __init__(self) -> None:
        super().__init__(bot=None)  # type: ignore[arg-type]
        self.messages: list[dict[str, Any]] = []

    async def send(
        self,
        session: AsyncSession,
        telegram_id: int,
        text: str,
        keyboard: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
        parse_mode: Optional[str] = None,
    ) -> bool:
        """Фиксирует сообщение вместо отправки."""
        self.messages.append(
            {"telegram_id": telegram_id, "text": text, "keyboard": keyboard}
        )
        return True

    def texts_for(self, telegram_id: int) -> list[str]:
        """Все тексты, отправленные указанному пользователю."""
        return [m["text"] for m in self.messages if m["telegram_id"] == telegram_id]

    def photos_for(self, telegram_id: int) -> list[dict[str, Any]]:
        """Все фото, отправленные указанному пользователю."""
        return [
            m for m in self.messages if m["telegram_id"] == telegram_id and m.get("photo")
        ]

    async def send_photo(
        self,
        session: AsyncSession,
        telegram_id: int,
        photo_file_id: str,
        caption: Optional[str] = None,
        keyboard: Optional[InlineKeyboardMarkup | ReplyKeyboardMarkup] = None,
    ) -> bool:
        """Фиксирует фото вместо отправки."""
        self.messages.append(
            {
                "telegram_id": telegram_id,
                "text": f"[photo] {caption or photo_file_id}",
                "photo": photo_file_id,
                "caption": caption,
                "keyboard": keyboard,
            }
        )
        return True

    def last_for(self, telegram_id: int) -> Optional[str]:
        """Последнее сообщение пользователю."""
        msgs = self.texts_for(telegram_id)
        return msgs[-1] if msgs else None

    def clear(self) -> None:
        """Очищает журнал."""
        self.messages.clear()


@dataclass
class ServiceStack:
    """Готовый набор сервисов для интеграционного теста.

    Имя не начинается с Test*, чтобы pytest не пытался собрать как тест-класс.
    """

    registry: ServiceRegistry
    orders: OrderService
    preorders: PreorderService
    ban: BanService
    delivery: DeliveryService
    geo: GeoService
    notifier: MockNotifier
    scheduler: FakeScheduler
    session_factory: async_sessionmaker[AsyncSession]


@pytest_asyncio.fixture
async def stack(db, session_factory) -> ServiceStack:
    """Собирает сервисный стек поверх тестовой сессии `db`."""
    settings = get_settings()
    notifier = MockNotifier()
    geo = build_geo_service(settings)
    parser = build_parser(settings)  # без OPENAI_API_KEY — regex-only
    matcher = ProductMatcher(threshold=settings.match_threshold)
    ban = BanService(settings, notifier)
    delivery = DeliveryService(settings)
    scheduler = FakeScheduler()
    runner = TimeoutRunner(session_factory)

    orders = OrderService(
        settings=settings,
        parser=parser,
        matcher=matcher,
        geo=geo,
        notifier=notifier,
        ban=ban,
        scheduler=scheduler,
    )
    preorders = PreorderService(
        settings=settings,
        notifier=notifier,
        geo=geo,
        scheduler=scheduler,
        order_service=orders,
    )
    orders.set_timeout_runner(runner)
    orders.bind_delivery(delivery)
    preorders.set_timeout_runner(runner)
    runner.bind(orders, preorders)

    registry = ServiceRegistry()
    registry.register("orders", orders)
    registry.register("preorders", preorders)
    registry.register("ban", ban)
    registry.register("delivery", delivery)
    registry.register("geo", geo)
    registry.register("notifier", notifier)
    registry.register("scheduler", scheduler)

    return ServiceStack(
        registry=registry,
        orders=orders,
        preorders=preorders,
        ban=ban,
        delivery=delivery,
        geo=geo,
        notifier=notifier,
        scheduler=scheduler,
        session_factory=session_factory,
    )

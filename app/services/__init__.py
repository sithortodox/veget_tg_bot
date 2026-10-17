"""Сборка и регистрация всех сервисов приложения.

`setup_services` создаёт объекты и кладёт их в ServiceRegistry.
Хендлеры и плагины работают только через реестр — это точка расширения:
новый сервис = новый модуль + одна строка здесь (или регистрация из плагина).
"""

from __future__ import annotations

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
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


def setup_services(
    settings: Settings,
    bot: Bot,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[ServiceRegistry, SchedulerService, TimeoutRunner]:
    """Создаёт и связывает все сервисы. Возвращает реестр, планировщик, раннер."""
    registry = ServiceRegistry()

    # --- Базовые сервисы ---
    notifier = Notifier(bot)
    geo = build_geo_service(settings)
    parser = build_parser(settings)
    matcher = ProductMatcher(threshold=settings.match_threshold)
    ban = BanService(settings, notifier)
    delivery = DeliveryService(settings)
    scheduler = SchedulerService()
    timeout_runner = TimeoutRunner(session_factory)

    # --- Сервисы с зависимостями ---
    order_service = OrderService(
        settings=settings,
        parser=parser,
        matcher=matcher,
        geo=geo,
        notifier=notifier,
        ban=ban,
        scheduler=scheduler,
    )
    preorder_service = PreorderService(
        settings=settings,
        notifier=notifier,
        geo=geo,
        scheduler=scheduler,
        order_service=order_service,
    )

    # Связываем таймауты (разрываем цикл «сервис → планировщик → сервис»)
    order_service.set_timeout_runner(timeout_runner)
    order_service.bind_delivery(delivery)
    preorder_service.set_timeout_runner(timeout_runner)
    timeout_runner.bind(order_service, preorder_service)

    # --- Регистрация в реестре (ключ = имя для хендлеров/плагинов) ---
    registry.register("notifier", notifier)
    registry.register("geo", geo)
    registry.register("parser", parser)
    registry.register("matcher", matcher)
    registry.register("ban", ban)
    registry.register("delivery", delivery)
    registry.register("scheduler", scheduler)
    registry.register("timeouts", timeout_runner)
    registry.register("orders", order_service)
    registry.register("preorders", preorder_service)
    registry.register("settings", settings)
    registry.register("session_factory", session_factory)

    return registry, scheduler, timeout_runner

"""Точка входа Telegram-бота «ОВОЩНОЙ».

Сборка: конфиг → БД → бот/диспетчер → сервисы → middleware → роутеры →
плагины → планировщик предзаказов/таймаутов → polling.

Запуск: python -m app.main
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, get_settings
from app.db import Base, build_engine, build_session_factory
from app.handlers import register_routers
from app.middlewares import DbSessionMiddleware, UserMiddleware
from app.plugins import PluginContext, load_plugins
from app.services import setup_services

logger = logging.getLogger("ovoshnoy")


def setup_logging(settings: Settings) -> None:
    """Настройка логирования: DEBUG-режим из конфига."""
    level = logging.DEBUG if settings.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Тихие библиотеки
    logging.getLogger("aiogram").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def on_startup(
    engine,  # AsyncEngine
    registry,
    scheduler,
    preorder_service,
    session_factory: async_sessionmaker,
) -> None:
    """Действия при старте: таблицы, планировщик, восстановление предзаказов."""
    # create_all — для разработки; продакшен использует sql/init.sql
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    scheduler.start()

    # Перепланируем активные предзаказы после рестарта бота
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.constants import OrderStatus, OrderType
    from app.db.models import Order

    async with session_factory() as session:
        result = await session.execute(
            select(Order).where(
                Order.order_type == OrderType.PREORDER.value,
                Order.status.in_(
                    [
                        OrderStatus.PREORDER_PENDING.value,
                        OrderStatus.PREORDER_REMINDED.value,
                        OrderStatus.PREORDER_SEARCHING.value,
                    ]
                ),
                Order.scheduled_at > datetime.now(timezone.utc),
            )
        )
        active = list(result.scalars().all())
        for order in active:
            await preorder_service.schedule_preorder_jobs(session, order)
        await session.commit()
    if active:
        logger.info("Восстановлено предзаказов: %s", len(active))


async def on_shutdown(scheduler) -> None:
    """Корректное завершение."""
    scheduler.shutdown()
    logger.info("Бот остановлен")


async def amain() -> None:
    """Главная асинхронная точка входа."""
    settings = get_settings()
    setup_logging(settings)
    logger.info("Запуск «ОВОЩНОЙ»...")

    # --- БД ---
    engine = build_engine()
    session_factory: async_sessionmaker = build_session_factory(engine)

    # --- Бот и диспетчер ---
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # FSM в Redis (MemoryStorage — только для отладки без Redis)
    try:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.ping()
        storage = RedisStorage(redis)
        logger.info("FSM: RedisStorage")
    except Exception:  # noqa: BLE001
        logger.warning("Redis недоступен — используем MemoryStorage (dev)")
        storage = MemoryStorage()

    dp = Dispatcher(storage=storage)

    # --- Сервисы ---
    registry, scheduler, timeout_runner = setup_services(
        settings, bot, session_factory
    )

    # --- Middleware (порядок важен: сначала сессия, затем пользователь) ---
    dp.update.middleware(DbSessionMiddleware(session_factory))
    dp.update.middleware(UserMiddleware(registry, settings).__call__())

    # --- Роутеры ---
    register_routers(dp)

    # --- Плагины (точка расширения без правки ядра) ---
    ctx = PluginContext(
        bot=bot,
        dp=dp,
        services=registry,
        scheduler=scheduler,
        settings=settings,
        session_factory=session_factory,
    )
    await load_plugins(ctx)

    # --- Старт/стоп хуки ---
    dp.startup.register(
        lambda: on_startup(engine, registry, scheduler, registry.get("preorders"), session_factory)
    )
    dp.shutdown.register(lambda: on_shutdown(scheduler))

    try:
        # Удаляем вебхуки и запускаем long-polling
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        await engine.dispose()


def main() -> None:
    """Синхронная обёртка."""
    try:
        asyncio.run(amain())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановлено пользователем")


if __name__ == "__main__":
    main()

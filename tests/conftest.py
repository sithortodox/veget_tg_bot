"""Общие фикстуры интеграционных тестов.

Требуют запущенных PostgreSQL+PostGIS и Redis (docker compose up -d).
Перед каждым тестом таблицы очищаются, чтобы тесты были независимы.
"""

from __future__ import annotations

import os

# Настройки окружения до импорта приложения
os.environ.setdefault("BOT_TOKEN", "test:token")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://ovoshnoy:ovoshnoy@localhost:5432/ovoshnoy",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")

import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Фикстуры сервисного слоя (MockNotifier, FakeScheduler, stack)
pytest_plugins = ["tests.services_fixture"]

from app.db.base import Base
from app.db.models import (
    BanHistory,
    MessageLog,
    Order,
    OrderItem,
    OrderStatusLog,
    PreorderLog,
    Product,
    Shop,
    SubOrder,
    User,
)

# Таблицы в порядке зависимостей (удаление/очистка)
ALL_TABLES = [
    MessageLog.__tablename__,
    PreorderLog.__tablename__,
    BanHistory.__tablename__,
    OrderStatusLog.__tablename__,
    OrderItem.__tablename__,
    SubOrder.__tablename__,
    Order.__tablename__,
    Product.__tablename__,
    Shop.__tablename__,
    User.__tablename__,
]


@pytest_asyncio.fixture
async def engine():
    """Async-движок к тестовой БД."""
    url = os.environ["DATABASE_URL"]
    eng = create_async_engine(url, pool_pre_ping=True)
    # Расширения (если БД создавалась без init.sql)
    async with eng.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий."""
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory):
    """Сессия с очисткой таблиц до и после теста."""
    async with session_factory() as session:
        # Очистка (TRUNCATE с RESET IDENTITY)
        rows = ", ".join(ALL_TABLES)
        await session.execute(
            text(f"TRUNCATE {rows} RESTART IDENTITY CASCADE")
        )
        await session.commit()
        yield session
        await session.rollback()
        rows = ", ".join(ALL_TABLES)
        await session.execute(text(f"TRUNCATE {rows} RESTART IDENTITY CASCADE"))
        await session.commit()


# ----------------------------------------------------------------------
# Полезные билдеры сущностей для тестов
# ----------------------------------------------------------------------


async def make_user(
    db: AsyncSession,
    telegram_id: int = 100,
    role: str = "client",
    lat: float = 45.0443,
    lon: float = 38.9763,
    **kwargs,
) -> User:
    """Создаёт пользователя с координатами (ст. Елизаветинская — пилот)."""
    user = User(
        telegram_id=telegram_id,
        role=role,
        name=kwargs.pop("name", f"User{telegram_id}"),
        phone=kwargs.pop("phone", f"+7999000{telegram_id % 10000:04d}"),
        address_text=kwargs.pop("address_text", "г. Краснодар, ул. Ленина, 15"),
        address_geo=kwargs.pop("address_geo", f"POINT({lon} {lat})"),
        **kwargs,
    )
    db.add(user)
    await db.flush()
    return user


async def make_shop(
    db: AsyncSession,
    telegram_id: int = 200,
    lat: float = 45.04435,
    lon: float = 38.97635,
    name: str = "У Пети",
    is_active: bool = True,
) -> Shop:
    """Создаёт магазин рядом с пилотной точкой."""
    shop = Shop(
        telegram_id=telegram_id,
        name=name,
        address_text=f"г. Краснодар, ул. Ленина, {telegram_id % 100}",
        address_geo=f"POINT({lon} {lat})",
        phone="+79991112233",
        payment_details="карта 2200",
        is_active=is_active,
        moderation_status="approved",
    )
    db.add(shop)
    await db.flush()
    return shop


async def make_product(
    db: AsyncSession,
    shop: Shop,
    name: str = "Помидоры",
    price: float = 120.0,
    unit: str = "кг",
    synonyms: list[str] | None = None,
) -> Product:
    """Создаёт карточку товара."""
    product = Product(
        shop_id=shop.id,
        name=name,
        price=price,
        unit=unit,
        synonyms=synonyms,
        is_active=True,
    )
    db.add(product)
    await db.flush()
    return product

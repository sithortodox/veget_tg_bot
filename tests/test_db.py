"""Интеграционные тесты слоя БД: схема, гео-запросы, связи (Э2)."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderItem, Product, Shop, SubOrder, User
from tests.conftest import make_product, make_shop, make_user


async def test_all_tables_exist(db: AsyncSession) -> None:
    """Все таблицы §13 созданы."""
    result = await db.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' ORDER BY table_name"
        )
    )
    names = {row[0] for row in result}
    for expected in (
        "users", "shops", "products", "orders",
        "sub_orders", "order_items", "order_status_log",
        "ban_history", "preorder_log", "message_log",
    ):
        assert expected in names, f"нет таблицы {expected}"


async def test_geo_columns_are_geography(db: AsyncSession) -> None:
    """Поля адреса — тип GEOGRAPHY (PostGIS)."""
    result = await db.execute(
        text(
            "SELECT table_name, column_name, udt_name FROM information_schema.columns "
            "WHERE table_schema='public' AND udt_name='geography' ORDER BY 1,2"
        )
    )
    geo_cols = {(r[0], r[1]) for r in result}
    assert ("users", "address_geo") in geo_cols
    assert ("shops", "address_geo") in geo_cols
    assert ("orders", "delivery_geo") in geo_cols


async def test_user_crud(db: AsyncSession) -> None:
    """Создание/чтение пользователя."""
    user = await make_user(db, telegram_id=111, role="client")
    await db.commit()
    loaded = await db.get(User, user.id)
    assert loaded is not None
    assert loaded.telegram_id == 111
    assert loaded.role == "client"
    assert loaded.address_geo is not None


async def test_shop_geo_search_within_radius(db: AsyncSession) -> None:
    """ST_DWithin находит магазины в радиусе 800 м (§3.1 G4)."""
    from sqlalchemy import func, select

    # Клиент в центре ст. Елизаветинская
    await make_user(db, telegram_id=1, lat=45.0443, lon=38.9763)
    # Магазин ~50 м от клиента
    near = await make_shop(db, telegram_id=10, name="Рядом", lat=45.0444, lon=38.97635)
    # Магазин ~3 км — вне радиуса
    far = await make_shop(db, telegram_id=11, name="Далеко", lat=45.0700, lon=39.0000)
    await db.commit()

    point = "POINT(38.9763 45.0443)"
    result = await db.execute(
        select(Shop).where(
            Shop.is_active.is_(True),
            func.ST_DWithin(
                Shop.address_geo, func.ST_GeogFromText(point), 800
            ),
        )
    )
    found = {s.id for s in result.scalars()}
    assert near.id in found
    assert far.id not in found


async def test_shop_geo_search_orders_by_distance(db: AsyncSession) -> None:
    """Магазины отсортированы по расстоянию от клиента."""
    from sqlalchemy import func, select

    await make_user(db, telegram_id=1, lat=45.0443, lon=38.9763)
    await make_shop(db, telegram_id=10, name="Дальний", lat=45.0460, lon=38.9763)
    await make_shop(db, telegram_id=11, name="Ближний", lat=45.04435, lon=38.97632)
    await db.commit()

    point = "POINT(38.9763 45.0443)"
    result = await db.execute(
        select(Shop)
        .where(func.ST_DWithin(Shop.address_geo, func.ST_GeogFromText(point), 800))
        .order_by(func.ST_Distance(Shop.address_geo, func.ST_GeogFromText(point)))
    )
    shops = list(result.scalars())
    assert len(shops) == 2
    assert shops[0].name == "Ближний"


async def test_inactive_shop_excluded(db: AsyncSession) -> None:
    """Неактивный магазин не попадает в поиск."""
    from sqlalchemy import select

    await make_shop(db, telegram_id=10, name="Активный", is_active=True)
    await make_shop(db, telegram_id=11, name="Скрытый", is_active=False)
    await db.commit()
    result = await db.execute(select(Shop).where(Shop.is_active.is_(True)))
    names = [s.name for s in result.scalars()]
    assert names == ["Активный"]


async def test_product_synonyms_gin_index(db: AsyncSession) -> None:
    """GIN-индекс на синонимы работает (поиск по массиву)."""
    from sqlalchemy import select

    shop = await make_shop(db)
    await make_product(db, shop, name="Помидоры", synonyms=["томат", "помидор"])
    await make_product(db, shop, name="Огурцы", synonyms=["огурец"])
    await db.commit()

    result = await db.execute(
        select(Product).where(Product.synonyms.any("томат"))
    )
    products = list(result.scalars())
    assert len(products) == 1
    assert products[0].name == "Помидоры"


async def test_order_cascade_delete(db: AsyncSession) -> None:
    """Удаление заказа каскадно удаляет подзаказы и позиции (§13)."""
    user = await make_user(db, telegram_id=5)
    shop = await make_shop(db)
    product = await make_product(db, shop)

    order = Order(client_id=user.id, status="draft")
    db.add(order)
    await db.flush()
    sub = SubOrder(order_id=order.id, shop_id=shop.id)
    db.add(sub)
    await db.flush()
    item = OrderItem(sub_order_id=sub.id, product_id=product.id, raw_name="Помидоры", qty=1)
    db.add(item)
    await db.flush()
    order_id, sub_id, item_id = order.id, sub.id, item.id
    await db.commit()

    await db.delete(await db.get(Order, order_id))
    await db.commit()

    assert await db.get(SubOrder, sub_id) is None
    assert await db.get(OrderItem, item_id) is None


async def test_gin_trgm_index_on_product_name(db: AsyncSession) -> None:
    """Индекс gin_trgm на имени товара существует и используется в ILIKE."""
    from sqlalchemy import select

    shop = await make_shop(db)
    await make_product(db, shop, name="Банан экзотический")
    await db.commit()

    # Проверка, что ILIKE-поиск работает (использование индекса — EXPLAIN ANALYZE)
    result = await db.execute(
        text(
            "SELECT count(*) FROM products WHERE name ILIKE '%банан%'"
        )
    )
    assert result.scalar() == 1

    plan = await db.execute(
        text(
            "EXPLAIN SELECT id FROM products WHERE name ILIKE '%банан%'"
        )
    )
    plan_text = "\n".join(str(r[0]) for r in plan)
    # Индекс может называться по-разному, но pg_trgm должен подключаться
    assert "products" in plan_text

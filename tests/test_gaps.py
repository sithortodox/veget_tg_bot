"""Тесты пробелов ТЗ (Э5): Excel-импорт, фото, фактический вес, радиус доставки."""

from __future__ import annotations

import io
from typing import Optional

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DeliveryType,
    OrderStatus,
    PaymentMethod,
    SubOrderStatus,
)
from app.db.models import Order, Product
from app.services.catalog_import import import_catalog_rows, parse_catalog_rows
from app.services.delivery import DeliveryService
from app.services.geo import clamp_search_radius
from tests.conftest import make_product, make_shop, make_user
from tests.services_fixture import ServiceStack


# ======================================================================
# 1. Excel/CSV-импорт каталога (S4)
# ======================================================================


def test_parse_csv_rows_ru_headers() -> None:
    """CSV с русскими заголовками разбирается."""
    raw = (
        'Наименование,Цена,Ед,Описание,Синонимы\n'
        'Помидоры,120,кг,сливовидные,"томаты, синие"\n'
    ).encode("utf-8-sig")
    rows = parse_catalog_rows(raw, "прайс.csv")
    assert len(rows) == 1
    assert rows[0]["name"] == "Помидоры"
    assert rows[0]["price"] == "120"
    assert rows[0]["unit"] == "кг"
    assert rows[0]["synonyms"] == "томаты, синие"


def test_parse_csv_rows_en_headers() -> None:
    """CSV с английскими заголовками разбирается."""
    raw = b"name,price,unit,description\nCabbage,50,kg,fresh\n"
    rows = parse_catalog_rows(raw, "catalog.csv")
    assert rows[0]["name"] == "Cabbage"
    assert rows[0]["price"] == "50"
    assert rows[0]["unit"] == "kg"
    assert rows[0]["description"] == "fresh"
    # Нет колонки synonyms — ключа нет вовсе (не падаем на KeyError)
    assert "synonyms" not in rows[0] or rows[0].get("synonyms") in ("", None)


def test_parse_xlsx_rows() -> None:
    """Excel .xlsx разбирается через openpyxl."""
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["name", "price", "unit", "description"])
    ws.append(["Огурцы", 90, "кг", "гладкие"])
    ws.append(["", 10, "кг", "строка без названия пропускается"])
    buf = io.BytesIO()
    wb.save(buf)
    rows = parse_catalog_rows(buf.getvalue(), "прайс.xlsx")
    assert len(rows) == 1
    assert rows[0]["name"] == "Огурцы"
    assert rows[0]["price"] == "90"


def test_parse_unknown_extension_rejected() -> None:
    """Неизвестное расширение — ValueError с русским текстом."""
    with pytest.raises(ValueError, match="xlsx или .csv"):
        parse_catalog_rows(b"whatever", "price.txt")


async def test_import_upsert_no_duplicates(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Повторный импорт обновляет позицию, а не плодит дубли."""
    shop = await make_shop(db, telegram_id=200)
    await db.commit()

    first = [{"name": "Помидоры", "price": "120", "unit": "кг"}]
    added, updated = await import_catalog_rows(db, shop, first)
    assert (added, updated) == (1, 0)

    # Та же позиция с новой ценой (другой регистр названия)
    second = [{"name": "помидоры", "price": "150", "unit": "кг"}]
    added, updated = await import_catalog_rows(db, shop, second)
    await db.commit()
    assert (added, updated) == (0, 1)

    products = list(
        (await db.execute(select(Product).where(Product.shop_id == shop.id))).scalars()
    )
    assert len(products) == 1
    assert float(products[0].price) == 150.0


async def test_import_synonyms_and_bad_price_skipped(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Синонимы сохраняются; строка с неправильной ценой пропускается."""
    shop = await make_shop(db, telegram_id=201)
    await db.commit()
    rows = [
        {"name": "Клубника", "price": "300,5", "unit": "кг", "synonyms": "ягоды; земляника"},
        {"name": "Брак", "price": "не число", "unit": "кг"},
    ]
    added, updated = await import_catalog_rows(db, shop, rows)
    await db.commit()
    assert added == 1
    product = (
        await db.execute(select(Product).where(Product.name == "Клубника"))
    ).scalar_one()
    assert float(product.price) == 300.5
    assert product.synonyms == ["ягоды", "земляника"]


# ======================================================================
# 2. Фактический вес/количество при сборке (§4.1, §5.1)
# ======================================================================


async def _order_in_assembly(db: AsyncSession, stack: ServiceStack) -> tuple:
    """Помидоры+капуста → подтверждён → утверждён → статус сборки."""
    client = await make_user(db, telegram_id=100, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=200)
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await make_product(db, shop, name="Помидоры", price=200, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(
        db, client, "капуста 4 кг, помидоры 2 кг"
    )
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order,
        delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    return client, shop, order


async def test_actual_weight_recalculates_totals(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Сценарий §4.1: капуста 4 кг = 200 ₽, помидоры 1.2 кг = 240 ₽ → итого 440 ₽."""
    client, shop, order = await _order_in_assembly(db, stack)
    sub = order.sub_orders[0]
    assert order.status == OrderStatus.ASSEMBLING.value

    # План: капуста 4×50=200, помидоры 2×200=400 → 600
    facts: dict[int, tuple[float, float]] = {}
    for item in sub.items:
        name = (item.product.name if item.product else item.raw_name).lower()
        if name.startswith("капуста"):
            facts[item.id] = (4.0, 50.0)  # совпало с планом
        else:
            facts[item.id] = (1.2, 200.0)  # фактически меньше

    await stack.orders.mark_assembled(db, sub, actual_items=facts)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    sub = order.sub_orders[0]
    assert sub.status == SubOrderStatus.READY.value
    assert float(sub.subtotal_actual) == pytest.approx(440.0)
    # Заказ собран → фактическая сумма попадает в order.total_actual
    assert order.status == OrderStatus.READY.value
    assert float(order.total_actual) == pytest.approx(440.0)
    # И план остался нетронут
    assert float(sub.subtotal_estimated) == pytest.approx(600.0)
    # Клиент получил итог с фактической суммой
    assert any(
        "440" in m for m in stack.notifier.texts_for(client.telegram_id)
    )


async def test_photo_sent_to_client_on_ready(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Фото карточки (§3.3 S2) уходит покупателю при готовности заказа."""
    client = await make_user(db, telegram_id=110, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=210)
    product = await make_product(db, shop, name="Помидоры", price=120)
    product.photo_file_id = "AgACAgIAAxkBAAI"
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order,
        delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.mark_assembled(db, order.sub_orders[0])
    await db.commit()

    photos = stack.notifier.photos_for(client.telegram_id)
    assert len(photos) == 1
    assert photos[0]["photo"] == "AgACAgIAAxkBAAI"
    assert "помидоры" in (photos[0]["caption"] or "")


async def test_product_created_without_photo_keeps_none(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Карточка без фото — photo_file_id пуст, фото не шлётся."""
    client = await make_user(db, telegram_id=111, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=211)
    await make_product(db, shop, name="Огурцы", price=90)
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "огурцы 1 кг")
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order,
        delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.mark_assembled(db, order.sub_orders[0])
    await db.commit()
    assert stack.notifier.photos_for(client.telegram_id) == []


# ======================================================================
# 3. Отмена продавцом своей части с причиной (S6)
# ======================================================================


async def _multishop_in_assembly(db: AsyncSession, stack: ServiceStack) -> tuple:
    """Два магазина, оба подтвердили, заказ утверждён → сборка."""
    client = await make_user(db, telegram_id=120, lat=45.0443, lon=38.9763)
    shop_a = await make_shop(db, telegram_id=220, name="Магазин А")
    shop_b = await make_shop(db, telegram_id=221, name="Магазин Б")
    await make_product(db, shop_a, name="Помидоры", price=100)
    await make_product(db, shop_b, name="Клубника", price=300)
    await db.commit()

    order, _ = await stack.orders.create_from_text(
        db, client, "помидоры 1 кг, клубника 1 кг"
    )
    assert len(order.sub_orders) == 2
    for sub in order.sub_orders:
        await stack.orders.seller_respond(db, sub, accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order,
        delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    return client, order


async def test_seller_cancels_part_recalculates(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Магазин отменяет часть с причиной → сумма пересчитана, клиент знает."""
    client, order = await _multishop_in_assembly(db, stack)
    stack.notifier.clear()
    sub_a = next(
        s for s in order.sub_orders if s.shop and s.shop.name == "Магазин А"
    )
    ok = await stack.orders.seller_cancels_part(
        db, sub_a, reason="разбил ящик", changed_by=220
    )
    await db.commit()
    assert ok is True

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.ASSEMBLING.value  # вторая часть жива
    sub_a = next(s for s in order.sub_orders if s.id == sub_a.id)
    assert sub_a.status == SubOrderStatus.CANCELLED.value
    # Было 100 + 300 = 400, стало 300
    assert float(order.total_estimated) == pytest.approx(300.0)
    # Причина сохранена на позициях
    assert all("разбил ящик" in (i.note or "") for i in sub_a.items)
    # Клиент уведомлён с причиной и новой суммой
    msgs = stack.notifier.texts_for(client.telegram_id)
    assert any("отказался" in m and "разбил ящик" in m for m in msgs)
    assert any("300" in m for m in msgs)


async def test_seller_cancels_part_last_one_cancels_order(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Единственный магазин отменил свою часть → заказ целиком отменён."""
    client = await make_user(db, telegram_id=130, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=230)
    await make_product(db, shop, name="Помидоры", price=100)
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order,
        delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    stack.notifier.clear()

    ok = await stack.orders.seller_cancels_part(db, order.sub_orders[0])
    await db.commit()
    assert ok is True
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.CANCELLED.value
    assert any(
        "отменён" in m for m in stack.notifier.texts_for(client.telegram_id)
    )


async def test_seller_cancels_part_finalizes_when_rest_ready(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Остальные уже собраны → отмена последней части завершает заказ (READY)."""
    client, order = await _multishop_in_assembly(db, stack)
    sub_b = next(
        s for s in order.sub_orders if s.shop and s.shop.name == "Магазин Б"
    )
    # Собираем Б; А ещё не собран → заказ остаётся в assembly
    await stack.orders.mark_assembled(db, sub_b)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.ASSEMBLING.value

    sub_a = next(
        s for s in order.sub_orders if s.shop and s.shop.name == "Магазин А"
    )
    stack.notifier.clear()
    await stack.orders.seller_cancels_part(db, sub_a, reason="закончился")
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.READY.value
    assert float(order.total_actual) == pytest.approx(300.0)  # только Б
    assert any(
        "собран" in m for m in stack.notifier.texts_for(client.telegram_id)
    )


async def test_seller_cancels_part_blocked_outside_assembly(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Вне статуса сборки отмена части невозможна (вернёт False)."""
    client, order = await _multishop_in_assembly(db, stack)
    # Собираем обе части → заказ READY
    for sub in order.sub_orders:
        await stack.orders.mark_assembled(db, sub)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.READY.value

    ok = await stack.orders.seller_cancels_part(db, order.sub_orders[0])
    assert ok is False


# ======================================================================
# 4. Зона доставки (§6.3) и радиус поиска (G4)
# ======================================================================


async def test_delivery_zone_rejects_far_address(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Адрес дальше delivery_radius_m → client_approve кидает ValueError."""
    # Магазин ~300 м от клиента — в поиске (800 м) есть, в зоне 50 м — нет
    client = await make_user(db, telegram_id=140, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=240, lat=45.0470, lon=38.9763)
    await make_product(db, shop, name="Помидоры", price=100)
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    assert order is not None
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)

    # Зона доставки искусственно сужена — меняем сервис, не глобальные настройки
    original_delivery = stack.orders._delivery
    stack.orders.bind_delivery(DeliveryService(_settings_with(delivery_radius_m=50)))
    try:
        with pytest.raises(ValueError, match="зоны доставки"):
            await stack.orders.client_approve(
                db, order,
                delivery_type=DeliveryType.COURIER.value,
                payment_method=PaymentMethod.CASH.value,
            )
    finally:
        stack.orders.bind_delivery(original_delivery)

    # Заказ не утверждён — статус не изменился
    order_id = order.id  # id фиксируем до expire (иначе синхронный refresh)
    db.expire_all()
    order = await stack.orders.get_order(db, order_id)
    assert order.status == OrderStatus.PENDING_CLIENT.value
    assert order.delivery_type is None


async def test_delivery_zone_ok_within_radius(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """В зоне доставки → утверждение проходит, доставка включена в сумму."""
    client = await make_user(db, telegram_id=141, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=241)  # ~5 м от клиента
    await make_product(db, shop, name="Помидоры", price=100)
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order,
        delivery_type=DeliveryType.COURIER.value,
        payment_method=PaymentMethod.CASH.value,
    )
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.ASSEMBLING.value
    assert float(order.delivery_cost) == pytest.approx(100.0)
    # 100 (товары) + 100 (доставка)
    assert float(order.total_estimated) == pytest.approx(200.0)


async def test_zone_error_helper_ok_and_fail(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """zone_error_for_order: None в зоне, текст — вне зоны."""
    client = await make_user(db, telegram_id=142, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=242, lat=45.0470, lon=38.9763)
    await make_product(db, shop, name="Помидоры", price=100)
    await db.commit()
    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")

    default_err = stack.delivery.zone_error_for_order(order)
    assert default_err is None  # 300 м < 800 м по умолчанию

    narrow = DeliveryService(_settings_with(delivery_radius_m=50))
    err = narrow.zone_error_for_order(order)
    assert err is not None and "радиус 50 м" in err


async def test_custom_radius_filters_shop_search(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Персональный радиус (G4): 667-й магазин находится при 800 м, нет — при 300."""
    client = await make_user(db, telegram_id=143, lat=45.0443, lon=38.9763)
    # ~667 м севернее клиента (0.006° широты ≈ 667 м)
    shop = await make_shop(db, telegram_id=243, lat=45.0503, lon=38.9763)
    await make_product(db, shop, name="Помидоры", price=100)
    await db.commit()

    # Радиус 300 м — магазин не попадает
    order, text = await stack.orders.create_from_text(
        db, client, "помидоры 1 кг", radius_m=300
    )
    assert order is None
    assert "300" in text

    # Радиус 800 м (дефолт/максимум G4) — находится
    order, text = await stack.orders.create_from_text(
        db, client, "помидоры 1 кг", radius_m=800
    )
    assert order is not None
    assert order.status == OrderStatus.PENDING_SELLERS.value


def test_clamp_search_radius_bounds() -> None:
    """Клампинг радиуса к диапазону G4 300–800 м."""
    assert clamp_search_radius(100, 300, 800) == 300
    assert clamp_search_radius(1500, 300, 800) == 800
    assert clamp_search_radius(500, 300, 800) == 500
    assert clamp_search_radius(0, 300, 800) == 300


def _settings_with(**overrides) -> object:
    """Копия настроек с изменениями (для проверки зоны доставки)."""
    from app.config import get_settings

    base = get_settings()
    data = base.model_dump()
    data.update(overrides)
    from app.config import Settings

    return Settings(**{k: v for k, v in data.items() if k != "admin_ids"})

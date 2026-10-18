"""Тесты функциональных пробелов ТЗ: A2/A4/A5/A6, C4, C7, §9.2/§9.3."""

from __future__ import annotations

from sqlalchemy import select

from app.constants import OrderStatus, SubOrderStatus
from app.db.models import Order, OrderStatusLog, User
from app.services.geo import GeocodeResult, GeoPoint, point_from_wkt
from tests.conftest import make_product, make_shop, make_user
from tests.services_fixture import ServiceStack
from tests.test_e2e import E2EDriver

# ======================================================================
# 1. A4: принудительная отмена администратором (сервис)
# ======================================================================


async def test_admin_cancel_forces_any_stage(db, stack: ServiceStack) -> None:
    """admin_cancel останавливает заказ, снимает jobs и уведомляет."""
    client = await make_user(db, telegram_id=7001)
    shop = await make_shop(db, telegram_id=7011)
    await make_product(db, shop, name="Помидоры", price=100, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    assert order.status == OrderStatus.PENDING_SELLERS.value

    ok = await stack.orders.admin_cancel(db, order, admin_id=9001, reason="тест")
    assert ok is True
    assert order.status == OrderStatus.CANCELLED.value
    assert all(
        s.status == SubOrderStatus.CANCELLED.value for s in order.sub_orders
    )
    # Планировщик получил отмену всех jobs заказа
    assert f"order:{order.id}:" in stack.scheduler.cancelled
    # Уведомления клиенту и магазину
    assert any(
        "администратором" in m for m in stack.notifier.texts_for(7001)
    )
    # Лог статуса для /alogs (A6)
    logs = list(
        (
            await db.execute(
                select(OrderStatusLog).where(OrderStatusLog.order_id == order.id)
            )
        ).scalars()
    )
    assert any(r.status == OrderStatus.CANCELLED.value for r in logs)

    # Повторная отмена закрытого заказа невозможна
    ok_again = await stack.orders.admin_cancel(db, order, admin_id=9001)
    assert ok_again is False


# ======================================================================
# 2. C7: редактирование позиций (сервис)
# ======================================================================


async def test_edit_item_plus_minus_recalculates(db, stack: ServiceStack) -> None:
    """± меняет количество с шагом и пересчитывает итог."""
    client = await make_user(db, telegram_id=7101)
    shop = await make_shop(db, telegram_id=7111)
    await make_product(db, shop, name="Помидоры", price=100, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    assert order.status == OrderStatus.PENDING_CLIENT.value
    item = order.sub_orders[0].items[0]

    changed, note = await stack.orders.edit_item(
        db, order, item.id, "p", changed_by=7101
    )
    assert changed is True
    assert float(item.qty) == 1.5
    assert float(order.total_estimated) == 150.0

    changed, _ = await stack.orders.edit_item(db, order, item.id, "m")
    assert changed is True
    assert float(item.qty) == 1.0
    assert float(order.total_estimated) == 100.0

    # Доходим до минимума: 1.0 → 0.5 разрешено, 0.5 → 0 — отказ
    changed, _ = await stack.orders.edit_item(db, order, item.id, "m")
    assert changed is True
    assert float(item.qty) == 0.5

    changed, note = await stack.orders.edit_item(db, order, item.id, "m")
    assert changed is False
    assert "Минимум" in note
    assert float(item.qty) == 0.5
    # Вернуть можно
    changed, _ = await stack.orders.edit_item(db, order, item.id, "p")
    assert changed is True
    assert float(item.qty) == 1.0
    assert float(order.total_estimated) == 100.0

    # Позиция, которой нет в заказе
    changed, note = await stack.orders.edit_item(db, order, 999999, "p")
    assert changed is False
    assert "не найдена" in note

    # Правка запрещена после передачи в сборку
    order.status = OrderStatus.ASSEMBLING.value
    changed, note = await stack.orders.edit_item(db, order, item.id, "p")
    assert changed is False
    assert "нельзя" in note


async def test_edit_item_delete_last_cancels_order(
    db, stack: ServiceStack
) -> None:
    """Удаление единственной позиции отменяет весь заказ."""
    client = await make_user(db, telegram_id=7201)
    shop = await make_shop(db, telegram_id=7211)
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "капуста 2 кг")
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    item = order.sub_orders[0].items[0]

    changed, note = await stack.orders.edit_item(
        db, order, item.id, "d", changed_by=7201
    )
    assert changed is True
    assert order.status == OrderStatus.CANCELLED.value
    assert "отменён" in note


async def test_edit_item_delete_one_of_many(db, stack: ServiceStack) -> None:
    """Удаление одной позиции из нескольких пересчитывает итог."""
    client = await make_user(db, telegram_id=7301)
    shop = await make_shop(db, telegram_id=7311)
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await make_product(db, shop, name="Морковь", price=80, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(
        db, client, "капуста 2 кг, морковь 1 кг"
    )
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    sub = order.sub_orders[0]
    assert len(sub.items) == 2
    by_name = {i.raw_name.lower(): i for i in sub.items}
    cabbage = next(
        i for n, i in by_name.items() if "капуста" in n or "cabbage" in n
    )

    changed, note = await stack.orders.edit_item(db, order, cabbage.id, "d")
    assert changed is True
    assert order.status == OrderStatus.PENDING_CLIENT.value
    # 100 (капуста) + 80 (морковь) - 100 = 80
    assert float(order.total_estimated) == 80.0


# ======================================================================
# 3. §9.2/§9.3: геокод → пин → подтверждение, расхождение адреса
# ======================================================================


async def _register_up_to_address(
    e2e: E2EDriver, user_id: int, address: str
) -> None:
    """Проходит регистрацию до ввода адреса включительно."""
    await e2e.text(user_id, "/start")
    await e2e.cb(user_id, "role:client")
    await e2e.text(user_id, "+79991234567")
    await e2e.text(user_id, address)


async def test_reg_geocode_pin_and_confirm(
    e2e: E2EDriver, monkeypatch
) -> None:
    """§9.2: после геокода бот шлёт пин и ждёт подтверждения точки."""
    db = e2e.db
    fake_point = GeoPoint(lat=45.05, lon=38.98)

    async def fake_geocode(address: str) -> GeocodeResult:
        return GeocodeResult(
            point=fake_point,
            address_text=address,
            components={"city": "Краснодар"},
            provider="fake",
        )

    monkeypatch.setattr(e2e.stack.geo, "geocode_address", fake_geocode)

    await _register_up_to_address(e2e, 3001, "г. Краснодар, ул. Ленина, 15")

    # Пин с координатами геокода + вопрос о подтверждении
    assert (45.05, 38.98) in e2e.sent_pins(3001)
    assert any(
        "Проверьте точку" in t for t in e2e.sent_texts(3001)
    )

    # Подтверждение → профиль сохранён (имя берём из нажавшего кнопку)
    await e2e.cb(3001, "creg:ok")
    assert any(
        "Профиль готов!" in t for t in e2e.sent_texts(3001)
    )
    db.expire_all()
    user = (
        await db.execute(select(User).where(User.telegram_id == 3001))
    ).scalar_one()
    assert user.role == "client"
    assert user.name == "U3001"
    assert user.phone == "+79991234567"
    assert point_from_wkt(user.address_geo) == fake_point


async def test_reg_manual_point_mismatch_confirmation(
    e2e: E2EDriver, monkeypatch
) -> None:
    """§9.3: расхождение текста и ручной точки > 500 м → подтверждение."""
    db = e2e.db
    fake_point = GeoPoint(lat=45.05, lon=38.98)

    async def fake_geocode(address: str) -> GeocodeResult:
        return GeocodeResult(
            point=fake_point, address_text=address, provider="fake"
        )

    monkeypatch.setattr(e2e.stack.geo, "geocode_address", fake_geocode)

    await _register_up_to_address(e2e, 3002, "г. Краснодар, ул. Ленина, 15")
    # Покупатель выбирает ручную точку
    await e2e.cb(3002, "creg:manual")
    assert any(
        "геолокацию" in t.lower() for t in e2e.sent_texts(3002)
    )
    # Точка в 10+ км от текстового адреса → просим подтвердить
    manual = GeoPoint(lat=45.15, lon=39.10)
    await e2e.location(3002, manual.lat, manual.lon)
    assert any(
        "отличается" in t.lower() for t in e2e.sent_texts(3002)
    )
    assert (45.15, 39.10) in e2e.sent_pins(3002)

    # Принимаем → сохраняем точку покупателя
    await e2e.cb(3002, "creg:accept")
    assert any(
        "Профиль готов!" in t for t in e2e.sent_texts(3002)
    )
    user = (
        await db.execute(select(User).where(User.telegram_id == 3002))
    ).scalar_one()
    saved = point_from_wkt(user.address_geo)
    assert abs(saved.lat - manual.lat) < 1e-6
    assert abs(saved.lon - manual.lon) < 1e-6


async def test_reg_reenter_returns_to_address(
    e2e: E2EDriver, monkeypatch
) -> None:
    """§9.3: кнопка «Ввести заново» возвращает к вводу адреса."""
    async def fake_geocode(address: str) -> GeocodeResult:
        return GeocodeResult(
            point=GeoPoint(lat=45.05, lon=38.98),
            address_text=address,
            provider="fake",
        )

    monkeypatch.setattr(e2e.stack.geo, "geocode_address", fake_geocode)

    await _register_up_to_address(e2e, 3003, "г. Краснодар, ул. Ленина, 15")
    await e2e.cb(3003, "creg:reenter")
    assert any(
        "Укажите адрес заново" in t for t in e2e.sent_texts(3003)
    )


# ======================================================================
# 4. C4: карточки товаров рядом
# ======================================================================


async def test_e2e_c4_catalog_cards(e2e: E2EDriver) -> None:
    """«🥕 Карточки товаров»: витрины рядом + фото карточек."""
    db = e2e.db
    await make_user(db, telegram_id=1101)
    shop = await make_shop(db, telegram_id=2101, name="У Пети")
    product = await make_product(db, shop, name="Яблоки", price=90, unit="кг")
    product.photo_file_id = "AgACAgIAAxkB-c4-photo"
    await db.commit()

    await e2e.text(1101, "🥕 Карточки товаров")

    texts = e2e.sent_texts(1101)
    assert any("🏪 У Пети" in t for t in texts)
    assert any("Яблоки" in t and "90" in t for t in texts)
    assert e2e.sent_photos(1101) == ["AgACAgIAAxkB-c4-photo"]


async def test_e2e_c4_no_shops_nearby(e2e: E2EDriver) -> None:
    """Без магазинов рядом — вежливая подсказка."""
    db = e2e.db
    # Пользователь в Саратове, магазин в Краснодаре — дальше 800 м
    await make_user(db, telegram_id=1121, lat=51.53325, lon=46.01937)
    await make_shop(db, telegram_id=2121, lat=45.0443, lon=38.9763)
    await db.commit()

    await e2e.text(1121, "🥕 Карточки товаров")
    assert any(
        "не нашлось" in t for t in e2e.sent_texts(1121)
    )


# ======================================================================
# 5. C7: редактор и замена списком (E2E)
# ======================================================================


async def test_e2e_c7_edit_and_replace(e2e: E2EDriver) -> None:
    """Редактор позиций: ±, готово, замена списком с отменой старого."""
    db = e2e.db
    await make_user(db, telegram_id=1001)
    shop = await make_shop(db, telegram_id=2001)
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await make_product(db, shop, name="Помидоры", price=200, unit="кг")
    await make_product(db, shop, name="Морковь", price=80, unit="кг")
    await db.commit()

    await e2e.text(1001, "📝 Новый заказ")
    await e2e.text(1001, "капуста 4 кг, помидоры 2 кг")

    db.expire_all()
    order = (await db.execute(select(Order))).scalars().one()
    oid = order.id
    sub_id = order.sub_orders[0].id
    await e2e.cb(2001, f"s:y:{sub_id}")

    order = await e2e.get_order(oid)
    assert order.status == OrderStatus.PENDING_CLIENT.value
    assert float(order.total_estimated) == 600.0  # 4×50 + 2×200
    items = list(order.sub_orders[0].items)
    assert len(items) == 2
    cabbage = next(i for i in items if float(i.qty or 0) == 4.0)

    # Открываем редактор
    await e2e.cb(1001, f"oe:{oid}")
    assert any(
        "Кнопки" in t for t in e2e.sent_edits(1001)
    )

    # +0.5 кг
    await e2e.cb(1001, f"ei:{oid}:{cabbage.id}:p")
    order = await e2e.get_order(oid)
    fresh = next(i for i in order.sub_orders[0].items if i.id == cabbage.id)
    assert float(fresh.qty) == 4.5
    assert float(order.total_estimated) == 625.0

    # −0.5 кг
    await e2e.cb(1001, f"ei:{oid}:{cabbage.id}:m")
    order = await e2e.get_order(oid)
    fresh = next(i for i in order.sub_orders[0].items if i.id == cabbage.id)
    assert float(fresh.qty) == 4.0
    assert float(order.total_estimated) == 600.0

    # Готово → сводка для утверждения
    await e2e.cb(1001, f"edone:{oid}")
    assert any(
        "Предварительно" in t for t in e2e.sent_edits(1001)
    )

    # Замена списком: старый отменяется, создаётся новый
    await e2e.cb(1001, f"erpl:{oid}")
    assert any(
        "новый список" in t for t in e2e.sent_edits(1001)
    )
    await e2e.text(1001, "морковь 1 кг")

    old = await e2e.get_order(oid)
    assert old.status == OrderStatus.CANCELLED.value
    db.expire_all()
    orders = list((await db.execute(select(Order))).scalars())
    assert len(orders) == 2
    new_order = next(o for o in orders if o.id != oid)
    assert new_order.status == OrderStatus.PENDING_SELLERS.value
    assert float(new_order.total_estimated) == 80.0


async def test_e2e_c7_delete_all_cancels(e2e: E2EDriver) -> None:
    """Удаление всех позиций в редакторе отменяет заказ."""
    db = e2e.db
    await make_user(db, telegram_id=1011)
    shop = await make_shop(db, telegram_id=2011)
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await db.commit()

    await e2e.text(1011, "📝 Новый заказ")
    await e2e.text(1011, "капуста 2 кг")
    db.expire_all()
    order = (await db.execute(select(Order))).scalars().one()
    oid = order.id
    await e2e.cb(2011, f"s:y:{order.sub_orders[0].id}")
    order = await e2e.get_order(oid)
    item = order.sub_orders[0].items[0]

    await e2e.cb(1011, f"oe:{oid}")
    await e2e.cb(1011, f"ei:{oid}:{item.id}:d")

    order = await e2e.get_order(oid)
    assert order.status == OrderStatus.CANCELLED.value
    assert any(
        "удалены все позиции" in (m["text"] or "")
        for m in e2e.stack.notifier.messages
        if m["telegram_id"] == 1011
    )


# ======================================================================
# 6. A2/A4/A5/A6: админ-команды (E2E)
# ======================================================================


async def test_e2e_admin_commands(e2e: E2EDriver) -> None:
    """/astats, /acancel, /amass, /alogs — полный набор."""
    db = e2e.db
    await make_user(db, telegram_id=9001, role="admin")
    await make_user(db, telegram_id=9101)
    await make_user(db, telegram_id=9102, is_banned=True)
    shop = await make_shop(db, telegram_id=9201, name="У Пети")
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await db.commit()

    order, _ = await e2e.stack.orders.create_from_text(
        db,
        (
            await db.execute(
                select(User).where(User.telegram_id == 9101)
            )
        ).scalar_one(),
        "капуста 2 кг",
    )
    await db.commit()
    oid = order.id

    # --- A2: статистика с топом позиций ---
    await e2e.text(9001, "/astats")
    stats = "\n".join(e2e.sent_texts(9001))
    assert "📊 Статистика" in stats
    assert "Топ позиций" in stats
    assert "Капуста" in stats

    # --- A5: рассылка незабаненным ---
    await e2e.text(9001, "/amass Привет всем")
    reply = "\n".join(e2e.sent_texts(9001))
    assert "Рассылка:" in reply and "доставлено" in reply
    assert any(
        "Привет всем" in m for m in e2e.stack.notifier.texts_for(9101)
    )
    assert e2e.stack.notifier.texts_for(9102) == []

    # --- A4: отмена заказа администратором ---
    await e2e.text(9001, f"/acancel {oid} проверка")
    order = await e2e.get_order(oid)
    assert order.status == OrderStatus.CANCELLED.value
    assert any(
        "отменён администратором" in t for t in e2e.sent_texts(9001)
    )
    assert any(
        "администратором" in m for m in e2e.stack.notifier.texts_for(9101)
    )

    # --- A6: санкции и инциденты ---
    await e2e.text(9001, "/alogs")
    logs = "\n".join(e2e.sent_texts(9001))
    assert "Санкции" in logs
    assert "Инциденты" in logs
    assert f"#{oid}" in logs
    assert "cancelled" in logs

    # --- Панель админа содержит новые команды ---
    await e2e.text(9001, "/admin")
    panel = "\n".join(e2e.sent_texts(9001))
    assert "/acancel" in panel
    assert "/amass" in panel
    assert "/alogs" in panel


async def test_e2e_acancel_unknown_order(e2e: E2EDriver) -> None:
    """/acancel с несуществующим заказом — понятный ответ."""
    db = e2e.db
    await make_user(db, telegram_id=9301, role="admin")
    await db.commit()

    await e2e.text(9301, "/acancel 424242")
    assert any(
        "не найден" in t for t in e2e.sent_texts(9301)
    )


async def test_e2e_amass_requires_text(e2e: E2EDriver) -> None:
    """/amass без текста — подсказка по использованию."""
    db = e2e.db
    await make_user(db, telegram_id=9401, role="admin")
    await db.commit()

    await e2e.text(9401, "/amass")
    assert any(
        "Использование" in t for t in e2e.sent_texts(9401)
    )

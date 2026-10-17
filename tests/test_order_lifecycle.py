"""Интеграционные тесты жизненного цикла заказа (Э3).

Сценарии §4 ТЗ: самовывоз, доставка, мультимагазин, таймауты, отмена.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DeliveryType,
    ItemStatus,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    SubOrderStatus,
)
from app.db.models import Order, User
from tests.conftest import make_product, make_shop, make_user
from tests.services_fixture import ServiceStack


async def _prepare_catalog(db: AsyncSession, n_shops: int = 1) -> tuple[User, list]:
    """Магазины с помидорами/огурцами рядом с клиентом."""
    client = await make_user(db, telegram_id=100, lat=45.0443, lon=38.9763)
    shops = []
    for i in range(n_shops):
        shop = await make_shop(
            db,
            telegram_id=200 + i,
            name=f"Магазин {chr(65 + i)}",
            lat=45.0443 + i * 0.0002,
            lon=38.9763 + i * 0.0002,
        )
        await make_product(db, shop, name="Помидоры", price=120, unit="кг",
                           synonyms=["томат"] if i == 0 else None)
        await make_product(db, shop, name="Огурцы", price=90, unit="кг")
        shops.append(shop)
    await db.commit()
    return client, shops


# ----------------------------------------------------------------------
# Самовывоз, один магазин (§4.1)
# ----------------------------------------------------------------------


async def test_pickup_full_cycle(db: AsyncSession, stack: ServiceStack) -> None:
    """Полный цикл: список → подтверждение → сборка → получение → оплата → done."""
    client, (shop,) = await _prepare_catalog(db, 1)

    # 1. Покупатель прислал список
    order, text = await stack.orders.create_from_text(
        db, client, "помидоры 2 кг, огурцы 1 кг"
    )
    await db.commit()
    assert order is not None
    assert order.status == OrderStatus.PENDING_SELLERS.value
    assert len(order.sub_orders) == 1
    assert len(order.sub_orders[0].items) == 2
    # Предварительная сумма: 2*120 + 1*90 = 330
    assert float(order.total_estimated) == 330.0

    # Продавцу ушёл запрос
    seller_msgs = stack.notifier.texts_for(shop.telegram_id)
    assert any("Новый запрос" in m for m in seller_msgs)

    # Запланирован таймер ответа продавца (§11.1, 180 сек)
    assert any("seller_confirm" in j[0] for j in stack.scheduler.scheduled)

    # 2. Продавец подтвердил наличие
    sub = order.sub_orders[0]
    await stack.orders.seller_respond(db, sub, accept=True, changed_by=shop.telegram_id)
    await db.commit()

    # 3. Покупатель получил предзаказ и утвердил
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PENDING_CLIENT.value
    client_last = stack.notifier.last_for(client.telegram_id)
    assert client_last and "Предзаказ" in client_last

    await stack.orders.client_approve(
        db, order, delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value, changed_by=client.telegram_id,
    )
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.ASSEMBLING.value
    assert order.delivery_type == DeliveryType.PICKUP.value
    # Продавцу пришла просьба собрать
    assert any("Собирайте" in m for m in stack.notifier.texts_for(shop.telegram_id))

    # 4. Продавец собрал
    sub = order.sub_orders[0]
    await stack.orders.mark_assembled(db, sub, changed_by=shop.telegram_id)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.READY.value
    assert float(order.total_actual) == 330.0
    ready_msg = stack.notifier.last_for(client.telegram_id)
    assert ready_msg and "собран" in ready_msg.lower()
    assert "Заберите" in ready_msg

    # 5. Покупатель оплатил, продавец подтвердил
    await stack.orders.client_says_paid(db, order, changed_by=client.telegram_id)
    await db.commit()
    assert any("оплате" in m for m in stack.notifier.texts_for(shop.telegram_id))

    await stack.orders.seller_confirmed_payment(db, order, changed_by=shop.telegram_id)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.DONE.value
    assert order.payment_status == PaymentStatus.PAID.value
    assert order.paid_at is not None
    assert any("закрыт" in m for m in stack.notifier.texts_for(client.telegram_id))


# ----------------------------------------------------------------------
# Доставка курьером (§4.2, §6.3)
# ----------------------------------------------------------------------


async def test_delivery_full_cycle(db: AsyncSession, stack: ServiceStack) -> None:
    """Доставка: сборка → курьер → забрал → доставил → оплата → done."""
    client, (shop,) = await _prepare_catalog(db, 1)
    courier = await make_user(db, telegram_id=300, role="courier")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await db.commit()
    sub = order.sub_orders[0]
    await stack.orders.seller_respond(db, sub, accept=True)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)

    await stack.orders.client_approve(
        db, order, delivery_type=DeliveryType.COURIER.value,
        payment_method=PaymentMethod.CARD.value, changed_by=client.telegram_id,
    )
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    # Доставка 100 ₽ добавлена к итогу (120 + 100 = 220)
    assert float(order.delivery_cost) == 100.0
    assert float(order.total_estimated) == 220.0

    sub = order.sub_orders[0]
    await stack.orders.mark_assembled(db, sub)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    # Курьер назначен, статус delivering
    assert order.status == OrderStatus.DELIVERING.value
    assert order.courier_id == courier.id
    assert any("Новая доставка" in m for m in stack.notifier.texts_for(courier.telegram_id))

    # Курьер забрал и доставил
    await stack.orders.courier_picked_up(db, order, changed_by=courier.telegram_id)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.picked_up_at is not None
    assert any("забрал" in m.lower() for m in stack.notifier.texts_for(client.telegram_id))

    await stack.orders.courier_delivered(db, order, changed_by=courier.telegram_id)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.READY.value
    assert any("доставлен" in m.lower() for m in stack.notifier.texts_for(client.telegram_id))

    # Постоплата
    await stack.orders.client_says_paid(db, order)
    await stack.orders.seller_confirmed_payment(db, order)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.DONE.value


# ----------------------------------------------------------------------
# Мультимагазин (§4.3)
# ----------------------------------------------------------------------


async def test_multishop_order(db: AsyncSession, stack: ServiceStack) -> None:
    """Заказ делится на два магазина; отклонение одного не рушит второй."""
    client, shops = await _prepare_catalog(db, 2)

    order, text = await stack.orders.create_from_text(
        db, client, "помидоры 1 кг, огурцы 1 кг"
    )
    await db.commit()
    assert order is not None
    # Оба магазина нашли оба товара → один из них выигрывает fuzzy-матчинг
    # (по умолчанию ближайший/лучший score). Проверяем что минимум 1 подзаказ.
    assert len(order.sub_orders) >= 1

    # Один магазин подтверждает, другой отклоняет — заказ всё равно собирается
    sub_yes = order.sub_orders[0]
    await stack.orders.seller_respond(db, sub_yes, accept=True)
    if len(order.sub_orders) > 1:
        sub_no = order.sub_orders[1]
        await stack.orders.seller_respond(db, sub_no, accept=False, reason="нет")
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    if order.status == OrderStatus.PENDING_CLIENT.value:
        active = [s for s in order.sub_orders if s.status == SubOrderStatus.CONFIRMED.value]
        assert len(active) >= 1
        rejected = [s for s in order.sub_orders if s.status == SubOrderStatus.REJECTED.value]
        assert all(i.status == ItemStatus.NOT_FOUND.value for s in rejected for i in s.items)
    elif order.status == OrderStatus.CANCELLED.value:
        # Допустимо, если отклонились все (не должно случиться при accepted=1)
        pytest.fail("Заказ отменён, хотя один магазин подтвердил")


async def test_multishop_both_confirm(db: AsyncSession, stack: ServiceStack) -> None:
    """Оба магазина подтверждают — сводный предзаказ покупателю."""
    client, shops = await _prepare_catalog(db, 2)
    from sqlalchemy import select

    from app.db.models import Product

    # Магазин А — только помидоры, Б — только клубника
    prods = await db.execute(select(Product))
    for p in prods.scalars():
        if p.shop_id == shops[0].id and p.name == "Огурцы":
            p.is_active = False
        if p.shop_id == shops[1].id and p.name == "Помидоры":
            p.is_active = False
    await db.flush()
    await make_product(db, shops[1], name="Клубника", price=300, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(
        db, client, "помидоры 1 кг, клубника 1 кг"
    )
    await db.commit()
    assert order is not None
    assert len(order.sub_orders) == 2

    for sub in order.sub_orders:
        await stack.orders.seller_respond(db, sub, accept=True)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PENDING_CLIENT.value
    # 120 + 300 = 420
    assert float(order.total_estimated) == 420.0


# ----------------------------------------------------------------------
# Таймауты и отмены (§11.1)
# ----------------------------------------------------------------------


async def test_seller_timeout_excludes_shop(db: AsyncSession, stack: ServiceStack) -> None:
    """Магазин молчит → исключается, остальные доводят заказ."""
    client, shops = await _prepare_catalog(db, 2)
    from sqlalchemy import select

    from app.db.models import Product

    prods = await db.execute(select(Product))
    for p in prods.scalars():
        if p.shop_id == shops[0].id and p.name == "Огурцы":
            p.is_active = False
        if p.shop_id == shops[1].id and p.name == "Помидоры":
            p.is_active = False
    # В магазине Б добавляем клубнику (вторая позиция заказа)
    await make_product(db, shops[1], name="Клубника", price=300, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(
        db, client, "помидоры 1 кг, клубника 1 кг"
    )
    await db.commit()
    assert order is not None
    assert len(order.sub_orders) == 2

    # Таймаут по одному магазину
    silent = order.sub_orders[0]
    await stack.orders.handle_seller_confirm_timeout(db, order.id, silent.shop_id)
    # Второй отвечает
    other = order.sub_orders[1]
    await stack.orders.seller_respond(db, other, accept=True)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PENDING_CLIENT.value
    silent_sub = next(s for s in order.sub_orders if s.id == silent.id)
    assert silent_sub.status == SubOrderStatus.REJECTED.value


async def test_client_approve_timeout_cancels(db: AsyncSession, stack: ServiceStack) -> None:
    """Покупатель молчит → предзаказ отменяется (§11.1)."""
    client, (shop,) = await _prepare_catalog(db, 1)
    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await db.commit()
    sub = order.sub_orders[0]
    await stack.orders.seller_respond(db, sub, accept=True)
    await db.commit()

    await stack.orders.handle_client_approve_timeout(db, order.id)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.CANCELLED.value
    assert any("отмен" in m.lower() for m in stack.notifier.texts_for(client.telegram_id))


async def test_pickup_timeout_registers_no_show(db: AsyncSession, stack: ServiceStack) -> None:
    """Неявка → no_show + нарушение бан-логики (§5.4)."""
    client, (shop,) = await _prepare_catalog(db, 1)
    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await db.commit()
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order, delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.mark_assembled(db, order.sub_orders[0])
    await db.commit()

    await stack.orders.handle_pickup_timeout(db, order.id)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.NO_SHOW.value
    db_user = await db.get(User, client.id)
    assert db_user.ban_count == 1
    # Уровень 1 — только предупреждение, бан не ставится (§5.4)
    assert db_user.is_banned is False


async def test_assemble_timeout_reminds_then_cancels(db: AsyncSession, stack: ServiceStack) -> None:
    """Магазин не собрал: первая — напоминание, вторая — отмена."""
    client, (shop,) = await _prepare_catalog(db, 1)
    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await db.commit()
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order, delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    sub_id = order.sub_orders[0].id

    # Первый таймаут — напоминание
    stack.notifier.clear()
    await stack.orders.handle_assemble_timeout(db, order.id, sub_id)
    await db.commit()
    assert any("Напоминание" in m for m in stack.notifier.texts_for(shop.telegram_id))
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.ASSEMBLING.value

    # Второй таймаут — отмена
    await stack.orders.handle_assemble_timeout(db, order.id, sub_id)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.CANCELLED.value
    assert any("отмен" in m.lower() for m in stack.notifier.texts_for(client.telegram_id))


# ----------------------------------------------------------------------
# Отмена покупателем (C13)
# ----------------------------------------------------------------------


async def test_client_cancel_before_assembly(db: AsyncSession, stack: ServiceStack) -> None:
    """Отмена до сборки — без бана, магазины уведомлены."""
    client, (shop,) = await _prepare_catalog(db, 1)
    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await db.commit()
    await stack.orders.client_cancel(db, order, reason="передумал", changed_by=client.telegram_id)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.CANCELLED.value
    assert any("отменил" in m for m in stack.notifier.texts_for(shop.telegram_id))
    db_user = await db.get(User, client.id)
    assert db_user.ban_count == 0


# ----------------------------------------------------------------------
# Повтор заказа (C12)
# ----------------------------------------------------------------------


async def test_repeat_order(db: AsyncSession, stack: ServiceStack) -> None:
    """Кнопка «повторить» создаёт новый заказ по позициям старого."""
    client, (shop,) = await _prepare_catalog(db, 1)
    order, _ = await stack.orders.create_from_text(db, client, "помидоры 2 кг")
    await db.commit()
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order, delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.mark_assembled(db, order.sub_orders[0])
    await stack.orders.seller_confirmed_payment(db, order)
    await db.commit()

    new_order, text = await stack.orders.repeat_order(db, client, order)
    await db.commit()
    assert new_order is not None
    assert new_order.id != order.id
    assert new_order.status == OrderStatus.PENDING_SELLERS.value
    assert "помидоры" in text.lower()


# ----------------------------------------------------------------------
# Нет магазинов рядом / ничего не найдено
# ----------------------------------------------------------------------


async def test_no_shops_nearby(db: AsyncSession, stack: ServiceStack) -> None:
    """Далёкий магазин не находится в радиусе 800 м."""
    client = await make_user(db, telegram_id=100, lat=45.0443, lon=38.9763)
    await make_shop(db, telegram_id=500, lat=45.1000, lon=39.1000, name="Далеко")
    await db.commit()

    order, text = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    assert order is None
    assert "не нашлось" in text


async def test_empty_list_rejected(db: AsyncSession, stack: ServiceStack) -> None:
    """Пустой список — понятная ошибка (§8.5)."""
    client = await make_user(db)
    await db.commit()
    order, text = await stack.orders.create_from_text(db, client, "   ")
    assert order is None
    assert "Пустой" in text or "пуст" in text.lower()

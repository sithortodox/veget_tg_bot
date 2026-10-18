"""Э8: продавец S7/S10, отказ от оплаты §5.4, уведомление магазина о готовности."""

from __future__ import annotations

from sqlalchemy import select

from app.constants import OrderStatus, PaymentStatus
from app.db.models import Order, User
from tests.conftest import make_product, make_shop, make_user
from tests.test_e2e import E2EDriver


async def _make_ready_order(e2e: E2EDriver) -> tuple[int, int, int, int]:
    """Создаёт заказ «сейчас» самовывозом/наличными и доводит до READY.

    Возвращает (client_tg, shop_tg, order_id, sub_id).
    """
    db = e2e.db
    client = await make_user(db, telegram_id=1101)
    shop = await make_shop(db, telegram_id=2101, name="У Теста")
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await db.commit()
    client_tg, shop_tg = client.telegram_id, shop.telegram_id

    await e2e.text(client_tg, "📝 Новый заказ")
    await e2e.text(client_tg, "капуста 2 кг")
    db.expire_all()
    order = (await db.execute(select(Order))).scalars().one()
    sub = order.sub_orders[0]

    await e2e.cb(shop_tg, f"s:y:{sub.id}")
    await e2e.cb(client_tg, f"oa:{order.id}")
    await e2e.cb(client_tg, f"od:{order.id}:pickup")
    await e2e.cb(client_tg, f"op:{order.id}:cash")
    await e2e.cb(shop_tg, f"s:a:{sub.id}")
    await e2e.text(shop_tg, "2")

    order = await e2e.get_order(order.id)
    assert order.status == OrderStatus.READY.value
    return client_tg, shop_tg, order.id, sub.id


async def test_e8_seller_card_shows_client_contacts(e2e: E2EDriver) -> None:
    """S10: карточка подзаказа — состав, адрес и телефон покупателя (S10)."""
    client_tg, shop_tg, _order_id, sub_id = await _make_ready_order(e2e)

    await e2e.cb(shop_tg, f"s:open:{sub_id}")
    card = e2e.sent_edits(shop_tg)[-1]
    assert "капуста" in card.lower()
    assert "2 кг" in card
    assert "факт 2" in card  # факт введён при сборке
    assert "+79990001101" in card  # телефон покупателя
    assert "Ленина" in card  # адрес доставки
    assert "самовывоз" in card


async def test_e8_seller_orders_tabs(e2e: E2EDriver) -> None:
    """S7: вкладки «Активные / На дату / История» переключаются."""
    client_tg, shop_tg, _order_id, sub_id = await _make_ready_order(e2e)

    await e2e.text(shop_tg, "📥 Заказы")
    assert any("Активные" in t for t in e2e.sent_texts(shop_tg))

    await e2e.cb(shop_tg, "s:tab:history")
    history = e2e.sent_texts(shop_tg) + e2e.sent_edits(shop_tg)
    assert any("История" in t for t in history)

    await e2e.cb(shop_tg, "s:tab:preorder")
    pre = e2e.sent_texts(shop_tg) + e2e.sent_edits(shop_tg)
    assert any("Предзаказы" in t for t in pre)


async def test_e8_seller_notified_when_ready(e2e: E2EDriver) -> None:
    """S7/S8: после сборки магазин получает «собран» с кнопкой оплаты."""
    _client_tg, shop_tg, _order_id, sub_id = await _make_ready_order(e2e)
    texts = e2e.stack.notifier.texts_for(shop_tg)
    assert any("собран" in m.lower() for m in texts)


async def test_e8_client_refuses_payment(e2e: E2EDriver) -> None:
    """§5.4: отказ от оплаты → статус refused_payment + бан-логика 1-го уровня."""
    client_tg, shop_tg, order_id, _sub_id = await _make_ready_order(e2e)

    await e2e.cb(client_tg, f"orf:{order_id}")

    order = await e2e.get_order(order_id)
    assert order.status == OrderStatus.REFUSED_PAYMENT.value
    assert order.payment_status == PaymentStatus.REFUSED.value

    # Магазин уведомлён
    assert any(
        "отказался оплачивать" in m for m in e2e.stack.notifier.texts_for(shop_tg)
    )
    # Санкция: уровень 1 — предупреждение, бан не применяется
    client_id = order.client_id
    e2e.db.expire_all()
    user = await e2e.db.get(User, client_id)
    assert user is not None
    assert (user.ban_count or 0) == 1
    assert user.is_banned is False
    client_texts = e2e.stack.notifier.texts_for(client_tg)
    assert any("отказ" in m.lower() or "⚠️" in m for m in client_texts)

    # Повторный отказ в READY невозможен — заказ уже закрыт
    await e2e.cb(client_tg, f"orf:{order_id}")
    order = await e2e.get_order(order_id)
    assert order.status == OrderStatus.REFUSED_PAYMENT.value


def test_e8_ready_keyboard_has_refusal_not_cancel() -> None:
    """C13-дефект: на готовом заказе кнопка «Отменить» заменена на отказ (§5.4)."""
    from app.keyboards.client_kb import ready_keyboard

    kb = ready_keyboard(1)
    texts = [b.text for row in kb.inline_keyboard for b in row]
    assert "❌ Не буду оплачивать" in texts
    assert "❌ Отменить заказ" not in texts

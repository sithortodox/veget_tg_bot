"""Тесты системы банов (§5.4) и предзаказов (§7) — Э4."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DeliveryType,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PreorderEvent,
    SubOrderStatus,
)
from app.db.models import BanHistory, Order, PreorderLog, User
from tests.conftest import make_product, make_shop, make_user
from tests.services_fixture import ServiceStack


# ======================================================================
# БАНЫ (§5.4)
# ======================================================================


async def test_ban_level_1_warning(db: AsyncSession, stack: ServiceStack) -> None:
    """1-е нарушение — предупреждение, бан не ставится."""
    user = await make_user(db, telegram_id=10)
    await db.commit()
    level = await stack.ban.register_violation(db, user, reason="не забрал")
    await db.commit()
    assert level == 1
    assert user.ban_count == 1
    assert user.is_banned is False
    # Есть запись в истории
    rows = await db.execute(select(BanHistory).where(BanHistory.user_id == user.id))
    history = list(rows.scalars())
    assert len(history) == 1
    assert history[0].level == 1


async def test_ban_level_2_ten_days(db: AsyncSession, stack: ServiceStack) -> None:
    """2-е нарушение — бан 10 суток."""
    user = await make_user(db, telegram_id=11)
    await db.commit()
    await stack.ban.register_violation(db, user, reason="1")
    level = await stack.ban.register_violation(db, user, reason="2")
    await db.commit()
    assert level == 2
    assert user.is_banned is True
    assert user.banned_until is not None
    delta = user.banned_until - datetime.now(timezone.utc)
    assert timedelta(days=9) < delta <= timedelta(days=10)


async def test_ban_level_3_thirty_days(db: AsyncSession, stack: ServiceStack) -> None:
    """3-е нарушение — бан 30 суток."""
    user = await make_user(db, telegram_id=12)
    await db.commit()
    for _ in range(2):
        await stack.ban.register_violation(db, user, reason="x")
    level = await stack.ban.register_violation(db, user, reason="3")
    await db.commit()
    assert level == 3
    assert user.is_banned is True
    delta = user.banned_until - datetime.now(timezone.utc)
    assert timedelta(days=29) < delta <= timedelta(days=30)


async def test_ban_level_4_forever(db: AsyncSession, stack: ServiceStack) -> None:
    """4-е нарушение — бан навсегда (banned_until=None)."""
    user = await make_user(db, telegram_id=13)
    await db.commit()
    for _ in range(3):
        await stack.ban.register_violation(db, user, reason="x")
    level = await stack.ban.register_violation(db, user, reason="4")
    await db.commit()
    assert level == 4
    assert user.is_banned is True
    assert user.banned_until is None  # бессрочно
    assert stack.ban.is_currently_banned(user) is True


async def test_ban_level_capped_at_4(db: AsyncSession, stack: ServiceStack) -> None:
    """5-е нарушение не повышает уровень выше 4."""
    user = await make_user(db, telegram_id=14)
    await db.commit()
    for _ in range(6):
        await stack.ban.register_violation(db, user, reason="x")
    await db.commit()
    rows = await db.execute(select(BanHistory).where(BanHistory.user_id == user.id))
    levels = {h.level for h in rows.scalars()}
    assert max(levels) == 4


async def test_unban(db: AsyncSession, stack: ServiceStack) -> None:
    """Разбан администратором снимает ограничение."""
    user = await make_user(db, telegram_id=15)
    await db.commit()
    await stack.ban.register_violation(db, user, reason="x")
    await stack.ban.register_violation(db, user, reason="x")
    assert user.is_banned is True
    await stack.ban.unban(db, user, reason="жалоба принята")
    await db.commit()
    assert user.is_banned is False
    assert user.banned_until is None
    assert any("Бан снят" in m for m in stack.notifier.texts_for(user.telegram_id))


async def test_expired_ban_auto_clear(db: AsyncSession, stack: ServiceStack) -> None:
    """Истёкший бан снимается автоматически при проверке."""
    user = await make_user(db, telegram_id=16)
    user.is_banned = True
    user.banned_until = datetime.now(timezone.utc) - timedelta(hours=1)
    await db.commit()
    cleared = await stack.ban.clear_if_expired(db, user)
    await db.commit()
    assert cleared is True
    assert user.is_banned is False


async def test_middleware_style_ban_check(db: AsyncSession, stack: ServiceStack) -> None:
    """Активный бан блокирует, истёкший — нет."""
    user = await make_user(db, telegram_id=17)
    await db.commit()
    await stack.ban.register_violation(db, user, reason="x")
    await stack.ban.register_violation(db, user, reason="x")
    assert stack.ban.is_currently_banned(user) is True
    # Срок истёк: проверка сразу возвращает False
    user.banned_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()
    assert stack.ban.is_currently_banned(user) is False
    # clear_if_expired снимает флаг в БД
    cleared = await stack.ban.clear_if_expired(db, user)
    await db.commit()
    assert cleared is True
    assert user.is_banned is False


async def test_no_show_flow_applies_ban(db: AsyncSession, stack: ServiceStack) -> None:
    """Полный no_show: заказ → сборка → таймаут → бан-логика."""
    client = await make_user(db, telegram_id=100, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=200)
    await make_product(db, shop, name="Помидоры", price=100)
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры 1 кг")
    await stack.orders.seller_respond(db, order.sub_orders[0], accept=True)
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.client_approve(
        db, order, delivery_type=DeliveryType.PICKUP.value,
        payment_method=PaymentMethod.CASH.value,
    )
    order = await stack.orders.get_order(db, order.id)
    await stack.orders.mark_assembled(db, order.sub_orders[0])
    await stack.orders.handle_pickup_timeout(db, order.id)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.NO_SHOW.value
    refreshed = await db.get(User, client.id)
    assert refreshed.ban_count == 1
    # Уровень 1 — предупреждение
    assert refreshed.is_banned is False


# ======================================================================
# ПРЕДЗАКАЗЫ (§7)
# ======================================================================


async def _make_preorder(
    db: AsyncSession, stack: ServiceStack, hours_ahead: float = 48
) -> tuple[User, Order]:
    """Клиент+магазин+предзаказ на N часов вперёд."""
    client = await make_user(db, telegram_id=100, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=200)
    await make_product(db, shop, name="Помидоры", price=120)
    await make_product(db, shop, name="Огурцы", price=90)
    await db.commit()

    when = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    order, text = await stack.orders.create_from_text(
        db, client, "помидоры 5 кг, огурцы 3 кг", scheduled_at=when
    )
    await db.commit()
    assert order is not None
    return client, order


async def test_preorder_created_with_status(db: AsyncSession, stack: ServiceStack) -> None:
    """Предзаказ создаётся со статусом preorder_pending и типом preorder."""
    client, order = await _make_preorder(db, stack)
    assert order.order_type == OrderType.PREORDER.value
    assert order.status == OrderStatus.PREORDER_PENDING.value
    assert order.scheduled_at is not None
    # 5*120 + 3*90 = 870
    assert float(order.total_estimated) == 870.0
    # Продавцу ушло уведомление о предзаказе
    assert any("предзаказ" in m.lower() for m in stack.notifier.texts_for(200))


async def test_preorder_jobs_scheduled(db: AsyncSession, stack: ServiceStack) -> None:
    """Планируются все 4 события: reminder, confirm, search, client (§7.6)."""
    client, order = await _make_preorder(db, stack)
    stack.scheduler.scheduled.clear()
    await stack.preorders.schedule_preorder_jobs(db, order)
    job_ids = [j[0] for j in stack.scheduler.scheduled]
    assert any("reminder" in j for j in job_ids)
    assert any("confirm_deadline" in j for j in job_ids)
    assert any("search_other" in j for j in job_ids)
    assert any("client_deadline" in j for j in job_ids)


async def test_preorder_seller_confirms_immediately(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Магазин подтверждает сразу → preorder_confirmed, клиент уведомлён (§7.5)."""
    client, order = await _make_preorder(db, stack)
    sub = order.sub_orders[0]
    await stack.orders.seller_respond(db, sub, accept=True)
    await stack.preorders.seller_confirmed_sub(db, sub)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PREORDER_CONFIRMED.value
    assert order.seller_confirmed_at is not None
    # Лог preorder_log
    logs = await db.execute(
        select(PreorderLog).where(PreorderLog.order_id == order.id)
    )
    events = {l.event for l in logs.scalars()}
    assert PreorderEvent.SELLER_CONFIRMED.value in events
    # Клиент получил подтверждение
    assert any("подтвердили" in m.lower() for m in stack.notifier.texts_for(client.telegram_id))


async def test_preorder_seller_reject(db: AsyncSession, stack: ServiceStack) -> None:
    """Отказ магазина фиксируется в логе предзаказов."""
    client, order = await _make_preorder(db, stack)
    sub = order.sub_orders[0]
    await stack.orders.seller_respond(db, sub, accept=False, reason="нет")
    await stack.preorders.seller_rejected_sub(db, sub)
    await db.commit()
    logs = await db.execute(
        select(PreorderLog).where(PreorderLog.order_id == order.id)
    )
    events = {l.event for l in logs.scalars()}
    assert PreorderEvent.SELLER_REJECTED.value in events


async def test_preorder_reminder_notification(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Напоминание за 12 ч уходит магазину и клиенту, статус reminded."""
    client, order = await _make_preorder(db, stack, hours_ahead=13)
    stack.notifier.clear()
    await stack.preorders.handle_reminder(db, order.id)
    await db.commit()

    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PREORDER_REMINDED.value
    assert order.reminder_sent_at is not None
    assert any("Напоминание" in m for m in stack.notifier.texts_for(200))
    assert any("предзаказ" in m.lower() for m in stack.notifier.texts_for(client.telegram_id))
    logs = await db.execute(
        select(PreorderLog).where(PreorderLog.order_id == order.id)
    )
    assert PreorderEvent.REMINDER_SENT.value in {l.event for l in logs.scalars()}


async def test_preorder_search_other_marks_status(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Дедлайн подтверждения → search_other_at; handle_search_other ищет замену (§7.2 п.7)."""
    client, order = await _make_preorder(db, stack, hours_ahead=3)
    await stack.preorders.handle_confirm_deadline(db, order.id)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.search_other_at is not None

    await stack.preorders.handle_search_other(db, order.id)
    await db.commit()

    # Старый предзаказ отменён (магазин не подтвердил), логи записаны
    old = await stack.orders.get_order(db, order.id)
    assert old.status == OrderStatus.PREORDER_CANCELLED.value
    logs = await db.execute(
        select(PreorderLog).where(PreorderLog.order_id == order.id)
    )
    events = {l.event for l in logs.scalars()}
    assert PreorderEvent.SEARCH_OTHER.value in events
    assert PreorderEvent.CANCELLED.value in events

    # Создана замена — новый предзаказ на ту же дату
    rows = await db.execute(select(Order).where(Order.id != order.id))
    new_orders = list(rows.scalars())
    assert len(new_orders) == 1
    assert new_orders[0].order_type == OrderType.PREORDER.value
    assert new_orders[0].scheduled_at == order.scheduled_at

    # Клиент уведомлён о замене
    assert any(
        "не подтвердил" in m for m in stack.notifier.texts_for(client.telegram_id)
    )


async def test_preorder_client_deadline_confirmed_ok(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Если все подзаказы confirmed — дедлайн клиента не отменяет заказ."""
    client, order = await _make_preorder(db, stack)
    sub = order.sub_orders[0]
    await stack.orders.seller_respond(db, sub, accept=True)
    await stack.preorders.seller_confirmed_sub(db, sub)
    await db.commit()

    await stack.preorders.handle_client_deadline(db, order.id)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PREORDER_CONFIRMED.value


async def test_preorder_client_deadline_cancels_unconfirmed(
    db: AsyncSession, stack: ServiceStack
) -> None:
    """Магазины не подтвердили → дедлайн клиента отменяет предзаказ."""
    client, order = await _make_preorder(db, stack)
    # Никто не подтвердил
    await stack.preorders.handle_client_deadline(db, order.id)
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PREORDER_CANCELLED.value
    logs = await db.execute(
        select(PreorderLog).where(PreorderLog.order_id == order.id)
    )
    assert PreorderEvent.CANCELLED.value in {l.event for l in logs.scalars()}


async def test_preorder_cancel_by_client(db: AsyncSession, stack: ServiceStack) -> None:
    """Отмена предзаказа покупателем — preorder_cancelled + лог."""
    client, order = await _make_preorder(db, stack)
    await stack.orders.client_cancel(db, order, reason="передумал")
    await db.commit()
    order = await stack.orders.get_order(db, order.id)
    assert order.status == OrderStatus.PREORDER_CANCELLED.value
    logs = await db.execute(
        select(PreorderLog).where(PreorderLog.order_id == order.id)
    )
    assert PreorderEvent.CANCELLED.value in {l.event for l in logs.scalars()}


async def test_preorder_cancel_jobs(db: AsyncSession, stack: ServiceStack) -> None:
    """Отмена предзаказа снимает все запланированные job-таймеры."""
    client, order = await _make_preorder(db, stack)
    await stack.preorders.schedule_preorder_jobs(db, order)
    stack.scheduler.cancelled.clear()
    stack.preorders.cancel_preorder_jobs(order.id)
    assert len(stack.scheduler.cancelled) == 4

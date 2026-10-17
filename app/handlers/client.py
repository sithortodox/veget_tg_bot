"""Хендлеры покупателя: заказ, предзаказ, оплата, история (§3.2, §4)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    DeliveryType,
    OrderStatus,
    PaymentMethod,
    Role,
)
from app.db.models import Order, OrderItem, SubOrder, User
from app.handlers.common import _services
from app.keyboards.client_kb import (
    approve_preorder_keyboard,
    delivery_keyboard,
    history_keyboard,
    payment_keyboard,
    time_keyboard,
)
from app.keyboards.common_kb import main_client_keyboard
from app.services.order import OrderService
from app.services.preorder import PreorderService
from app.services.registry import ServiceRegistry
from app.states.client_states import ClientOrder

logger = logging.getLogger(__name__)
router = Router(name="client")


def _is_client(user: User) -> bool:
    return user.role == Role.CLIENT.value


# ----------------------------------------------------------------------
# Меню
# ----------------------------------------------------------------------


@router.message(F.text == "📝 Новый заказ")
async def menu_new_order(message: Message, state: FSMContext) -> None:
    """Кнопка «Новый заказ»: ждём список текстом."""
    await state.set_state(ClientOrder.waiting_list)
    await message.answer(
        "✍️ Пришлите список продуктов одним сообщением:\n"
        "«капуста 2 кг, помидоры 1 кг, яблоки 2 кило, банан»"
    )


@router.message(F.text == "📦 Мои заказы")
async def menu_orders(message: Message, data: dict) -> None:
    """История заказов + повтор (C12)."""
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    result = await session.execute(
        select(Order)
        .where(Order.client_id == db_user.id)
        .order_by(Order.created_at.desc())
        .limit(10)
    )
    orders = list(result.scalars().all())
    if not orders:
        await message.answer("Пока заказов нет. Пришлите первый список! 🥕")
        return
    lines = []
    pairs: list[tuple[int, str]] = []
    for o in orders:
        status = o.status
        total = float(o.total_actual or o.total_estimated or 0)
        label = f"#{o.id} {status} · {total:.0f} ₽"
        lines.append(f"• {label} — {o.created_at:%d.%m %H:%M}")
        if o.status in (OrderStatus.DONE.value, OrderStatus.CANCELLED.value, OrderStatus.NO_SHOW.value):
            pairs.append((o.id, f"#{o.id} · {total:.0f} ₽"))
    text = "📦 Ваши заказы:\n" + "\n".join(lines)
    if pairs:
        text += "\n\n🔁 Повторить прошлый заказ:"
        await message.answer(text, reply_markup=history_keyboard(pairs))
    else:
        await message.answer(text)


@router.message(F.text == "👤 Профиль")
async def menu_profile(message: Message, data: dict) -> None:
    """Профиль покупателя."""
    db_user: User = data["db_user"]
    await message.answer(
        f"👤 {db_user.name or '—'}\n"
        f"📱 {db_user.phone or '—'}\n"
        f"📍 {db_user.address_text or '—'}\n\n"
        "Для смены адреса: /start → регистрация заново.\n"
        "Радиус поиска магазинов: 300–800 м."
    )


# ----------------------------------------------------------------------
# Приём списка (C2)
# ----------------------------------------------------------------------


@router.message(ClientOrder.waiting_list, F.text)
async def order_list_received(message: Message, state: FSMContext, data: dict) -> None:
    """Свободный текст → парсинг → поиск магазинов → запрос продавцам."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    orders: OrderService = services["orders"]

    if not _is_client(db_user) and db_user.address_geo is None:
        await message.answer("Сначала завершите регистрацию: /start")
        return

    await message.answer("🔍 Разбираю список и ищу магазины...")
    order, text = await orders.create_from_text(session, db_user, message.text)
    if order is None:
        await message.answer(text)
        return

    await state.set_state(ClientOrder.choose_time)
    await state.update_data(order_id=order.id, last_list=message.text)
    # Для обычного заказа сразу спрашиваем «сейчас/на дату»; для предзаказа
    # создание уже с датой — сразу показываем сводку
    if order.order_type == "preorder":
        await message.answer(text)
        return
    await message.answer(
        text + "\n\nКогда нужно?",
        reply_markup=time_keyboard(),
    )


# ----------------------------------------------------------------------
# Сейчас / на дату (C3, C14)
# ----------------------------------------------------------------------


@router.callback_query(F.data == "ot:now")
async def time_now(cb: CallbackQuery, state: FSMContext) -> None:
    """Заказ нужен сейчас — ждём подтверждения продавцов."""
    await cb.answer("Принято")
    await state.set_state(ClientOrder.waiting_list)
    order_id = (await state.get_data()).get("order_id")
    await cb.message.edit_text(
        f"⚡ Заказ #{order_id}: отправил запросы продавцам.\n"
        "Ответят за 3 минуты — пришлю сводный предзаказ."
    )


@router.callback_query(F.data == "ot:date")
async def time_date(cb: CallbackQuery, state: FSMContext) -> None:
    """Переход к вводу даты предзаказа."""
    await cb.answer()
    await state.set_state(ClientOrder.entering_date)
    await cb.message.edit_text(
        "📅 Когда нужно? Пришлите дату и время, например:\n"
        "«18.10 10:00» или «завтра 9:00»"
    )


@router.message(ClientOrder.entering_date, F.text)
async def order_date_entered(message: Message, state: FSMContext, data: dict) -> None:
    """Парсим дату предзаказа и создаём заказ на дату (§7)."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    orders: OrderService = services["orders"]
    preorders: PreorderService = services["preorders"]

    scheduled = _parse_dt(message.text)
    if scheduled is None or scheduled <= datetime.now(timezone.utc):
        await message.answer(
            "Не понял дату. Напишите в формате «18.10.2026 10:00» "
            "(дата должна быть в будущем)."
        )
        return

    # Удаляем черновик обычного заказа, созданный до уточнения даты
    fsm_data = await state.get_data()
    old_id = fsm_data.get("order_id")
    if old_id:
        old = await orders.get_order(session, old_id)
        if old is not None and old.status == OrderStatus.PENDING_SELLERS.value:
            await orders.client_cancel(session, old, reason="переоформление на дату")

    # Текст позиций из старого черновика (если был) уже не берём — просим заново
    # Для плавности: используем последний текст из FSM
    text_list = fsm_data.get("last_list") or message.text
    # Если last_list нет — создаём предзаказ из повторного ввода
    if not fsm_data.get("last_list"):
        await message.answer("Пришлите список продуктов для предзаказа ещё раз:")
        await state.set_state(ClientOrder.waiting_list)
        return

    order, text = await orders.create_from_text(
        session, db_user, text_list, scheduled_at=scheduled
    )
    if order is None:
        await message.answer(text)
        return
    await preorders.schedule_preorder_jobs(session, order)
    await state.clear()
    await message.answer(
        f"✅ Оформлен предзаказ #{order.id} на {scheduled:%d.%m.%Y %H:%M}.\n{text}",
        reply_markup=main_client_keyboard(),
    )


def _parse_dt(raw: str) -> datetime | None:
    """Простой парсер дат: «18.10 10:00», «18.10.2026 10:00», «завтра 9:00»."""
    from datetime import timedelta

    raw = raw.strip().lower()
    now = datetime.now(timezone.utc)
    if raw.startswith("завтра"):
        base = now + timedelta(days=1)
        rest = raw.replace("завтра", "").strip()
        if rest:
            try:
                hh, mm = rest.split(":")
                return base.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            except ValueError:
                return None
        return base.replace(hour=10, minute=0, second=0, microsecond=0)
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M", "%d.%m %H:%M", "%d.%m"):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.year == 1900:
                dt = dt.replace(year=now.year)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------
# Утверждение предзаказа (C8)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("oa:"))
async def order_approve(cb: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Покупатель утвердил предзаказ → выбор доставки."""
    await cb.answer()
    order_id = int(cb.data.split(":")[1])
    await state.set_state(ClientOrder.choosing_delivery)
    await state.update_data(order_id=order_id)
    await cb.message.edit_text(
        f"Заказ #{order_id} принят. Как получить?",
        reply_markup=delivery_keyboard(order_id),
    )


@router.callback_query(F.data.startswith("oc:"))
async def order_cancel(cb: CallbackQuery, data: dict) -> None:
    """Отмена заказа до сборки (C13) — без штрафа."""
    await cb.answer("Отменён")
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    order_id = int(cb.data.split(":")[1])
    order = await orders.get_order(session, order_id)
    if order is not None:
        await orders.client_cancel(session, order, changed_by=cb.from_user.id)
    await cb.message.edit_text(f"❌ Заказ #{order_id} отменён.")


@router.callback_query(F.data.startswith("oe:"))
async def order_edit_hint(cb: CallbackQuery, state: FSMContext) -> None:
    """Подсказка по редактированию (C7): отменить и пересобрать список."""
    await cb.answer()
    await state.set_state(ClientOrder.waiting_list)
    await cb.message.edit_text(
        "✏️ Чтобы изменить состав — пришлите новый список сообщением, "
        "я оформлю заново. Текущий предзаказ можно отменить кнопкой ниже.",
        reply_markup=None,
    )


# ----------------------------------------------------------------------
# Доставка / самовывоз (C9)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("od:"))
async def order_delivery(cb: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Выбор способа получения → выбор оплаты."""
    await cb.answer()
    _, order_id_s, delivery = cb.data.split(":")
    order_id = int(order_id_s)
    await state.update_data(order_id=order_id, delivery_type=delivery)
    await state.set_state(ClientOrder.choosing_payment)
    extra = ""
    if delivery == DeliveryType.COURIER.value:
        settings = data["app_settings"]
        extra = f"\n🛵 Стоимость доставки: {settings.delivery_cost:.0f} ₽"
    await cb.message.edit_text(
        f"Как оплатите при получении?{extra}",
        reply_markup=payment_keyboard(order_id),
    )


@router.callback_query(F.data.startswith("op:"))
async def order_payment(cb: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Финальное утверждение: доставка + постоплата (C8–C10)."""
    await cb.answer("Заказ передан в сборку!")
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    _, order_id_s, payment = cb.data.split(":")
    order_id = int(order_id_s)
    fsm_data = await state.get_data()
    delivery = fsm_data.get("delivery_type", DeliveryType.PICKUP.value)

    order = await orders.get_order(session, order_id)
    if order is None:
        await cb.message.edit_text("Заказ не найден.")
        return
    await orders.client_approve(
        session,
        order,
        delivery_type=delivery,
        payment_method=payment,
        changed_by=cb.from_user.id,
    )
    await state.clear()
    await cb.message.edit_text(
        f"✅ Заказ #{order_id} утверждён! Магазины собирают — сообщу о готовности."
    )


# ----------------------------------------------------------------------
# Заказ готов (C11) + оплата (C10)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("orp:"))
async def order_paid_by_client(cb: CallbackQuery, data: dict) -> None:
    """Покупатель нажал «Оплатил» — уведомляем продавцов."""
    await cb.answer("Передал продавцу")
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    order_id = int(cb.data.split(":")[1])
    order = await orders.get_order(session, order_id)
    if order is not None:
        await orders.client_says_paid(session, order, changed_by=cb.from_user.id)
    await cb.message.edit_text("✅ Спасибо! Продавец подтвердит получение оплаты.")


# ----------------------------------------------------------------------
# Повтор заказа (C12)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("orep:"))
async def order_repeat(cb: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Повтор прошлого заказа одной кнопкой."""
    await cb.answer()
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    orders: OrderService = services["orders"]
    source_id = int(cb.data.split(":")[1])
    source = await orders.get_order(session, source_id)
    if source is None:
        await cb.message.answer("Исходный заказ не найден.")
        return
    await cb.message.answer("🔁 Повторяю заказ...")
    new_order, text = await orders.repeat_order(session, db_user, source)
    if new_order is None:
        await cb.message.answer(text)
        return
    await state.set_state(ClientOrder.choose_time)
    await state.update_data(order_id=new_order.id, last_list=text)
    await cb.message.answer(text + "\n\nКогда нужно?", reply_markup=time_keyboard())


# ----------------------------------------------------------------------
# Служебное: сохранение последнего списка для «на дату»
# ----------------------------------------------------------------------


@router.message(ClientOrder.waiting_list)
async def order_list_fallback(message: Message) -> None:
    """Подсказка, если текст не распознан как список."""
    await message.answer("Пришлите список текстом, например: «капуста 2 кг, помидоры»")

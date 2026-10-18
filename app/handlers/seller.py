"""Хендлеры продавца: заказы, подтверждение, сборка, витрина (§3.3)."""

from __future__ import annotations

import html
import io
import logging
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ContentType, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import VALID_UNITS, OrderStatus, OrderType, SubOrderStatus
from app.db.models import Order, OrderItem, Product, Shop, SubOrder
from app.keyboards.common_kb import main_seller_keyboard
from app.keyboards.seller_kb import (
    orders_list_keyboard,
    product_catalog_keyboard,
    product_edit_fields_keyboard,
    sub_card_keyboard,
)
from app.services.catalog_import import import_catalog_rows, parse_catalog_rows
from app.services.order import OrderService
from app.services.preorder import PreorderService
from app.services.registry import ServiceRegistry
from app.states.seller_states import (
    SellerAssemble,
    SellerProduct,
    SellerProductEdit,
    SellerReject,
)

logger = logging.getLogger(__name__)
router = Router(name="seller")


async def _get_shop(session: AsyncSession, telegram_id: int) -> Optional[Shop]:
    """Магазин текущего продавца."""
    result = await session.execute(
        select(Shop).where(Shop.telegram_id == telegram_id)
    )
    return result.scalar_one_or_none()


# ----------------------------------------------------------------------
# Меню
# ----------------------------------------------------------------------


_ACTIVE_SUB_STATUSES = [
    SubOrderStatus.PENDING.value,
    SubOrderStatus.CONFIRMED.value,
    SubOrderStatus.ASSEMBLING.value,
    SubOrderStatus.READY.value,
    SubOrderStatus.PICKED_UP.value,
]
_HISTORY_SUB_STATUSES = [
    SubOrderStatus.DONE.value,
    SubOrderStatus.REJECTED.value,
    SubOrderStatus.CANCELLED.value,
]
_PREORDER_ORDER_STATUSES = [
    OrderStatus.PREORDER_PENDING.value,
    OrderStatus.PREORDER_REMINDED.value,
    OrderStatus.PREORDER_CONFIRMED.value,
    OrderStatus.PREORDER_SEARCHING.value,
]


async def _orders_list_view(
    session: AsyncSession, shop: Shop, tab: str
) -> tuple[str, object]:
    """Текст + клавиатура для вкладки списка заказов (S7)."""
    if tab == "preorder":
        stmt = (
            select(SubOrder)
            .join(SubOrder.order)
            .where(SubOrder.shop_id == shop.id)
            .where(Order.order_type == OrderType.PREORDER.value)
            .where(Order.status.in_(_PREORDER_ORDER_STATUSES))
            .options(selectinload(SubOrder.order).selectinload(Order.client))
            .order_by(SubOrder.created_at.desc())
        )
        title = "📅 Предзаказы:"
    elif tab == "history":
        stmt = (
            select(SubOrder)
            .where(SubOrder.shop_id == shop.id)
            .where(SubOrder.status.in_(_HISTORY_SUB_STATUSES))
            .options(selectinload(SubOrder.order).selectinload(Order.client))
            .order_by(SubOrder.created_at.desc())
            .limit(10)
        )
        title = "🗂 История (последние 10):"
    else:
        stmt = (
            select(SubOrder)
            .where(SubOrder.shop_id == shop.id)
            .where(SubOrder.status.in_(_ACTIVE_SUB_STATUSES))
            .options(selectinload(SubOrder.order).selectinload(Order.client))
            .order_by(SubOrder.created_at.desc())
        )
        title = "📥 Активные позиции:"
    subs = list((await session.execute(stmt)).scalars())
    lines = [title]
    pairs: list[tuple[int, str]] = []
    for sub in subs:
        client = sub.order.client if sub.order else None
        name = client.name if client else "?"
        lines.append(
            f"• Заказ #{sub.order_id} · {sub.status} · {name} · "
            f"~{float(sub.subtotal_estimated or 0):.0f} ₽"
        )
        pairs.append((sub.id, f"Заказ #{sub.order_id} ({sub.status})"))
    if not pairs:
        lines.append("Пусто.")
    else:
        lines.append("\nОткройте позицию кнопкой ниже.")
    return "\n".join(lines), orders_list_keyboard(pairs, tab=tab)


@router.message(F.text == "📥 Заказы")
async def menu_orders(message: Message, data: dict) -> None:
    """Заказы магазина с вкладками (S7)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        await message.answer("Магазин не найден. /start → «Я магазин».")
        return
    text, kb = await _orders_list_view(session, shop, "active")
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("s:tab:"))
async def seller_orders_tab(cb: CallbackQuery, data: dict) -> None:
    """Переключение вкладки списка заказов (S7)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, cb.from_user.id)
    if shop is None:
        await cb.answer("Магазин не найден", show_alert=True)
        return
    tab = cb.data.split(":")[2]
    text, kb = await _orders_list_view(session, shop, tab)
    await cb.answer()
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:  # noqa: BLE001 — содержимое не изменилось, не роняем UX
        await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("s:open:"))
async def seller_open_sub(cb: CallbackQuery, data: dict) -> None:
    """Карточка подзазаза: состав, факт, контакты покупателя (S10)."""
    session: AsyncSession = data["session"]
    sub_id = int(cb.data.split(":")[2])
    result = await session.execute(
        select(SubOrder)
        .where(SubOrder.id == sub_id)
        .options(
            selectinload(SubOrder.order).selectinload(Order.client),
            selectinload(SubOrder.items).selectinload(OrderItem.product),
        )
    )
    sub = result.scalar_one_or_none()
    if sub is None:
        await cb.answer("Подзаказ не найден", show_alert=True)
        return
    shop = await _get_shop(session, cb.from_user.id)
    if shop is None or sub.shop_id != shop.id:
        await cb.answer("Это не ваш заказ", show_alert=True)
        return
    order = sub.order
    client = order.client if order else None
    lines = [f"📦 Заказ #{sub.order_id} · {sub.status}"]
    if (
        order is not None
        and order.order_type == OrderType.PREORDER.value
        and order.scheduled_at
    ):
        lines.append(f"📅 На дату: {order.scheduled_at:%d.%m.%Y %H:%M} UTC")
    lines.append("🛒 Состав:")
    for item in sub.items:
        plan = f"{float(item.qty or 0):g} {item.unit or ''}".strip()
        fact = (
            f" (факт {float(item.actual_qty):g})"
            if item.actual_qty is not None
            else ""
        )
        lines.append(f"• {item.raw_name} — {plan}{fact}")
    shown_total = float(
        sub.subtotal_actual
        if sub.subtotal_actual is not None
        else sub.subtotal_estimated or 0
    )
    lines.append(f"💰 {'Итого' if sub.subtotal_actual is not None else '~'}: {shown_total:.0f} ₽")
    if order is not None:
        is_pickup = order.delivery_type in ("pickup", None)
        lines.append(f"🚶 Получение: {'самовывоз' if is_pickup else 'доставка курьером'}")
        addr = order.delivery_address or (
            client.address_text if client else None
        )
        if addr:
            lines.append(f"🏠 Адрес: {addr}")
    if client is not None:
        lines.append(f"👤 {client.name or '—'} · 📱 {client.phone or '—'}")
    await cb.answer()
    await cb.message.edit_text(
        "\n".join(lines),
        reply_markup=sub_card_keyboard(sub.id, sub.order_id, sub.status),
    )


@router.message(F.text == "🥕 Моя витрина")
async def menu_catalog(message: Message, data: dict) -> None:
    """Управление витриной (S2–S4): карточки товаров с действиями (S3)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        await message.answer("Магазин не найден. /start → «Я магазин».")
        return
    await _send_catalog(session, shop, message)


async def _send_catalog(
    session: AsyncSession, shop: Shop, target, edit: bool = False
) -> None:
    """Список карточек витрины кнопками (включая скрытые — можно вернуть)."""
    result = await session.execute(
        select(Product)
        .where(Product.shop_id == shop.id)
        .order_by(Product.is_active.desc(), Product.name)
        .limit(20)
    )
    products = list(result.scalars().all())
    lines = [f"🥕 Витрина «{shop.name}» ({len(products)} позиций):"]
    pairs: list[tuple[int, str]] = []
    for p in products:
        mark = "" if p.is_active else " 🙈"
        lines.append(
            f"• {p.name} — {float(p.price):.0f} ₽/{p.unit}{mark}  [id:{p.id}]"
        )
        pairs.append(
            (p.id, f"{p.name} — {float(p.price):.0f} ₽/{p.unit}{mark}")
        )
    lines.append(
        "\nНажмите позицию, чтобы изменить цену/описание/фото, скрыть или удалить.\n"
        "+ добавить: напишите «+товар»\n"
        "Импорт Excel/CSV: файл .xlsx или .csv "
        "(колонки: name,price,unit,description,synonyms)"
    )
    text = "\n".join(lines)[:4000]
    kb = product_catalog_keyboard(pairs)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


def _product_card_text(product: Product, shop: Shop) -> str:
    """Текст карточки товара (S3)."""
    desc = html.escape(product.description) if product.description else "—"
    status = "активен" if product.is_active else "🙈 скрыт"
    photo = " 📷" if product.photo_file_id else ""
    return (
        f"🥕 {html.escape(product.name)}{photo}\n"
        f"💰 {float(product.price):.0f} ₽/{product.unit}\n"
        f"📝 {desc}\n"
        f"Статус: {status}\n\n"
        "Что изменить?"
    )


@router.message(F.text == "👤 Профиль магазина")
async def menu_shop_profile(message: Message, data: dict) -> None:
    """Профиль магазина (S10 — что видит покупатель, здесь — свои данные)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        await message.answer("Магазин не найден. /start → «Я магазин».")
        return
    await message.answer(
        f"🏪 {shop.name}\n"
        f"📍 {shop.address_text or '—'}\n"
        f"📱 {shop.phone or '—'}\n"
        f"💳 {shop.payment_details or '—'}\n"
        f"Статус: {'активен' if shop.is_active else 'неактивен'} · {shop.moderation_status}"
    )


# ----------------------------------------------------------------------
# Ответ на подтверждение наличия (S5–S6)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("s:y:"))
async def seller_accept(cb: CallbackQuery, data: dict) -> None:
    """Продавец: «Есть, собираю» / подтверждение предзаказа."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    preorders: PreorderService = services["preorders"]
    sub_id = int(cb.data.split(":")[2])
    sub = await orders.get_sub_order(session, sub_id)
    if sub is None:
        await cb.answer("Подзаказ не найден", show_alert=True)
        return
    shop = await _get_shop(session, cb.from_user.id)
    if shop is None or sub.shop_id != shop.id:
        await cb.answer("Это не ваш заказ", show_alert=True)
        return

    order = await orders.get_order(session, sub.order_id)
    await orders.seller_respond(
        session, sub, accept=True, changed_by=cb.from_user.id
    )
    if order is not None and order.order_type == "preorder":
        await preorders.seller_confirmed_sub(session, sub)
    await cb.answer("✅ Принято — собирайте, когда подтвердит покупатель")
    await cb.message.edit_text(f"Заказ #{sub.order_id}: подтверждён ✅")


@router.callback_query(F.data.startswith("s:later:"))
async def seller_later(cb: CallbackQuery, data: dict) -> None:
    """Предзаказ: «подтвержу за 12 ч» — просто снимаем тревогу, ждём напоминания."""
    await cb.answer("Хорошо, напомню за 12 часов")
    await cb.message.edit_text("⏳ Ждём напоминания перед датой предзаказа.")


@router.callback_query(F.data.startswith("s:n:"))
async def seller_reject(
    cb: CallbackQuery, state: FSMContext, data: dict
) -> None:
    """Продавец: «Нет в наличии» (или отмена своей части с причиной)."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    preorders: PreorderService = services["preorders"]
    sub_id = int(cb.data.split(":")[2])
    sub = await orders.get_sub_order(session, sub_id)
    if sub is None:
        await cb.answer("Подзаказ не найден", show_alert=True)
        return
    shop = await _get_shop(session, cb.from_user.id)
    if shop is None or sub.shop_id != shop.id:
        await cb.answer("Это не ваш заказ", show_alert=True)
        return

    # Уже идёт сборка — спрашиваем причину отмены своей части (S6)
    if sub.status == SubOrderStatus.ASSEMBLING.value:
        await state.set_state(SellerReject.reason)
        await state.update_data(sub_id=sub.id)
        await cb.answer()
        await cb.message.answer("Причина отмены вашей части заказа (или «-»):")
        return

    order = await orders.get_order(session, sub.order_id)
    await orders.seller_respond(
        session, sub, accept=False, reason="нет в наличии", changed_by=cb.from_user.id
    )
    if order is not None and order.order_type == "preorder":
        await preorders.seller_rejected_sub(session, sub)
    await cb.answer("Отклонено")
    await cb.message.edit_text(f"Заказ #{sub.order_id}: отклонён ❌")


@router.message(SellerReject.reason, F.text)
async def seller_reject_reason(
    message: Message, state: FSMContext, data: dict
) -> None:
    """Приняли причину отмены — отменяем свою часть (S6 с причиной)."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    fsm = await state.get_data()
    sub = await orders.get_sub_order(session, fsm["sub_id"])
    if sub is None:
        await state.clear()
        await message.answer("Подзаказ не найден.")
        return
    shop = await _get_shop(session, message.from_user.id)
    if shop is None or sub.shop_id != shop.id:
        await state.clear()
        await message.answer("Это не ваш заказ.")
        return
    raw = message.text.strip()
    reason = "" if raw == "-" else raw
    ok = await orders.seller_cancels_part(
        session, sub, reason=reason, changed_by=message.from_user.id
    )
    await state.clear()
    if ok:
        text = "🚫 Ваша часть заказа отменена"
        text += f": {reason}" if reason else "."
        text += "\nПокупателя уведомил."
    else:
        text = "Сейчас отменять нельзя — заказ уже в другом статусе."
    await message.answer(text, reply_markup=main_seller_keyboard())


# ----------------------------------------------------------------------
# Сборка (S8) — с фактическим количеством (§5.1)
# ----------------------------------------------------------------------


def _ask_fact_message(idx: int, total: int, item: dict) -> str:
    """Подсказка ввода факта для позиции idx из total."""
    return (
        f"📦 Сборка {idx + 1}/{total}: «{item['name']}» — "
        f"план {item['qty']:g} {item['unit']}.\n"
        "Введите фактическое количество (вес) или «ок», если совпало:"
    )


@router.callback_query(F.data.startswith("s:a:"))
async def seller_assembled(
    cb: CallbackQuery, state: FSMContext, data: dict
) -> None:
    """Отметка «Собрал(а)» → ввод фактического количества по позициям."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    sub_id = int(cb.data.split(":")[2])
    sub = await orders.get_sub_order(session, sub_id)
    if sub is None:
        await cb.answer("Подзаказ не найден", show_alert=True)
        return
    shop = await _get_shop(session, cb.from_user.id)
    if shop is None or sub.shop_id != shop.id:
        await cb.answer("Это не ваш заказ", show_alert=True)
        return
    if sub.status not in (
        SubOrderStatus.ASSEMBLING.value,
        SubOrderStatus.CONFIRMED.value,
    ):
        await cb.answer("Подзаказ уже не в статусе сборки", show_alert=True)
        return

    items = [
        {
            "id": item.id,
            "name": item.raw_name,
            "qty": float(item.qty or 0),
            "unit": item.unit or "",
            "price": float(item.price or 0),
        }
        for item in sub.items
    ]
    if not items:
        # Позиций нет — фиксируем сборку сразу
        await orders.mark_assembled(session, sub, changed_by=cb.from_user.id)
        await cb.answer("✅ Отмечено — ждём покупателя")
        await cb.message.edit_text(f"Заказ #{sub.order_id}: собран ✅")
        return

    await state.set_state(SellerAssemble.entering)
    await state.update_data(sub_id=sub.id, items=items, idx=0, actuals={})
    await cb.answer("Укажите фактическое количество")
    await cb.message.answer(_ask_fact_message(0, len(items), items[0]))


@router.message(SellerAssemble.entering, F.text)
async def seller_assemble_fact(
    message: Message, state: FSMContext, data: dict
) -> None:
    """Приём фактического количества; после последней позиции — сборка."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    fsm = await state.get_data()
    items: list[dict] = fsm["items"]
    idx: int = fsm["idx"]
    actuals: dict[str, list[float]] = fsm.get("actuals") or {}
    item = items[idx]

    raw = message.text.strip().lower().replace(",", ".")
    if raw in ("ок", "оk", "-", "=", "план", "как есть"):
        qty = item["qty"]
    else:
        try:
            qty = float(raw)
        except ValueError:
            await message.answer(
                "Нужно число (например 4 или 1.2) или «ок», если совпало с планом."
            )
            return
        if qty < 0:
            await message.answer("Количество не может быть отрицательным.")
            return
    actuals[str(item["id"])] = [qty, item["price"]]
    idx += 1

    if idx < len(items):
        await state.update_data(idx=idx, actuals=actuals)
        await message.answer(_ask_fact_message(idx, len(items), items[idx]))
        return

    # Все позиции введены → отмечаем сборку с фактами (§5.1)
    sub = await orders.get_sub_order(session, fsm["sub_id"])
    await state.clear()
    if sub is None or sub.status not in (
        SubOrderStatus.ASSEMBLING.value,
        SubOrderStatus.CONFIRMED.value,
    ):
        await message.answer("Подзаказ уже не в статусе сборки.")
        return
    actual_items = {
        int(item_id): (values[0], values[1]) for item_id, values in actuals.items()
    }
    await orders.mark_assembled(
        session, sub, actual_items=actual_items, changed_by=message.from_user.id
    )
    subtotal = sum(q * p for q, p in actual_items.values())
    await message.answer(
        f"✅ Заказ #{sub.order_id} собран. Факт по вашей части: {subtotal:.0f} ₽",
        reply_markup=main_seller_keyboard(),
    )


# ----------------------------------------------------------------------
# Оплата (§5.3)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("s:p:"))
async def seller_payment(cb: CallbackQuery, data: dict) -> None:
    """«Получил оплату» — заказ закрывается."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    _, _, order_id_s, _sub_s = cb.data.split(":")
    order_id = int(order_id_s)
    order = await orders.get_order(session, order_id)
    if order is None:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    shop = await _get_shop(session, cb.from_user.id)
    if shop is None or not any(s.shop_id == shop.id for s in order.sub_orders):
        await cb.answer("Это не ваш заказ", show_alert=True)
        return
    await orders.seller_confirmed_payment(session, order, changed_by=cb.from_user.id)
    await cb.answer("💵 Оплата получена")
    await cb.message.edit_text(f"Заказ #{order_id}: оплачен и закрыт 💵")


@router.callback_query(F.data.startswith("s:g:"))
async def seller_gave(cb: CallbackQuery, data: dict) -> None:
    """«Выдал (самовывоз)» — фиксируем выдачу."""
    services: ServiceRegistry = data["services"]
    session: AsyncSession = data["session"]
    orders: OrderService = services["orders"]
    _, _, order_id_s, sub_id_s = cb.data.split(":")
    order_id = int(order_id_s)
    sub_id = int(sub_id_s)
    order = await orders.get_order(session, order_id)
    sub = await orders.get_sub_order(session, sub_id)
    if order is None or sub is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    await orders.seller_got_cash(session, order, sub, changed_by=cb.from_user.id)
    await cb.answer("Выдано")
    await cb.message.edit_text(f"Заказ #{order_id}: выдан покупателю 🚶")


# ----------------------------------------------------------------------
# Витрина: добавление товара (S2)
# ----------------------------------------------------------------------


@router.message(F.text == "+товар")
async def product_add_start(message: Message, state: FSMContext) -> None:
    """Начало добавления карточки товара."""
    await state.set_state(SellerProduct.name)
    await message.answer("Название товара (например «Помидоры сливовидные»):")


@router.message(SellerProduct.name, F.text)
async def product_name(message: Message, state: FSMContext) -> None:
    """Название."""
    await state.update_data(name=message.text.strip())
    await state.set_state(SellerProduct.price)
    await message.answer("Цена за единицу, числом (например 89):")


@router.message(SellerProduct.price, F.text)
async def product_price(message: Message, state: FSMContext) -> None:
    """Цена."""
    try:
        price = float(message.text.replace(",", ".").strip())
    except ValueError:
        await message.answer("Нужно число, например 89 или 89.5")
        return
    await state.update_data(price=price)
    await state.set_state(SellerProduct.unit)
    await message.answer(
        "Единица измерения: " + ", ".join(VALID_UNITS)
    )


@router.message(SellerProduct.unit, F.text)
async def product_unit(message: Message, state: FSMContext) -> None:
    """Единица измерения."""
    unit = message.text.strip().lower()
    if unit not in VALID_UNITS:
        await message.answer("Доступно: " + ", ".join(VALID_UNITS))
        return
    await state.update_data(unit=unit)
    await state.set_state(SellerProduct.description)
    await message.answer("Краткое описание (или «-»):")


@router.message(SellerProduct.description, F.text)
async def product_description(message: Message, state: FSMContext) -> None:
    """Описание → просим фото товара (S2)."""
    await state.update_data(description=message.text.strip())
    await state.set_state(SellerProduct.photo)
    await message.answer(
        "📷 Пришлите фото товара (можно несколько — возьму главное) "
        "или отправьте «-», без фото:"
    )


async def _save_product(
    message: Message, state: FSMContext, data: dict
) -> None:
    """Создаёт карточку товара из накопленных FSM-данных."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        await state.clear()
        await message.answer("Магазин не найден. /start → «Я магазин».")
        return
    fsm = await state.get_data()
    desc = fsm.get("description") or "-"
    product = Product(
        shop_id=shop.id,
        name=fsm["name"],
        price=fsm["price"],
        unit=fsm["unit"],
        description=None if desc == "-" else desc,
        photo_file_id=fsm.get("photo_file_id"),
        is_active=True,
    )
    session.add(product)
    await session.flush()
    await state.clear()
    photo_note = " 📷" if product.photo_file_id else ""
    await message.answer(
        f"✅ «{product.name}» добавлен: {float(product.price):.0f} ₽/{product.unit}{photo_note}",
        reply_markup=main_seller_keyboard(),
    )


@router.message(SellerProduct.photo, F.photo)
async def product_photo(message: Message, state: FSMContext, data: dict) -> None:
    """Фото карточки → сохраняем file_id последнего (самого крупного)."""
    photo = message.photo[-1]
    await state.update_data(photo_file_id=photo.file_id)
    await _save_product(message, state, data)


@router.message(SellerProduct.photo, F.text == "-")
async def product_photo_skip(message: Message, state: FSMContext, data: dict) -> None:
    """Без фото → сохраняем карточку."""
    await _save_product(message, state, data)


@router.message(SellerProduct.photo)
async def product_photo_hint(message: Message) -> None:
    """Не то сообщение — повторяем просьбу."""
    await message.answer("📷 Отправьте фото или «-», без фото:")


# ----------------------------------------------------------------------
# Скрытие / удаление / редактирование карточки (S3)
# ----------------------------------------------------------------------


async def _owned_product(
    session: AsyncSession, telegram_id: int, product_id: int
) -> Optional[Product]:
    """Карточка товара текущего магазина (или None)."""
    shop = await _get_shop(session, telegram_id)
    if shop is None:
        return None
    product = await session.get(Product, product_id)
    if product is None or product.shop_id != shop.id:
        return None
    return product


@router.callback_query(F.data.startswith("sp:card:"))
async def product_card(cb: CallbackQuery, data: dict) -> None:
    """Карточка товара: поля + действия (S3)."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    product = await _owned_product(session, cb.from_user.id, product_id)
    if product is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    shop = await _get_shop(session, cb.from_user.id)
    await cb.answer()
    await cb.message.edit_text(
        _product_card_text(product, shop),
        reply_markup=product_edit_fields_keyboard(product.id, product.is_active),
    )


@router.callback_query(F.data == "sp:list")
async def product_list_back(cb: CallbackQuery, data: dict) -> None:
    """«← К витрине» из карточки товара."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, cb.from_user.id)
    if shop is None:
        await cb.answer("Магазин не найден", show_alert=True)
        return
    await cb.answer()
    await _send_catalog(session, shop, cb.message, edit=True)


@router.callback_query(F.data.startswith("sp:price:"))
async def product_edit_price_start(cb: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Просим новую цену."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    product = await _owned_product(session, cb.from_user.id, product_id)
    if product is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    await state.set_state(SellerProductEdit.price)
    await state.update_data(product_id=product.id)
    await cb.answer()
    await cb.message.edit_text(
        f"«{product.name}»: текущая цена {float(product.price):.0f} ₽/{product.unit}.\n"
        "Введите новую цену числом (например 89 или 89.5):"
    )


@router.message(SellerProductEdit.price, F.text)
async def product_edit_price_save(message: Message, state: FSMContext, data: dict) -> None:
    """Сохраняем новую цену."""
    session: AsyncSession = data["session"]
    fsm = await state.get_data()
    product = await _owned_product(session, message.from_user.id, fsm["product_id"])
    if product is None:
        await state.clear()
        await message.answer("Карточка не найдена.")
        return
    try:
        price = float(message.text.replace(",", ".").strip())
    except ValueError:
        await message.answer("Нужно число, например 89 или 89.5")
        return
    product.price = price
    await session.flush()
    await state.clear()
    await message.answer(
        f"✅ Цена обновлена: {product.name} — {price:.0f} ₽/{product.unit}",
        reply_markup=product_edit_fields_keyboard(product.id, product.is_active),
    )


@router.callback_query(F.data.startswith("sp:desc:"))
async def product_edit_desc_start(cb: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Просим новое описание."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    product = await _owned_product(session, cb.from_user.id, product_id)
    if product is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    await state.set_state(SellerProductEdit.description)
    await state.update_data(product_id=product.id)
    await cb.answer()
    current = product.description or "—"
    await cb.message.edit_text(
        f"«{product.name}»: описание «{current}».\n"
        "Введите новое описание (или «-», чтобы убрать):"
    )


@router.message(SellerProductEdit.description, F.text)
async def product_edit_desc_save(message: Message, state: FSMContext, data: dict) -> None:
    """Сохраняем описание."""
    session: AsyncSession = data["session"]
    fsm = await state.get_data()
    product = await _owned_product(session, message.from_user.id, fsm["product_id"])
    if product is None:
        await state.clear()
        await message.answer("Карточка не найдена.")
        return
    text = message.text.strip()
    product.description = None if text == "-" else text
    await session.flush()
    await state.clear()
    await message.answer(
        f"✅ Описание обновлено: {product.name}",
        reply_markup=product_edit_fields_keyboard(product.id, product.is_active),
    )


@router.callback_query(F.data.startswith("sp:photo:"))
async def product_edit_photo_start(cb: CallbackQuery, state: FSMContext, data: dict) -> None:
    """Просим новое фото."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    product = await _owned_product(session, cb.from_user.id, product_id)
    if product is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    await state.set_state(SellerProductEdit.photo)
    await state.update_data(product_id=product.id)
    await cb.answer()
    await cb.message.edit_text(
        f"«{product.name}»: пришлите новое фото товара "
        "или отправьте «-», чтобы убрать фото:"
    )


@router.message(SellerProductEdit.photo, F.photo)
async def product_edit_photo_save(message: Message, state: FSMContext, data: dict) -> None:
    """Сохраняем фото."""
    session: AsyncSession = data["session"]
    fsm = await state.get_data()
    product = await _owned_product(session, message.from_user.id, fsm["product_id"])
    if product is None:
        await state.clear()
        await message.answer("Карточка не найдена.")
        return
    product.photo_file_id = message.photo[-1].file_id
    await session.flush()
    await state.clear()
    await message.answer(
        f"✅ Фото обновлено: {product.name} 📷",
        reply_markup=product_edit_fields_keyboard(product.id, product.is_active),
    )


@router.message(SellerProductEdit.photo, F.text == "-")
async def product_edit_photo_clear(message: Message, state: FSMContext, data: dict) -> None:
    """Убираем фото."""
    session: AsyncSession = data["session"]
    fsm = await state.get_data()
    product = await _owned_product(session, message.from_user.id, fsm["product_id"])
    if product is None:
        await state.clear()
        await message.answer("Карточка не найдена.")
        return
    product.photo_file_id = None
    await session.flush()
    await state.clear()
    await message.answer(
        f"✅ Фото убрано: {product.name}",
        reply_markup=product_edit_fields_keyboard(product.id, product.is_active),
    )


@router.message(SellerProductEdit.photo)
async def product_edit_photo_hint(message: Message) -> None:
    """Не то сообщение — повторяем просьбу."""
    await message.answer("📷 Отправьте фото или «-», без фото:")


@router.callback_query(F.data.startswith("sp:show:"))
async def product_show(cb: CallbackQuery, data: dict) -> None:
    """Вернуть товар на витрину."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    product = await _owned_product(session, cb.from_user.id, product_id)
    if product is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    product.is_active = True
    await session.flush()
    await cb.answer("Показан")
    shop = await _get_shop(session, cb.from_user.id)
    await cb.message.edit_text(
        _product_card_text(product, shop),
        reply_markup=product_edit_fields_keyboard(product.id, product.is_active),
    )


@router.callback_query(F.data.startswith("sp:hide:"))
async def product_hide(cb: CallbackQuery, data: dict) -> None:
    """Скрыть товар с витрины."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    product = await _owned_product(session, cb.from_user.id, product_id)
    if product is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    product.is_active = False
    await session.flush()
    await cb.answer("Скрыт")
    shop = await _get_shop(session, cb.from_user.id)
    await cb.message.edit_text(
        _product_card_text(product, shop),
        reply_markup=product_edit_fields_keyboard(product.id, product.is_active),
    )


@router.callback_query(F.data.startswith("sp:del:"))
async def product_delete(cb: CallbackQuery, data: dict) -> None:
    """Удалить карточку."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    product = await _owned_product(session, cb.from_user.id, product_id)
    if product is None:
        await cb.answer("Не найдено", show_alert=True)
        return
    name = product.name
    await session.delete(product)
    await session.flush()
    await cb.answer("Удалён")
    await cb.message.edit_text(f"Карточка «{name}» удалена.")


# ----------------------------------------------------------------------
# Импорт Excel/CSV (S4)
# ----------------------------------------------------------------------


@router.message(
    F.text.lower().in_({"импорт", "+импорт", "прайс", "excel", "загрузить прайс"})
)
async def import_hint(message: Message, data: dict) -> None:
    """Ключевые слова → подсказка по массовому импорту (S4)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        return
    await message.answer(
        "📄 Массовый импорт каталога (S4):\n"
        "Пришлите файл .xlsx или .csv (первая строка — заголовки).\n"
        "Колонки: name (наименование), price (цена), unit (ед), "
        "description (описание), synonyms (синонимы через запятую).\n"
        "Совпадающие по названию товары обновятся, дублей не будет."
    )


@router.message(F.content_type == ContentType.DOCUMENT)
async def catalog_import(message: Message, data: dict) -> None:
    """Массовый импорт каталога из Excel (.xlsx) или CSV (S4)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        return  # не наш магазин — игнорируем (файлы обрабатывают другие хендлеры)
    doc = message.document
    if doc is None:
        return
    filename = (doc.file_name or "").lower()
    if not (filename.endswith(".csv") or filename.endswith(".xlsx")):
        await message.answer(
            "Поддерживаю .xlsx и .csv: колонки name,price,unit,description,synonyms "
            "(заголовки можно и по-русски: наименование, цена, ед, описание)."
        )
        return
    file = await doc.get_file()
    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    try:
        rows = parse_catalog_rows(buffer.getvalue(), doc.file_name or "")
    except ValueError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    if not rows:
        await message.answer("В файле не нашлось ни одной строки с названием.")
        return
    added, updated = await import_catalog_rows(session, shop, rows)
    await message.answer(
        f"✅ Импорт завершён: добавлено {added}, обновлено {updated} позиций.",
        reply_markup=main_seller_keyboard(),
    )

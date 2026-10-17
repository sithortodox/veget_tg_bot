"""Хендлеры продавца: заказы, подтверждение, сборка, витрина (§3.3)."""

from __future__ import annotations

import csv
import io
import logging
from typing import Optional

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ContentType, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import ItemStatus, Role, SubOrderStatus, VALID_UNITS
from app.db.models import Order, Product, Shop, SubOrder, User
from app.handlers.common import _services
from app.keyboards.common_kb import main_seller_keyboard
from app.keyboards.seller_kb import (
    orders_list_keyboard,
    product_actions_keyboard,
    sub_assembled_keyboard,
    sub_confirm_keyboard,
)
from app.services.notifier import Notifier
from app.services.order import OrderService
from app.services.preorder import PreorderService
from app.services.registry import ServiceRegistry
from app.states.seller_states import SellerProduct

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


@router.message(F.text == "📥 Заказы")
async def menu_orders(message: Message, data: dict) -> None:
    """Активные заказы магазина (S7)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        await message.answer("Магазин не найден. /start → «Я магазин».")
        return
    result = await session.execute(
        select(SubOrder)
        .where(SubOrder.shop_id == shop.id)
        .where(
            SubOrder.status.in_(
                [
                    SubOrderStatus.PENDING.value,
                    SubOrderStatus.CONFIRMED.value,
                    SubOrderStatus.ASSEMBLING.value,
                    SubOrderStatus.READY.value,
                ]
            )
        )
        .options(selectinload(SubOrder.order).selectinload(Order.client))
        .order_by(SubOrder.created_at.desc())
    )
    subs = list(result.scalars().all())
    if not subs:
        await message.answer("📭 Активных заказов нет.")
        return
    pairs: list[tuple[int, str]] = []
    lines = ["📥 Активные позиции:"]
    for sub in subs:
        client = sub.order.client if sub.order else None
        name = client.name if client else "?"
        lines.append(
            f"• Заказ #{sub.order_id} · {sub.status} · {name} · "
            f"~{float(sub.subtotal_estimated or 0):.0f} ₽"
        )
        pairs.append((sub.id, f"Заказ #{sub.order_id} ({sub.status})"))
    lines.append("\nОткройте позицию кнопкой ниже.")
    await message.answer("\n".join(lines), reply_markup=orders_list_keyboard(pairs))


@router.message(F.text == "🥕 Моя витрина")
async def menu_catalog(message: Message, data: dict) -> None:
    """Управление витриной (S2–S4)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        await message.answer("Магазин не найден. /start → «Я магазин».")
        return
    result = await session.execute(
        select(Product)
        .where(Product.shop_id == shop.id)
        .where(Product.is_active.is_(True))
        .order_by(Product.name)
        .limit(30)
    )
    products = list(result.scalars().all())
    lines = [f"🥕 Витрина «{shop.name}» ({len(products)} позиций):"]
    for p in products:
        lines.append(f"• {p.name} — {float(p.price):.0f} ₽/{p.unit}  [id:{p.id}]")
    lines.append(
        "\n+ добавить: напишите «+товар»\n"
        "Импорт CSV: отправьте файл (колонки: name,price,unit,description)"
    )
    await message.answer("\n".join(lines)[:4000])


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
async def seller_reject(cb: CallbackQuery, data: dict) -> None:
    """Продавец: «Нет в наличии»."""
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
        session, sub, accept=False, reason="нет в наличии", changed_by=cb.from_user.id
    )
    if order is not None and order.order_type == "preorder":
        await preorders.seller_rejected_sub(session, sub)
    await cb.answer("Отклонено")
    await cb.message.edit_text(f"Заказ #{sub.order_id}: отклонён ❌")


# ----------------------------------------------------------------------
# Сборка (S8)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("s:a:"))
async def seller_assembled(cb: CallbackQuery, data: dict) -> None:
    """Отметка «Собрал(а)»."""
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
    await orders.mark_assembled(session, sub, changed_by=cb.from_user.id)
    await cb.answer("✅ Отмечено — ждём покупателя")
    await cb.message.edit_text(f"Заказ #{sub.order_id}: собран ✅")


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
async def product_description(message: Message, state: FSMContext, data: dict) -> None:
    """Описание → сохраняем карточку."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        await state.clear()
        await message.answer("Магазин не найден. /start → «Я магазин».")
        return
    fsm = await state.get_data()
    desc = message.text.strip()
    product = Product(
        shop_id=shop.id,
        name=fsm["name"],
        price=fsm["price"],
        unit=fsm["unit"],
        description=None if desc == "-" else desc,
        is_active=True,
    )
    session.add(product)
    await session.flush()
    await state.clear()
    await message.answer(
        f"✅ «{product.name}» добавлен: {float(product.price):.0f} ₽/{product.unit}",
        reply_markup=main_seller_keyboard(),
    )


# ----------------------------------------------------------------------
# Скрытие / удаление карточки (S3)
# ----------------------------------------------------------------------


@router.callback_query(F.data.startswith("sp:hide:"))
async def product_hide(cb: CallbackQuery, data: dict) -> None:
    """Скрыть товар с витрины."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    shop = await _get_shop(session, cb.from_user.id)
    product = await session.get(Product, product_id)
    if product is None or (shop and product.shop_id != shop.id):
        await cb.answer("Не найдено", show_alert=True)
        return
    product.is_active = False
    await session.flush()
    await cb.answer("Скрыт")
    await cb.message.edit_text(f"«{product.name}» скрыт с витрины.")


@router.callback_query(F.data.startswith("sp:del:"))
async def product_delete(cb: CallbackQuery, data: dict) -> None:
    """Удалить карточку."""
    session: AsyncSession = data["session"]
    product_id = int(cb.data.split(":")[2])
    shop = await _get_shop(session, cb.from_user.id)
    product = await session.get(Product, product_id)
    if product is None or (shop and product.shop_id != shop.id):
        await cb.answer("Не найдено", show_alert=True)
        return
    await session.delete(product)
    await session.flush()
    await cb.answer("Удалён")
    await cb.message.edit_text("Карточка удалена.")


# ----------------------------------------------------------------------
# Импорт CSV/Excel (S4)
# ----------------------------------------------------------------------


@router.message(F.content_type == ContentType.DOCUMENT)
async def catalog_import(message: Message, data: dict) -> None:
    """Массовый импорт каталога из CSV (name,price,unit,description)."""
    session: AsyncSession = data["session"]
    shop = await _get_shop(session, message.from_user.id)
    if shop is None:
        return  # не наш магазин — игнорируем (файлы обрабатывают другие хендлеры)
    doc = message.document
    if doc is None:
        return
    if not (doc.file_name or "").lower().endswith(".csv"):
        await message.answer(
            "Пока поддерживаю CSV: колонки name,price,unit,description "
            "(первая строка — заголовки). Excel сохраните как CSV."
        )
        return
    file = await doc.get_file()
    buffer = io.BytesIO()
    await message.bot.download_file(file.file_path, buffer)
    raw = buffer.getvalue()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1251")
    reader = csv.DictReader(io.StringIO(text))
    added = 0
    for row in reader:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        try:
            price = float((row.get("price") or "0").replace(",", "."))
        except ValueError:
            continue
        unit = (row.get("unit") or "шт").strip().lower()
        if unit not in VALID_UNITS:
            unit = "шт"
        session.add(
            Product(
                shop_id=shop.id,
                name=name,
                price=price,
                unit=unit,
                description=(row.get("description") or "").strip() or None,
                is_active=True,
            )
        )
        added += 1
    await session.flush()
    await message.answer(f"✅ Импортировано позиций: {added}")

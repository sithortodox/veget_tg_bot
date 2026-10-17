"""Общие хендлеры: /start, выбор роли, регистрация всех ролей (G1–G3, §3)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import ModerationStatus, Role
from app.db.models import Shop, User
from app.keyboards.common_kb import (
    contact_keyboard,
    main_client_keyboard,
    main_courier_keyboard,
    main_seller_keyboard,
    role_keyboard,
)
from app.services.geo import GeoService
from app.services.registry import ServiceRegistry
from app.states.client_states import ClientReg
from app.states.courier_states import CourierReg
from app.states.seller_states import SellerReg

logger = logging.getLogger(__name__)
router = Router(name="common")


def _services(data: dict) -> ServiceRegistry:
    return data["services"]


# ----------------------------------------------------------------------
# /start
# ----------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(
    message: Message, state: FSMContext, db_user: User, data: dict
) -> None:
    """Старт: приветствие + выбор роли, либо возврат в меню роли."""
    await state.clear()
    if db_user.role == Role.CLIENT.value and db_user.address_geo:
        # Уже зарегистрирован как покупатель
        await message.answer(
            "Привет! Я — «ОВОЩНОЙ» 🥕\n"
            "Пришлите список продуктов текстом, например:\n"
            "«капуста 2 кг, помидоры 1 кг, банан»",
            reply_markup=main_client_keyboard(),
        )
        return
    if db_user.role == Role.SELLER.value:
        await message.answer(
            "Меню магазина 👇", reply_markup=main_seller_keyboard()
        )
        return
    if db_user.role == Role.COURIER.value:
        await message.answer("Меню курьера 👇", reply_markup=main_courier_keyboard())
        return
    await message.answer(
        "Привет! Я — «ОВОЩНОЙ» 🥕\n"
        "Локальный маркетплейс продуктов у вашего дома.\n\n"
        "Выберите роль:",
        reply_markup=role_keyboard(),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    """Справка."""
    await message.answer(
        "«ОВОЩНОЙ» — заказ продуктов в 1 сообщение.\n\n"
        "🛒 Покупатель: пришлите список → подтвердите предзаказ → заберите/получите → оплатите при получении.\n"
        "🏪 Магазин: подтверждайте наличие, собирайте, отмечайте оплату.\n"
        "🛵 Курьер: берите доставки, отмечайте забор и доставку.\n\n"
        "/start — главное меню"
    )


# ----------------------------------------------------------------------
# Выбор роли
# ----------------------------------------------------------------------


@router.callback_query(F.data == "role:client")
async def role_client(
    cb: CallbackQuery, state: FSMContext, db_user: User, data: dict
) -> None:
    """Покупатель: доводим регистрацию или показываем меню."""
    await cb.answer()
    if db_user.address_geo and db_user.phone:
        await cb.message.edit_text("Готово к заказам! Пришлите список продуктов 👇")
        await cb.message.answer(
            "Например: «капуста 2 кг, помидоры 1 кг, банан»",
            reply_markup=main_client_keyboard(),
        )
        return
    await cb.message.edit_text("Регистрация покупателя 📱")
    await state.set_state(ClientReg.phone)
    await cb.message.answer(
        "Пришлите номер телефона (или нажмите кнопку):",
        reply_markup=contact_keyboard(),
    )


@router.callback_query(F.data == "role:seller")
async def role_seller(
    cb: CallbackQuery, state: FSMContext, db_user: User, data: dict
) -> None:
    """Продавец: регистрация магазина."""
    await cb.answer()
    services: ServiceRegistry = _services(data)
    session: AsyncSession = data["session"]
    # Уже есть магазин?
    from sqlalchemy import select

    existing = await session.execute(
        select(Shop).where(Shop.telegram_id == cb.from_user.id)
    )
    shop = existing.scalar_one_or_none()
    if shop is not None:
        db_user.role = Role.SELLER.value
        await cb.message.edit_text(f"Магазин «{shop.name}» — меню 👇")
        await cb.message.answer(
            "Управляйте витриной и заказами:", reply_markup=main_seller_keyboard()
        )
        return
    await cb.message.edit_text("Регистрация магазина 🏪")
    await state.set_state(SellerReg.name)
    await cb.message.answer("Введите название магазина:")


@router.callback_query(F.data == "role:courier")
async def role_courier(
    cb: CallbackQuery, state: FSMContext, db_user: User, data: dict
) -> None:
    """Курьер: минимальная регистрация (фаза 2, но ролл-аут уже сейчас)."""
    await cb.answer()
    if db_user.name and db_user.phone:
        db_user.role = Role.COURIER.value
        await cb.message.edit_text("Меню курьера 🛵")
        await cb.message.answer(
            "Здесь появятся ваши доставки:", reply_markup=main_courier_keyboard()
        )
        return
    await cb.message.edit_text("Регистрация курьера 🛵")
    await state.set_state(CourierReg.name)
    await cb.message.answer("Как вас зовут?")


# ----------------------------------------------------------------------
# Регистрация покупателя (C1)
# ----------------------------------------------------------------------


@router.message(ClientReg.phone, F.contact)
async def client_phone_contact(message: Message, state: FSMContext) -> None:
    """Телефон из кнопки «Контакт»."""
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(ClientReg.address)
    await message.answer(
        "📍 Укажите адрес (улица, дом):", reply_markup=None
    )


@router.message(ClientReg.phone, F.text)
async def client_phone_text(message: Message, state: FSMContext) -> None:
    """Телефон текстом."""
    await state.update_data(phone=message.text.strip())
    await state.set_state(ClientReg.address)
    await message.answer("📍 Укажите адрес (улица, дом):")


@router.message(ClientReg.address, F.text)
async def client_address(message: Message, state: FSMContext, data: dict) -> None:
    """Геокодируем текстовый адрес (§9.2)."""
    services: ServiceRegistry = _services(data)
    geo: GeoService = services["geo"]
    address = message.text.strip()
    await state.update_data(address_text=address)

    result = await geo.geocode_address(address)
    if result is None:
        await state.set_state(ClientReg.geo)
        await message.answer(
            "😕 Не удалось определить координаты по адресу.\n"
            "Отправьте геолокацию (скрепка 📎 → Геопозиция)."
        )
        return

    await state.update_data(
        address_geo=result.point.wkt,
        address_lat=result.point.lat,
        address_lon=result.point.lon,
        address_components=result.components,
    )
    await _finish_client_reg(message, state, data)


@router.message(ClientReg.geo, F.location)
async def client_geo_point(message: Message, state: FSMContext, data: dict) -> None:
    """Ручная точка (гео-точка от покупателя)."""
    from app.services.geo import GeoPoint

    lat = message.location.latitude
    lon = message.location.longitude
    point = GeoPoint(lat=lat, lon=lon)
    await state.update_data(
        address_geo=point.wkt, address_lat=lat, address_lon=lon
    )
    await _finish_client_reg(message, state, data)


async def _finish_client_reg(
    message: Message, state: FSMContext, data: dict
) -> None:
    """Сохраняет профиль покупателя и показывает меню."""
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    fsm_data = await state.get_data()

    db_user.role = Role.CLIENT.value
    db_user.phone = fsm_data.get("phone") or db_user.phone
    db_user.address_text = fsm_data.get("address_text") or db_user.address_text
    db_user.address_geo = fsm_data.get("address_geo") or db_user.address_geo
    db_user.address_components = fsm_data.get("address_components")
    if db_user.name is None and message.from_user:
        db_user.name = message.from_user.full_name
    await session.flush()
    await state.clear()
    await message.answer(
        "✅ Профиль готов!\n"
        f"📍 {db_user.address_text or 'адрес по точке на карте'}\n\n"
        "Пришлите список продуктов текстом, например:\n"
        "«капуста 2 кг, помидоры 1 кг, банан»",
        reply_markup=main_client_keyboard(),
    )


# ----------------------------------------------------------------------
# Регистрация продавца (S1)
# ----------------------------------------------------------------------


@router.message(SellerReg.name, F.text)
async def seller_name(message: Message, state: FSMContext) -> None:
    """Название магазина."""
    await state.update_data(name=message.text.strip())
    await state.set_state(SellerReg.address)
    await message.answer("📍 Адрес магазина (улица, дом):")


@router.message(SellerReg.address, F.text)
async def seller_address(message: Message, state: FSMContext, data: dict) -> None:
    """Адрес магазина + геокодирование."""
    services: ServiceRegistry = _services(data)
    geo: GeoService = services["geo"]
    address = message.text.strip()
    await state.update_data(address_text=address)
    result = await geo.geocode_address(address)
    if result is not None:
        await state.update_data(
            address_geo=result.point.wkt,
            address_components=result.components,
        )
    else:
        await state.update_data(address_geo=None)
    await state.set_state(SellerReg.phone)
    await message.answer(
        "📱 Телефон магазина (для связи с покупателями):",
        reply_markup=contact_keyboard(),
    )


@router.message(SellerReg.phone, F.contact)
async def seller_phone_contact(message: Message, state: FSMContext) -> None:
    """Телефон магазина из контакта."""
    await state.update_data(phone=message.contact.phone_number)
    await state.set_state(SellerReg.payment_details)
    await message.answer(
        "💳 Реквизиты для перевода (номер карты/СБП) или напишите «наличные»:"
    )


@router.message(SellerReg.phone, F.text)
async def seller_phone_text(message: Message, state: FSMContext) -> None:
    """Телефон магазина текстом."""
    await state.update_data(phone=message.text.strip())
    await state.set_state(SellerReg.payment_details)
    await message.answer("💳 Реквизиты для перевода или напишите «наличные»:")


@router.message(SellerReg.payment_details, F.text)
async def seller_finish(
    message: Message, state: FSMContext, data: dict
) -> None:
    """Завершение регистрации магазина."""
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    fsm_data = await state.get_data()

    shop = Shop(
        telegram_id=message.from_user.id if message.from_user else None,
        name=fsm_data.get("name", "Магазин"),
        address_text=fsm_data.get("address_text"),
        address_geo=fsm_data.get("address_geo"),
        address_components=fsm_data.get("address_components"),
        phone=fsm_data.get("phone"),
        payment_details=message.text.strip(),
        is_active=True,
        moderation_status=ModerationStatus.APPROVED.value,  # MVP: авто-модерация
    )
    session.add(shop)
    db_user.role = Role.SELLER.value
    if db_user.name is None and message.from_user:
        db_user.name = message.from_user.full_name
    await session.flush()
    await state.clear()
    await message.answer(
        f"✅ Магазин «{shop.name}» зарегистрирован!\n\n"
        "Добавьте товары: «+товар» или кнопка «Моя витрина».\n"
        "Заказы будут приходить сюда автоматически.",
        reply_markup=main_seller_keyboard(),
    )


# ----------------------------------------------------------------------
# Регистрация курьера (K1)
# ----------------------------------------------------------------------


@router.message(CourierReg.name, F.text)
async def courier_name(message: Message, state: FSMContext) -> None:
    """Имя курьера."""
    await state.update_data(name=message.text.strip())
    await state.set_state(CourierReg.phone)
    await message.answer("📱 Телефон:", reply_markup=contact_keyboard())


@router.message(CourierReg.phone, F.contact)
async def courier_phone_contact(message: Message, state: FSMContext, data: dict) -> None:
    """Телефон курьера из контакта."""
    await state.update_data(phone=message.contact.phone_number)
    await _finish_courier_reg(message, state, data)


@router.message(CourierReg.phone, F.text)
async def courier_phone_text(message: Message, state: FSMContext, data: dict) -> None:
    """Телефон курьера текстом."""
    await state.update_data(phone=message.text.strip())
    await _finish_courier_reg(message, state, data)


async def _finish_courier_reg(
    message: Message, state: FSMContext, data: dict
) -> None:
    """Сохраняет курьера."""
    session: AsyncSession = data["session"]
    db_user: User = data["db_user"]
    fsm_data = await state.get_data()
    db_user.role = Role.COURIER.value
    db_user.name = fsm_data.get("name") or db_user.name
    db_user.phone = fsm_data.get("phone") or db_user.phone
    await session.flush()
    await state.clear()
    await message.answer(
        "✅ Вы зарегистрированы как курьер.\n"
        "Доставки будут приходить сюда.",
        reply_markup=main_courier_keyboard(),
    )

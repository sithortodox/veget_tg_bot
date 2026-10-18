"""Общие клавиатуры (роли, меню)."""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)


def role_keyboard() -> InlineKeyboardMarkup:
    """Выбор роли при /start (G1)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Я покупатель", callback_data="role:client")],
            [
                InlineKeyboardButton(
                    text="🏪 Я магазин", callback_data="role:seller"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛵 Я курьер", callback_data="role:courier"
                )
            ],
        ]
    )


def contact_keyboard() -> ReplyKeyboardMarkup:
    """Просьба поделиться контактом (телефон)."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def skip_keyboard() -> ReplyKeyboardMarkup:
    """Кнопка «Пропустить» для необязательных полей."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⏭ Пропустить")]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def main_client_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню покупателя."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📝 Новый заказ")],
            [KeyboardButton(text="📦 Мои заказы")],
            [KeyboardButton(text="🥕 Карточки товаров")],
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🔁 Сменить роль")],
        ],
        resize_keyboard=True,
    )


def creg_confirm_keyboard() -> InlineKeyboardMarkup:
    """§9.2: подтверждение точки после геокода адреса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📍 Точка верна", callback_data="creg:ok"
                ),
                InlineKeyboardButton(
                    text="📍 Указать вручную", callback_data="creg:manual"
                ),
            ]
        ]
    )


def creg_accept_keyboard() -> InlineKeyboardMarkup:
    """§9.3: расхождение текстового адреса и ручной точки."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Принять адрес", callback_data="creg:accept"
                ),
                InlineKeyboardButton(
                    text="✏️ Ввести заново", callback_data="creg:reenter"
                ),
            ]
        ]
    )


def main_seller_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню продавца."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📥 Заказы")],
            [KeyboardButton(text="🥕 Моя витрина")],
            [KeyboardButton(text="👤 Профиль магазина")],
            [KeyboardButton(text="🔁 Сменить роль")],
        ],
        resize_keyboard=True,
    )


def main_courier_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню курьера."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🛵 Мои доставки")],
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🔁 Сменить роль")],
        ],
        resize_keyboard=True,
    )

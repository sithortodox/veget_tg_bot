"""Клавиатуры курьера."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def courier_order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Действия курьера по доставке (K3–K4)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🏪 Забрал у магазина", callback_data=f"k:up:{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Доставил покупателю", callback_data=f"k:dn:{order_id}"
                )
            ],
        ]
    )

"""Клавиатуры продавца (inline-колбэки)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def sub_confirm_keyboard(sub_order_id: int) -> InlineKeyboardMarkup:
    """Подтверждение наличия по подзаказу (S5–S6)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Есть, собираю", callback_data=f"s:y:{sub_order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Нет в наличии", callback_data=f"s:n:{sub_order_id}"
                )
            ],
        ]
    )


def sub_assembled_keyboard(sub_order_id: int) -> InlineKeyboardMarkup:
    """Отметка «заказ собран» (S8)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📦 Собрал(а)", callback_data=f"s:a:{sub_order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚫 Отменить мою часть", callback_data=f"s:n:{sub_order_id}"
                )
            ],
        ]
    )


def payment_confirm_keyboard(order_id: int, sub_order_id: int) -> InlineKeyboardMarkup:
    """Подтверждение получения оплаты (S8, §5.3)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💵 Получил оплату",
                    callback_data=f"s:p:{order_id}:{sub_order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🚶 Выдал (самовывоз)",
                    callback_data=f"s:g:{order_id}:{sub_order_id}",
                )
            ],
        ]
    )


def preorder_seller_keyboard(sub_order_id: int) -> InlineKeyboardMarkup:
    """Действия по новому предзаказу (§7.5)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить сейчас",
                    callback_data=f"s:y:{sub_order_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="⏰ Подтвердить за 12 ч", callback_data=f"s:later:{sub_order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отклонить", callback_data=f"s:n:{sub_order_id}"
                )
            ],
        ]
    )


def preorder_reminder_keyboard(sub_order_id: int) -> InlineKeyboardMarkup:
    """Напоминание о предзаказе (§7.5)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Есть, собираю", callback_data=f"s:y:{sub_order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Нет в наличии", callback_data=f"s:n:{sub_order_id}"
                )
            ],
        ]
    )


def product_actions_keyboard(product_id: int) -> InlineKeyboardMarkup:
    """Действия над карточкой товара (S3)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🙈 Скрыть", callback_data=f"sp:hide:{product_id}"
                ),
                InlineKeyboardButton(
                    text="🗑 Удалить", callback_data=f"sp:del:{product_id}"
                ),
            ]
        ]
    )


def orders_list_keyboard(orders: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Список заказов продавца."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"s:open:{order_id}")]
        for order_id, label in orders
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

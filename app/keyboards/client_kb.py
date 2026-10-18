"""Клавиатуры покупателя (inline-колбэки)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def approve_preorder_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Утверждение предзаказа (C8) + изменить/отмена."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Оформить", callback_data=f"oa:{order_id}"
                ),
                InlineKeyboardButton(
                    text="✏️ Изменить", callback_data=f"oe:{order_id}"
                ),
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отмена", callback_data=f"oc:{order_id}"
                )
            ],
        ]
    )


def delivery_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Выбор самовывоз/доставка (C9)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚶 Самовывоз", callback_data=f"od:{order_id}:pickup"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🛵 Доставка", callback_data=f"od:{order_id}:courier"
                )
            ],
        ]
    )


def payment_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Выбор способа постоплаты (C10)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💵 Наличные", callback_data=f"op:{order_id}:cash"
                ),
                InlineKeyboardButton(
                    text="💳 Перевод на карту", callback_data=f"op:{order_id}:card"
                ),
            ]
        ]
    )


def ready_keyboard(order_id: int) -> InlineKeyboardMarkup:
    """Заказ собран: подтверждение оплаты покупателем + отмена."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Оплатил", callback_data=f"orp:{order_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="❌ Отменить заказ", callback_data=f"oc:{order_id}"
                )
            ],
        ]
    )


def time_keyboard() -> InlineKeyboardMarkup:
    """Нужно сейчас или на дату (C3)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Нужно сейчас", callback_data="ot:now")],
            [InlineKeyboardButton(text="📅 На дату", callback_data="ot:date")],
        ]
    )


def history_keyboard(orders: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    """Список прошлых заказов + повтор (C12)."""
    rows: list[list[InlineKeyboardButton]] = []
    for order_id, label in orders:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔁 {label}", callback_data=f"orep:{order_id}"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_keyboard() -> InlineKeyboardMarkup:
    """Действия с профилем."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📍 Изменить адрес", callback_data="cprof:addr"
                )
            ],
            [
                InlineKeyboardButton(
                    text="🔍 Расширить радиус поиска", callback_data="cprof:radius"
                )
            ],
        ]
    )


def order_edit_keyboard(order) -> InlineKeyboardMarkup:
    """C7: редактор позиций предзаказа (± количество, удаление, замена)."""
    from app.constants import SubOrderStatus

    rows: list[list[InlineKeyboardButton]] = []
    for sub in order.sub_orders:
        if sub.status in (
            SubOrderStatus.REJECTED.value,
            SubOrderStatus.CANCELLED.value,
        ):
            continue
        for item in sub.items:
            name = (item.raw_name or "?")[:18]
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"➕ {name}",
                        callback_data=f"ei:{order.id}:{item.id}:p",
                    ),
                    InlineKeyboardButton(
                        text=f"➖ {name}",
                        callback_data=f"ei:{order.id}:{item.id}:m",
                    ),
                    InlineKeyboardButton(
                        text="🗑", callback_data=f"ei:{order.id}:{item.id}:d"
                    ),
                ]
            )
    rows.append(
        [
            InlineKeyboardButton(
                text="✏️ Заменить списком", callback_data=f"erpl:{order.id}"
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="✅ Готово", callback_data=f"edone:{order.id}"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

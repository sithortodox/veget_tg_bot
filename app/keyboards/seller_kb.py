"""Клавиатуры продавца (inline-колбэки)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.constants import SubOrderStatus


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


def orders_list_keyboard(
    orders: list[tuple[int, str]], tab: str = "active"
) -> InlineKeyboardMarkup:
    """Список заказов продавца + вкладки S7."""
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"s:open:{order_id}")]
        for order_id, label in orders
    ]
    rows.extend(orders_tabs_keyboard(tab).inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def orders_tabs_keyboard(current: str = "active") -> InlineKeyboardMarkup:
    """Вкладки списка заказов (S7): активные / на дату / история."""
    tabs = [
        ("active", "📥 Активные"),
        ("preorder", "📅 На дату"),
        ("history", "🗂 История"),
    ]
    row = [
        InlineKeyboardButton(
            text=("• " + label if key == current else label),
            callback_data=f"s:tab:{key}",
        )
        for key, label in tabs
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def sub_issued_keyboard(order_id: int, sub_order_id: int) -> InlineKeyboardMarkup:
    """Выдача своей части в мультимагазине (без закрытия всего заказа)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚶 Выдал (самовывоз)",
                    callback_data=f"s:g:{order_id}:{sub_order_id}",
                )
            ]
        ]
    )


def sub_card_keyboard(
    sub_order_id: int, order_id: int, status: str
) -> InlineKeyboardMarkup:
    """Действия по карточке подзаказа (S10): по статусу + назад к списку."""
    rows: list[list[InlineKeyboardButton]] = []
    if status == SubOrderStatus.PENDING.value:
        rows.append(
            [
                InlineKeyboardButton(
                    text="✅ Есть, собираю", callback_data=f"s:y:{sub_order_id}"
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="❌ Нет в наличии", callback_data=f"s:n:{sub_order_id}"
                )
            ]
        )
    elif status in (
        SubOrderStatus.CONFIRMED.value,
        SubOrderStatus.ASSEMBLING.value,
    ):
        rows.append(
            [
                InlineKeyboardButton(
                    text="📦 Собрал(а)", callback_data=f"s:a:{sub_order_id}"
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🚫 Отменить мою часть", callback_data=f"s:n:{sub_order_id}"
                )
            ]
        )
    elif status == SubOrderStatus.READY.value:
        rows.append(
            [
                InlineKeyboardButton(
                    text="💵 Получил оплату",
                    callback_data=f"s:p:{order_id}:{sub_order_id}",
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="🚶 Выдал (самовывоз)",
                    callback_data=f"s:g:{order_id}:{sub_order_id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                text="← К списку заказов", callback_data="s:tab:active"
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)

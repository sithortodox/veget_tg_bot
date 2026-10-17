"""Бизнес-константы: статусы, роли, типы оплаты и доставки.

Значения согласованы со §10 (статусы заказа) ТЗ.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    """Роль пользователя в системе."""

    CLIENT = "client"
    SELLER = "seller"
    COURIER = "courier"
    ADMIN = "admin"


class OrderStatus(StrEnum):
    """Статусы заказа (§10)."""

    DRAFT = "draft"  # черновик (идёт парсинг)
    PENDING_SELLERS = "pending_sellers"  # ждём подтверждения магазинов
    PENDING_CLIENT = "pending_client"  # ждём утверждения покупателя
    ASSEMBLING = "assembling"  # собирается
    READY = "ready"  # собран, ждём покупателя/курьера
    DELIVERING = "delivering"  # курьер несёт
    PICKED_UP = "picked_up"  # получен
    PAID = "paid"  # оплачен
    DONE = "done"  # закрыт
    CANCELLED = "cancelled"  # отменён
    NO_SHOW = "no_show"  # не пришёл
    REFUSED_PAYMENT = "refused_payment"  # отказ от оплаты

    # Предзаказ
    PREORDER_PENDING = "preorder_pending"  # предзаказ оформлен, ждём даты
    PREORDER_REMINDED = "preorder_reminded"  # напомнили магазину
    PREORDER_CONFIRMED = "preorder_confirmed"  # магазин подтвердил
    PREORDER_SEARCHING = "preorder_searching"  # ищем другой магазин
    PREORDER_CANCELLED = "preorder_cancelled"  # отменён


class SubOrderStatus(StrEnum):
    """Статусы подзаказа (посылка в конкретном магазине)."""

    PENDING = "pending"  # ждём ответа продавца
    CONFIRMED = "confirmed"  # продавец подтвердил наличие
    REJECTED = "rejected"  # продавец отклонил
    ASSEMBLING = "assembling"  # собирается
    READY = "ready"  # собран
    PICKED_UP = "picked_up"  # выдан/забран
    DONE = "done"
    CANCELLED = "cancelled"


class OrderType(StrEnum):
    """Тип заказа: сейчас или предзаказ на дату."""

    NOW = "now"
    PREORDER = "preorder"


class DeliveryType(StrEnum):
    """Способ получения."""

    PICKUP = "pickup"
    COURIER = "courier"


class PaymentMethod(StrEnum):
    """Способ постоплаты."""

    CASH = "cash"
    CARD = "card"
    SBP = "sbp"  # фаза 2


class PaymentStatus(StrEnum):
    """Статус оплаты."""

    PENDING = "pending"
    PAID = "paid"
    REFUSED = "refused"


class ItemStatus(StrEnum):
    """Судьба позиции в подзаказе."""

    FOUND = "found"
    NOT_FOUND = "not_found"
    ANALOG = "analog"  # предложен аналог


class PreorderEvent(StrEnum):
    """События лога предзаказов (§13, preorder_log.event)."""

    REMINDER_SENT = "reminder_sent"
    SELLER_CONFIRMED = "seller_confirmed"
    SELLER_REJECTED = "seller_rejected"
    SEARCH_OTHER = "search_other"
    CANCELLED = "cancelled"


class ModerationStatus(StrEnum):
    """Статус модерации магазина."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# Уровни банов (§5.4)
BAN_LEVELS: dict[int, dict[str, object]] = {
    1: {"days": 0, "text": "Предупреждение. Это первое нарушение."},
    2: {"days": 10, "text": "Бан на 10 суток."},
    3: {"days": 30, "text": "Бан на 30 суток."},
    4: {"days": -1, "text": "Бан навсегда."},  # -1 = бессрочный
}

# Единицы измерения, поддерживаемые витриной (§13, products.unit)
VALID_UNITS: tuple[str, ...] = ("кг", "шт", "вилок", "пучок", "упаковка", "ящик", "л")

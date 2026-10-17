"""Сервис доставки (§6 ТЗ): проверка зоны, стоимость, назначение курьера.

MVP: курьером может быть пользователь с ролью courier (или админ назначает).
Фаза 2: отдельный бот курьера с маршрутизацией — подключается плагином.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.constants import DeliveryType, Role
from app.db.models import Order, User
from app.services.geo import GeoPoint, haversine_m

logger = logging.getLogger(__name__)


class DeliveryService:
    """Логика пешей доставки."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def is_in_delivery_zone(
        self, client_point: GeoPoint, shop_point: GeoPoint
    ) -> bool:
        """Доставка доступна, если адрес в радиусе доставки (§6.3)."""
        return haversine_m(client_point, shop_point) <= self._settings.delivery_radius_m

    def zone_error_for_order(self, order: Order) -> Optional[str]:
        """Текст ошибки, если адрес клиента вне зоны доставки хотя бы одного магазина.

        None — всё в зоне, доставка возможна.
        """
        from app.services.geo import point_from_wkt

        if not order.delivery_geo:
            return "Не определён адрес доставки — укажите адрес в профиле."
        client_point = point_from_wkt(order.delivery_geo)
        for sub in order.sub_orders:
            shop = sub.shop
            if shop is None or not shop.address_geo:
                continue
            if not self.is_in_delivery_zone(client_point, point_from_wkt(shop.address_geo)):
                return (
                    f"📍 Адрес вне зоны доставки (радиус {self._settings.delivery_radius_m} м): "
                    f"магазин «{shop.name}». Выберите самовывоз или сократите адрес."
                )
        return None

    @property
    def cost(self) -> float:
        """Фиксированная стоимость доставки из конфига."""
        return float(self._settings.delivery_cost)

    async def find_free_courier(self, session: AsyncSession) -> Optional[User]:
        """Ищет активного курьера (MVP: первый попавшийся)."""
        result = await session.execute(
            select(User)
            .where(User.role == Role.COURIER.value)
            .where(User.is_banned.is_(False))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def assign_courier(
        self, session: AsyncSession, order: Order
    ) -> Optional[User]:
        """Назначает курьера на заказ. None — курьеров нет, ответственность на админе/продавце."""
        courier = await self.find_free_courier(session)
        if courier is None:
            logger.info("Свободных курьеров нет для заказа %s", order.id)
            return None
        order.courier_id = courier.id
        await session.flush()
        return courier

    @staticmethod
    def describe_delivery(order: Order) -> str:
        """Человекочитаемое описание доставки для сообщений."""
        if order.delivery_type == DeliveryType.COURIER.value:
            return f"🛵 Доставка курьером — {order.delivery_cost} ₽"
        return "🚶 Самовывоз"

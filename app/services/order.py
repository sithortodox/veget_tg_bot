"""Сервис заказов — ядро бизнес-логики (§4, §10, §11 ТЗ).

Полный цикл: парсинг списка → поиск магазинов → запросы продавцам →
предзаказ покупателю → сборка → получение → постоплата → закрытие.

Расширяемость: дополнительные события оформляются как новые методы сервиса
и подписываются планировщиком/хендлерами; существующие не меняются.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings
from app.constants import (
    DeliveryType,
    ItemStatus,
    OrderStatus,
    OrderType,
    PaymentStatus,
    PreorderEvent,
    SubOrderStatus,
)
from app.db.models import (
    Order,
    OrderItem,
    OrderStatusLog,
    PreorderLog,
    Product,
    SubOrder,
    User,
)
from app.services.ban import BanService
from app.services.geo import GeoPoint, GeoService, haversine_m, point_from_wkt
from app.services.matcher import MatchResult, ProductMatcher
from app.services.notifier import Notifier
from app.services.parser import ProductParser
from app.services.scheduler import (
    SchedulerService,
    job_id_assemble,
    job_id_client_approve,
    job_id_courier_deliver,
    job_id_courier_pickup,
    job_id_pickup,
    job_id_seller_confirm,
)

logger = logging.getLogger(__name__)

# Единицы веса/объёма: счётная форма без единицы («один банан») для них
# неоднозначна — покупателя просим уточнить шт или вес (§8)
_WEIGHT_UNITS = {"кг", "г", "л", "литр", "килограмм"}


def utcnow() -> datetime:
    """Текущее время UTC."""
    return datetime.now(timezone.utc)


class OrderService:
    """Фасад жизненного цикла заказа."""

    def __init__(
        self,
        settings: Settings,
        parser: ProductParser,
        matcher: ProductMatcher,
        geo: GeoService,
        notifier: Notifier,
        ban: BanService,
        scheduler: SchedulerService,
    ) -> None:
        self._settings = settings
        self._parser = parser
        self._matcher = matcher
        self._geo = geo
        self._notifier = notifier
        self._ban = ban
        self._scheduler = scheduler

    # ------------------------------------------------------------------
    # Загрузка сущностей
    # ------------------------------------------------------------------

    async def get_order(self, session: AsyncSession, order_id: int) -> Optional[Order]:
        """Загружает заказ со всеми вложенными данными."""
        stmt = (
            select(Order)
            .where(Order.id == order_id)
            .options(
                selectinload(Order.client),
                selectinload(Order.sub_orders).selectinload(SubOrder.shop),
                selectinload(Order.sub_orders)
                .selectinload(SubOrder.items)
                .selectinload(OrderItem.product),
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_sub_order(
        self, session: AsyncSession, sub_order_id: int
    ) -> Optional[SubOrder]:
        """Загружает подзаказ с магазином, заказом и позициями."""
        stmt = (
            select(SubOrder)
            .where(SubOrder.id == sub_order_id)
            .options(
                selectinload(SubOrder.shop),
                selectinload(SubOrder.items).selectinload(OrderItem.product),
                selectinload(SubOrder.order).selectinload(Order.client),
            )
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_active_client_order(
        self, session: AsyncSession, client_id: int
    ) -> Optional[Order]:
        """Последний «живой» заказ покупателя (для редактирования и статуса)."""
        active_statuses = [
            OrderStatus.DRAFT.value,
            OrderStatus.PENDING_SELLERS.value,
            OrderStatus.PENDING_CLIENT.value,
            OrderStatus.ASSEMBLING.value,
            OrderStatus.READY.value,
            OrderStatus.DELIVERING.value,
            OrderStatus.PREORDER_PENDING.value,
            OrderStatus.PREORDER_REMINDED.value,
            OrderStatus.PREORDER_CONFIRMED.value,
            OrderStatus.PREORDER_SEARCHING.value,
        ]
        stmt = (
            select(Order)
            .where(Order.client_id == client_id)
            .where(Order.status.in_(active_statuses))
            .options(
                selectinload(Order.client),
                selectinload(Order.sub_orders).selectinload(SubOrder.shop),
                selectinload(Order.sub_orders)
                .selectinload(SubOrder.items)
                .selectinload(OrderItem.product),
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Создание заказа из текстового списка
    # ------------------------------------------------------------------

    async def create_from_text(
        self,
        session: AsyncSession,
        client: User,
        text: str,
        scheduled_at: Optional[datetime] = None,
        radius_m: Optional[int] = None,
    ) -> tuple[Optional[Order], str]:
        """Создаёт заказ (или предзаказ) из свободного текста.

        Возвращает (заказ | None, сообщение для покупателя).
        None + сообщение — ошибка, которую нужно показать как есть.
        """
        # 1. Парсинг списка (§8)
        try:
            positions = await self._parser.parse(text)
        except ValueError as exc:
            return None, f"⚠️ {exc}"

        # 2. Точка клиента
        if not client.address_geo:
            return None, "⚠️ Сначала укажите адрес доставки (/start → профиль)."
        client_point = self._point_from_wkt(client.address_geo)

        # 3. Магазины в радиусе (§3.1 G4)
        shops = await self._geo.find_nearby_shops(
            session, client_point, radius_m=radius_m
        )
        if not shops:
            return None, (
                "😔 Рядом не нашлось активных магазинов в радиусе "
                f"{radius_m or self._settings.default_search_radius_m} м.\n"
                "Попробуйте позже или расширьте радиус."
            )

        # 4. Витрина магазинов
        products = await self._load_products(session, [s.id for s in shops])
        shop_by_id = {s.id: s for s in shops}
        catalog = [(p, shop_by_id[p.shop_id]) for p in products]

        # 5. Fuzzy-матчинг позиций (§8.3); при равном сходстве — ближайший (C5)
        distances = {
            s.id: haversine_m(client_point, point_from_wkt(s.address_geo))
            for s in shops
        }
        matches = self._matcher.match(positions, catalog, distances=distances)
        found = [m for m in matches if m.product is not None]
        if not found:
            return None, (
                "😕 Ни одна позиция не найдена на витринах рядом с вами.\n"
                "Проверьте названия или попробуйте другой список."
            )

        # 5.1 Уточнение единиц: посчитали штуками, а товар весовой (§8)
        ambiguous = [
            m
            for m in found
            if m.position.unit is None
            and m.position.qty is not None
            and (m.product.unit or "").lower() in _WEIGHT_UNITS
        ]
        if ambiguous:
            lines = "\n".join(
                f"• {m.position.raw} — {m.position.qty:g}" for m in ambiguous[:5]
            )
            unit0 = ambiguous[0].product.unit or "кг"
            return None, (
                "⚠️ Уточните единицы измерения:\n"
                f"{lines}\n"
                f"Этот товар продаётся в единицах «{unit0}». Напишите точнее, "
                "например «бананы 0.5 кг» или «бананы 3 шт», "
                "и пришлите список ещё раз."
            )

        # 6. Группировка по магазинам → подзаказы
        by_shop: dict[int, list[MatchResult]] = {}
        for m in found:
            assert m.shop is not None
            by_shop.setdefault(m.shop.id, []).append(m)

        # 7. Создание заказа и подзаказов
        is_preorder = scheduled_at is not None
        order = Order(
            client_id=client.id,
            status=(
                OrderStatus.PREORDER_PENDING.value
                if is_preorder
                else OrderStatus.PENDING_SELLERS.value
            ),
            order_type=(
                OrderType.PREORDER.value if is_preorder else OrderType.NOW.value
            ),
            scheduled_at=scheduled_at,
            # client_point уже разобран из address_geo (в т.ч. из WKBElement),
            # в колонку кладём WKT-строку — иначе GeoAlchemy2 потребует shapely
            delivery_geo=client_point.wkt,
            delivery_address=client.address_text,
            payment_status=PaymentStatus.PENDING.value,
        )
        session.add(order)
        # Явная инициализация коллекции — иначе после flush доступ
        # к order.sub_orders вызовет lazy-load (в async это ошибка)
        order.sub_orders = []
        await session.flush()

        # Собираем подзаказы; коллекции items/shop инициализируем руками
        created_subs: list[SubOrder] = []
        for shop_id, shop_matches in by_shop.items():
            sub = SubOrder(order_id=order.id, shop_id=shop_id, status=SubOrderStatus.PENDING.value)
            sub.shop = shop_by_id[shop_id]
            sub.items = []  # инициализация до flush — ключ к async-безопасности
            session.add(sub)
            await session.flush()
            subtotal = 0.0
            for m in shop_matches:
                assert m.product is not None
                qty = m.position.qty if m.position.qty else 1.0
                price = float(m.product.price)
                item = OrderItem(
                    sub_order_id=sub.id,
                    product_id=m.product.id,
                    raw_name=m.position.normalized or m.position.raw,
                    qty=qty,
                    unit=m.position.unit or m.product.unit,
                    price=price,
                    status=ItemStatus.FOUND.value,
                )
                item.product = m.product
                sub.items.append(item)  # append в инициализированную коллекцию
                session.add(item)
                subtotal += qty * price
            sub.subtotal_estimated = subtotal
            created_subs.append(sub)
        await session.flush()

        # Суммы по локальному списку
        order.total_estimated = sum(
            float(s.subtotal_estimated or 0) for s in created_subs
        )
        # Коллекция уже инициализирована — присваивание безопасно в async
        order.sub_orders = created_subs
        await self._log_status(session, order, changed_by=client.telegram_id)
        await session.flush()

        # Неопознанные позиции — предупреждаем
        not_found = [m for m in matches if m.product is None]
        warn = ""
        if not_found:
            names = ", ".join(m.position.raw for m in not_found[:5])
            warn = f"\n⚠️ Не нашли: {names}. Уточните или уберите."

        # 8. Оповещение продавцов (по локальному списку — без lazy-load)
        if is_preorder:
            await self._notify_sellers_preorder(session, order, subs=created_subs)
        else:
            await self._notify_sellers_confirm(session, order, subs=created_subs)

        # 9. Таймауты ответа продавцов (§11.1) — только для заказа «сейчас»
        if not is_preorder:
            for sub in created_subs:
                self._scheduler.schedule_once(
                    job_id_seller_confirm(order.id, sub.shop_id),
                    self._settings.timeout_seller_confirm,
                    self._timeout_runner.on_seller_confirm_timeout,  # type: ignore[attr-defined]
                    order.id,
                    sub.shop_id,
                )

        return order, self._format_preorder(order, subs=created_subs) + warn

    # Таймаут-раннер подставляется извне, чтобы не плодить циклы
    _timeout_runner: object = None

    def set_timeout_runner(self, runner: object) -> None:
        """Привязывает исполнитель таймаутов (вызывается при сборке приложения)."""
        self._timeout_runner = runner

    async def _load_products(
        self, session: AsyncSession, shop_ids: list[int]
    ) -> list[Product]:
        """Активные товары указанных магазинов."""
        if not shop_ids:
            return []
        result = await session.execute(
            select(Product)
            .where(Product.shop_id.in_(shop_ids))
            .where(Product.is_active.is_(True))
        )
        return list(result.scalars().all())

    @staticmethod
    def _point_from_wkt(wkt: str) -> GeoPoint:
        """POINT(lon lat) → GeoPoint."""
        from app.services.geo import point_from_wkt

        return point_from_wkt(wkt)

    async def _recalc_total(self, session: AsyncSession, order: Order) -> float:
        """Пересчёт предварительной суммы по подзаказам (без отклонённых/отменённых)."""
        total = 0.0
        for sub in order.sub_orders:
            if sub.status in (
                SubOrderStatus.REJECTED.value,
                SubOrderStatus.CANCELLED.value,
            ):
                continue
            total += float(sub.subtotal_estimated or 0)
        delivery = (
            float(order.delivery_cost or 0)
            if order.delivery_type == DeliveryType.COURIER.value
            else 0.0
        )
        return total + delivery

    async def _log_status(
        self,
        session: AsyncSession,
        order: Order,
        changed_by: Optional[int] = None,
    ) -> None:
        """Пишет переход статуса в order_status_log."""
        session.add(
            OrderStatusLog(order_id=order.id, status=order.status, changed_by=changed_by)
        )
        await session.flush()

    # ------------------------------------------------------------------
    # Форматирование сообщений
    # ------------------------------------------------------------------

    @staticmethod
    def _format_order_lines(order: Order, subs: Optional[list[SubOrder]] = None) -> str:
        """Список позиций заказа по магазинам.

        `subs` — явный список подзаказов; если не передан, берём order.sub_orders
        (должен быть загружен через selectinload — в async lazy-load запрещён).
        """
        lines: list[str] = []
        idx = 1
        source = subs if subs is not None else list(order.sub_orders)
        for sub in source:
            shop_name = sub.shop.name if sub.shop else "?"
            if sub.status == SubOrderStatus.REJECTED.value:
                continue
            for item in sub.items:
                unit = item.unit or ""
                # float-приведение: edit_item хранит qty как float, price — Decimal
                qty = float(item.qty or 0)
                price = float(item.price or 0)
                lines.append(
                    f"{idx}. {item.raw_name} — {qty:g} {unit} — ~{qty * price:.0f} ₽"
                    f" ({shop_name})"
                )
                idx += 1
        return "\n".join(lines)

    def _format_preorder(
        self, order: Order, subs: Optional[list[SubOrder]] = None
    ) -> str:
        """Сводная таблица предзаказа для покупателя (§7.4)."""
        when = ""
        if order.scheduled_at:
            when = f" на {order.scheduled_at:%d.%m.%Y, %H:%M}"
        total = float(order.total_estimated or 0)
        lines = self._format_order_lines(order, subs=subs)
        text = f"📋 Предзаказ{when}:\n{lines}\n💰 Предварительно: {total:.0f} ₽"
        if order.order_type == OrderType.PREORDER.value:
            text += (
                "\n⚠️ Магазин подтвердит наличие за "
                f"{self._settings.preorder_reminder_hours} часов до даты."
            )
        return text

    def preorder_view(self, order: Order) -> str:
        """Сводка предзаказа для показа покупателю (C6, C7)."""
        rejected = [
            s.shop.name
            for s in order.sub_orders
            if s.status == SubOrderStatus.REJECTED.value and s.shop
        ]
        warn = ("\n⚠️ Отклонены: " + ", ".join(rejected)) if rejected else ""
        return self._format_preorder(order) + warn

    # ------------------------------------------------------------------
    # Редактирование предзаказа (C7)
    # ------------------------------------------------------------------

    async def edit_item(
        self,
        session: AsyncSession,
        order: Order,
        item_id: int,
        action: str,
        changed_by: Optional[int] = None,
    ) -> tuple[bool, str]:
        """Правка позиции предзаказа: p=плюс, m=минус, d=удаление.

        Пересчитывает подытог подзаказа и итог заказа. Если удалены все
        активные позиции — заказ отменяется. Возвращает (изменено, сообщение).
        """
        if order.status != OrderStatus.PENDING_CLIENT.value:
            return False, "⚠️ Заказ уже передан в сборку — править нельзя."
        target: Optional[OrderItem] = None
        parent: Optional[SubOrder] = None
        for sub in order.sub_orders:
            if sub.status in (
                SubOrderStatus.REJECTED.value,
                SubOrderStatus.CANCELLED.value,
            ):
                continue
            for item in sub.items:
                if item.id == item_id:
                    target, parent = item, sub
                    break
            if target is not None:
                break
        if target is None or parent is None:
            return False, "⚠️ Позиция не найдена."

        unit = (target.unit or "").lower()
        step = 1.0 if unit in ("шт", "штук", "упак", "пучок", "вилок") else 0.5
        qty = float(target.qty or 0)
        name = target.raw_name
        if action == "p":
            target.qty = round(qty + step, 2)
        elif action == "m":
            new_qty = round(qty - step, 2)
            if new_qty <= 0:
                return False, "⚠️ Минимум — удалите позицию кнопкой 🗑."
            target.qty = new_qty
        elif action == "d":
            parent.items.remove(target)  # delete-orphan → каскадное удаление
        else:
            return False, "⚠️ Неизвестное действие."

        # Пересчёт подытога подзаказа и итога заказа
        parent.subtotal_estimated = sum(
            float(i.qty or 0) * float(i.price or 0) for i in parent.items
        )
        if not parent.items:
            parent.status = SubOrderStatus.CANCELLED.value
            parent.subtotal_estimated = 0
        order.total_estimated = await self._recalc_total(session, order)
        await session.flush()

        active_items = [
            i
            for s in order.sub_orders
            if s.status
            not in (SubOrderStatus.REJECTED.value, SubOrderStatus.CANCELLED.value)
            for i in s.items
        ]
        if not active_items:
            await self.client_cancel(
                session, order, reason="удалены все позиции", changed_by=changed_by
            )
            return True, "❌ Все позиции удалены — заказ отменён."

        if action == "d":
            note = f"🗑 {name} удалено"
        else:
            note = f"{name}: {target.qty:g} {target.unit or ''}".rstrip()
        return True, note


    # ------------------------------------------------------------------
    # Оповещения продавцов
    # ------------------------------------------------------------------

    async def _notify_sellers_confirm(
        self, session: AsyncSession, order: Order, subs: Optional[list[SubOrder]] = None
    ) -> None:
        """Просьба подтвердить наличие (обычный заказ, §3.3 S5)."""
        from app.keyboards.seller_kb import sub_confirm_keyboard

        for sub in subs if subs is not None else []:
            if not sub.shop:
                continue
            lines = [f"📩 Новый запрос от покупателя (заказ #{order.id}):"]
            for item in sub.items:
                unit = item.unit or ""
                lines.append(f"• {item.raw_name} — {item.qty or 0:g} {unit}")
            lines.append(f"💰 ~{sub.subtotal_estimated or 0:.0f} ₽")
            lines.append("Подтвердите наличие за "
                         f"{self._settings.timeout_seller_confirm // 60} мин.")
            await self._notifier.notify_shop(
                session,
                sub.shop,
                "\n".join(lines),
                keyboard=sub_confirm_keyboard(sub.id),
            )

    async def _notify_sellers_preorder(
        self, session: AsyncSession, order: Order, subs: Optional[list[SubOrder]] = None
    ) -> None:
        """Уведомление магазину о предзаказе (§7.5)."""
        from app.keyboards.seller_kb import preorder_seller_keyboard

        for sub in subs if subs is not None else []:
            if not sub.shop:
                continue
            lines = [
                f"📅 Новый предзаказ на {order.scheduled_at:%d.%m.%Y, %H:%M}"
                if order.scheduled_at
                else "📅 Новый предзаказ",
            ]
            for item in sub.items:
                unit = item.unit or ""
                lines.append(f"• {item.raw_name} — {item.qty or 0:g} {unit}")
            lines.append(f"💰 Предварительно: {sub.subtotal_estimated or 0:.0f} ₽")
            await self._notifier.notify_shop(
                session,
                sub.shop,
                "\n".join(lines),
                keyboard=preorder_seller_keyboard(sub.id),
            )

    # ------------------------------------------------------------------
    # Ответ продавца (подтвердил / отклонил)
    # ------------------------------------------------------------------

    async def seller_respond(
        self,
        session: AsyncSession,
        sub_order: SubOrder,
        accept: bool,
        reason: str = "",
        changed_by: Optional[int] = None,
    ) -> None:
        """Продавец подтверждает или отклоняет свой подзаказ (S6)."""
        order = await self.get_order(session, sub_order.order_id)
        if order is None or order.status not in (
            OrderStatus.PENDING_SELLERS.value,
            OrderStatus.PREORDER_PENDING.value,
            OrderStatus.PREORDER_SEARCHING.value,
        ):
            return

        # Отменяем таймер ответа этого магазина
        self._scheduler.cancel(job_id_seller_confirm(order.id, sub_order.shop_id))

        if accept:
            sub_order.status = SubOrderStatus.CONFIRMED.value
        else:
            sub_order.status = SubOrderStatus.REJECTED.value
            for item in sub_order.items:
                item.status = ItemStatus.NOT_FOUND.value
                if reason:
                    item.note = reason
        await session.flush()

        if order.order_type == OrderType.PREORDER.value:

            # Логирование события предзаказа делает PreorderService;
            # здесь фиксируем факт ответа
            session.add(
                PreorderLog(
                    order_id=order.id,
                    event=(
                        PreorderEvent.SELLER_CONFIRMED.value
                        if accept
                        else PreorderEvent.SELLER_REJECTED.value
                    ),
                    shop_id=sub_order.shop_id,
                )
            )
            await session.flush()
            # Для предзаказа ответ продавца сразу подтверждает состав
            await self._try_finalize_sellers(session, order)
            return

        await self._try_finalize_sellers(session, order)

    async def _try_finalize_sellers(self, session: AsyncSession, order: Order) -> None:
        """Когда все магазины ответили — собираем предзаказ для покупателя.

        Финализация приходит из разных сессий (таймауты планировщика,
        ответы продавцов в чате), поэтому:
        - сериализуемся advisory-блокировкой по заказу — последний вошедший
          видит закоммиченные статусы остальных;
        - статусы читаем свежим SELECT, а не из кэша сессии — иначе два
          параллельных job'а видят друг друга «pending», оба пропускают
          финализацию и заказ навсегда зависает в pending_sellers.

        Advisory-лок снимается на коммите транзакции (`_xact_lock`) и не
        конфликтует с row-локами подзаказов — дедлок невозможен.
        """
        await session.execute(
            sa_text("SELECT pg_advisory_xact_lock(1001, :order_id)"),
            {"order_id": order.id},
        )
        rows = (
            await session.execute(
                select(
                    SubOrder.id,
                    SubOrder.status,
                    SubOrder.subtotal_estimated,
                )
                .where(SubOrder.order_id == order.id)
                .order_by(SubOrder.id)
            )
        ).all()
        fresh = {row.id: (row.status, row.subtotal_estimated) for row in rows}
        for sub in order.sub_orders:
            if sub.id in fresh:
                sub.status, sub.subtotal_estimated = fresh[sub.id]
        statuses = {status for status, _ in fresh.values()}
        unresolved = {
            SubOrderStatus.PENDING.value,
            SubOrderStatus.ASSEMBLING.value,
            SubOrderStatus.READY.value,
        }
        if statuses & unresolved:
            return  # ещё ждём кого-то

        # Убираем отклонённые подзаказы из расчёта
        active_subs = [
            s
            for s in order.sub_orders
            if s.status != SubOrderStatus.REJECTED.value
        ]
        if not active_subs:
            order.status = OrderStatus.CANCELLED.value
            await self._log_status(session, order)
            await self._notifier.send(
                session,
                order.client.telegram_id,
                "😔 Ни один магазин не смог подтвердить наличие по вашему списку.",
            )
            return

        # Пересчитываем итог только по подтверждённым
        order.total_estimated = sum(
            float(s.subtotal_estimated or 0) for s in active_subs
        )
        order.status = OrderStatus.PENDING_CLIENT.value
        await self._log_status(session, order)
        await session.flush()

        from app.keyboards.client_kb import approve_preorder_keyboard

        rejected_names = [
            s.shop.name
            for s in order.sub_orders
            if s.status == SubOrderStatus.REJECTED.value and s.shop
        ]
        warn = ""
        if rejected_names:
            warn = "\n⚠️ Отклонены: " + ", ".join(rejected_names)

        text = (
            self._format_preorder(order)
            + warn
            + "\n\nПодтвердите заказ в течение "
            f"{self._settings.timeout_client_approve // 60} мин."
        )
        await self._notifier.send(
            session,
            order.client.telegram_id,
            text,
            keyboard=approve_preorder_keyboard(order.id),
        )
        # Таймер утверждения покупателем (§11.1)
        self._scheduler.schedule_once(
            job_id_client_approve(order.id),
            self._settings.timeout_client_approve,
            self._timeout_runner.on_client_approve_timeout,  # type: ignore[attr-defined]
            order.id,
        )

    # ------------------------------------------------------------------
    # Утверждение покупателем
    # ------------------------------------------------------------------

    async def client_approve(
        self,
        session: AsyncSession,
        order: Order,
        delivery_type: str,
        payment_method: str,
        changed_by: Optional[int] = None,
    ) -> None:
        """Покупатель утвердил заказ и выбрал способ получения/оплаты (C8–C10)."""
        if order.status != OrderStatus.PENDING_CLIENT.value:
            return
        self._scheduler.cancel(job_id_client_approve(order.id))

        # Проверка зоны доставки (§6.3): адрес ≤ delivery_radius_m от магазинов
        delivery = getattr(self, "_delivery", None)
        if delivery_type == DeliveryType.COURIER.value and delivery is not None:
            zone_error = delivery.zone_error_for_order(order)
            if zone_error is not None:
                raise ValueError(zone_error)

        order.delivery_type = delivery_type
        order.payment_method = payment_method
        if delivery_type == DeliveryType.COURIER.value:
            order.delivery_cost = float(self._settings.delivery_cost)
            order.total_estimated = float(order.total_estimated or 0) + float(
                order.delivery_cost
            )

        order.status = OrderStatus.ASSEMBLING.value
        await self._log_status(session, order, changed_by=changed_by)
        await session.flush()

        # Сообщаем магазинам: собирайте
        from app.keyboards.seller_kb import sub_assembled_keyboard

        for sub in order.sub_orders:
            if sub.status != SubOrderStatus.CONFIRMED.value:
                continue
            sub.status = SubOrderStatus.ASSEMBLING.value
            if sub.shop:
                lines = [f"🛒 Собирайте заказ #{order.id}:"]
                for item in sub.items:
                    unit = item.unit or ""
                    lines.append(f"• {item.raw_name} — {item.qty or 0:g} {unit}")
                lines.append(
                    "Отметьте, когда соберёте. Если вес отличается — укажите факт."
                )
                await self._notifier.notify_shop(
                    session,
                    sub.shop,
                    "\n".join(lines),
                    keyboard=sub_assembled_keyboard(sub.id),
                )
            self._scheduler.schedule_once(
                job_id_assemble(order.id, sub.id),
                self._settings.timeout_seller_assemble,
                self._timeout_runner.on_assemble_timeout,  # type: ignore[attr-defined]
                order.id,
                sub.id,
            )
        await session.flush()

        delivery_note = (
            f"🛵 Доставка {order.delivery_cost} ₽. "
            if delivery_type == DeliveryType.COURIER.value
            else "🚶 Самовывоз. "
        )
        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"✅ Заказ #{order.id} принят!\n{delivery_note}"
            f"💰 Итого ~{float(order.total_estimated or 0):.0f} ₽ (постоплата при получении).\n"
            "Магазины собирают — сообщу, когда будет готово.",
        )

    # ------------------------------------------------------------------
    # Сборка
    # ------------------------------------------------------------------

    async def mark_assembled(
        self,
        session: AsyncSession,
        sub_order: SubOrder,
        actual_items: Optional[dict[int, tuple[float, float]]] = None,
        changed_by: Optional[int] = None,
    ) -> None:
        """Продавец отметил «Собрал» (S8).

        `actual_items` — {item_id: (qty, price)} фактические значения (опционально).
        """
        if sub_order.status not in (
            SubOrderStatus.ASSEMBLING.value,
            SubOrderStatus.CONFIRMED.value,
        ):
            return
        self._scheduler.cancel(job_id_assemble(sub_order.order_id, sub_order.id))

        subtotal = 0.0
        for item in sub_order.items:
            if actual_items and item.id in actual_items:
                qty, price = actual_items[item.id]
                item.actual_qty = qty
                item.actual_price = price
            else:
                item.actual_qty = item.qty
                item.actual_price = item.price
            subtotal += float(item.actual_qty or 0) * float(item.actual_price or 0)
        sub_order.subtotal_actual = subtotal
        sub_order.status = SubOrderStatus.READY.value
        sub_order.assembled_at = utcnow()
        await session.flush()
        await self._finalize_if_all_ready(session, sub_order.order_id, changed_by=changed_by)

    async def seller_cancels_part(
        self,
        session: AsyncSession,
        sub_order: SubOrder,
        reason: str = "",
        changed_by: Optional[int] = None,
    ) -> bool:
        """Продавец отменяет свою часть заказа во время сборки (S6, «с причиной»).

        Пересчитывает сумму, уведомляет покупателя; если живых позиций не
        осталось — отменяет весь заказ. Если остались только готовые —
        завершает сборку и уведомляет покупателя о готовности.
        Возвращает False, если сейчас отменять нельзя.
        """
        order = await self.get_order(session, sub_order.order_id)
        if order is None or order.status != OrderStatus.ASSEMBLING.value:
            return False
        if sub_order.status not in (
            SubOrderStatus.ASSEMBLING.value,
            SubOrderStatus.CONFIRMED.value,
        ):
            return False

        self._scheduler.cancel(job_id_assemble(order.id, sub_order.id))
        sub_order.status = SubOrderStatus.CANCELLED.value
        if reason:
            for item in sub_order.items:
                item.note = reason
        await session.flush()

        shop_name = sub_order.shop.name if sub_order.shop else "Магазин"
        active = [
            s
            for s in order.sub_orders
            if s.status
            not in (SubOrderStatus.REJECTED.value, SubOrderStatus.CANCELLED.value)
        ]
        if not active:
            # Ни одной живой позиции — заказ целиком
            order.status = OrderStatus.CANCELLED.value
            await self._log_status(session, order, changed_by=changed_by)
            await session.flush()
            await self._notifier.send(
                session,
                order.client.telegram_id,
                f"❌ Заказ #{order.id} отменён: магазин «{shop_name}» отказался "
                + (f"({reason})." if reason else "от своей части."),
            )
            return True

        order.total_estimated = await self._recalc_total(session, order)
        await session.flush()

        warn = f"⚠️ Магазин «{shop_name}» отказался от своей части заказа #{order.id}"
        if reason:
            warn += f": {reason}"
        warn += f".\n💰 Новая сумма ~{float(order.total_estimated or 0):.0f} ₽"
        await self._notifier.send(session, order.client.telegram_id, warn)
        # Остались только собранные? Завершаем сборку, иначе ждём остальных
        await self._finalize_if_all_ready(session, order.id, changed_by=changed_by)
        return True

    async def _finalize_if_all_ready(
        self,
        session: AsyncSession,
        order_id: int,
        changed_by: Optional[int] = None,
    ) -> None:
        """Когда все подзаказы собраны (или исключены) — статус READY и уведомление."""
        order = await self.get_order(session, order_id)
        if order is None:
            return
        # Все подзаказы готовы?
        if any(
            s.status
            in (SubOrderStatus.ASSEMBLING.value, SubOrderStatus.CONFIRMED.value)
            for s in order.sub_orders
        ):
            return

        order.status = OrderStatus.READY.value
        order.assembled_at = utcnow()
        order.total_actual = sum(
            float(s.subtotal_actual or 0)
            for s in order.sub_orders
            if s.status == SubOrderStatus.READY.value
        ) + float(order.delivery_cost or 0)
        await self._log_status(session, order, changed_by=changed_by)
        await session.flush()

        from app.keyboards.client_kb import ready_keyboard

        # Адреса магазинов для самовывоза / инструкция для доставки
        total_actual = float(order.total_actual or 0)
        lines = [f"✅ Заказ #{order.id} собран!", f"💰 Итого: {total_actual:.0f} ₽"]
        if order.delivery_type == DeliveryType.PICKUP.value or order.delivery_type is None:
            lines.append("📍 Заберите:")
            for s in order.sub_orders:
                if s.status == SubOrderStatus.READY.value and s.shop:
                    lines.append(f"• «{s.shop.name}», {s.shop.address_text or 'адрес уточните'}")
            lines.append(
                f"⏰ Заберите в течение {self._settings.timeout_client_pickup // 60} минут."
            )
        else:
            lines.append("🛵 Курьер заберёт и доставит вам.")
            # Назначаем курьера

            # delivery сервис доступен через реестр? используем локально
            pass

        await self._notifier.send(
            session,
            order.client.telegram_id,
            "\n".join(lines),
            keyboard=ready_keyboard(order.id),
        )
        # Фото позиций (если продавцы прикрепили к карточкам) — §3.3 S2
        seen: set[str] = set()
        sent = 0
        for s in order.sub_orders:
            if s.status != SubOrderStatus.READY.value:
                continue
            for item in s.items:
                product = item.product
                file_id = product.photo_file_id if product else None
                if not file_id or file_id in seen:
                    continue
                seen.add(file_id)
                await self._notifier.send_photo(
                    session,
                    order.client.telegram_id,
                    file_id,
                    caption=f"🥕 {item.raw_name}",
                )
                sent += 1
                if sent >= 3:
                    break
            if sent >= 3:
                break
        # Таймер получения/оплаты
        self._scheduler.schedule_once(
            job_id_pickup(order.id),
            self._settings.timeout_client_pickup,
            self._timeout_runner.on_pickup_timeout,  # type: ignore[attr-defined]
            order.id,
        )

        # Для доставки: пробуем назначить курьера сразу
        if order.delivery_type == DeliveryType.COURIER.value:
            await self._try_assign_courier(session, order)

    async def _try_assign_courier(self, session: AsyncSession, order: Order) -> None:
        """Пытается назначить курьера; если нет — пишет админам."""
        from app.services.delivery import DeliveryService

        # Инстанс доставки придёт из реестра через референс
        delivery: DeliveryService = self._delivery  # type: ignore[attr-defined]
        courier = await delivery.assign_courier(session, order)
        if courier is None:
            settings_admins = self._settings.admin_ids
            await self._notifier.notify_admins(
                session,
                settings_admins,
                f"⚠️ Заказ #{order.id}: нужен курьер, свободных нет. "
                "Назначьте вручную или возьмите как продавец.",
            )
            return

        order.status = OrderStatus.DELIVERING.value
        await self._log_status(session, order)
        await session.flush()

        from app.keyboards.courier_kb import courier_order_keyboard

        lines = [
            f"🛵 Новая доставка (заказ #{order.id}):",
        ]
        for s in order.sub_orders:
            if s.status == SubOrderStatus.READY.value and s.shop:
                lines.append(f"🏪 Забрать: «{s.shop.name}», {s.shop.address_text}")
        lines.append(
            f"🏠 Доставить: {order.delivery_address or 'адрес клиента в профиле'}"
        )
        lines.append("💰 К оплате курьером не принимается — постоплата покупателю.")
        await self._notifier.send(
            session,
            courier.telegram_id,
            "\n".join(lines),
            keyboard=courier_order_keyboard(order.id),
        )
        self._scheduler.schedule_once(
            job_id_courier_pickup(order.id),
            self._settings.timeout_courier_pickup,
            self._timeout_runner.on_courier_pickup_timeout,  # type: ignore[attr-defined]
            order.id,
        )

    def bind_delivery(self, delivery_service: object) -> None:
        """Привязывает DeliveryService (избегаем циклического импорта)."""
        self._delivery = delivery_service

    # ------------------------------------------------------------------
    # Курьер
    # ------------------------------------------------------------------

    async def courier_picked_up(
        self, session: AsyncSession, order: Order, changed_by: Optional[int] = None
    ) -> None:
        """Курьер забрал заказ у магазина(ов) (K3)."""
        if order.status != OrderStatus.DELIVERING.value:
            return
        self._scheduler.cancel(job_id_courier_pickup(order.id))
        for sub in order.sub_orders:
            if sub.status == SubOrderStatus.READY.value:
                sub.status = SubOrderStatus.PICKED_UP.value
                sub.picked_up_at = utcnow()
        order.picked_up_at = utcnow()
        await self._log_status(session, order, changed_by=changed_by)
        await session.flush()

        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"🛵 Курьер забрал ваш заказ #{order.id} и несёт к вам!",
        )
        self._scheduler.schedule_once(
            job_id_courier_deliver(order.id),
            self._settings.timeout_courier_deliver,
            self._timeout_runner.on_courier_deliver_timeout,  # type: ignore[attr-defined]
            order.id,
        )

    async def courier_delivered(
        self, session: AsyncSession, order: Order, changed_by: Optional[int] = None
    ) -> None:
        """Курьер доставил заказ (K4)."""
        if order.status != OrderStatus.DELIVERING.value:
            return
        self._scheduler.cancel(job_id_courier_deliver(order.id))
        order.status = OrderStatus.READY.value
        await self._log_status(session, order, changed_by=changed_by)
        await session.flush()

        from app.keyboards.client_kb import ready_keyboard

        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"🏠 Заказ #{order.id} доставлен!\n"
            f"💰 К оплате: {float(order.total_actual or 0):.0f} ₽ (постоплата).",
            keyboard=ready_keyboard(order.id),
        )
        self._scheduler.schedule_once(
            job_id_pickup(order.id),
            self._settings.timeout_client_payment,
            self._timeout_runner.on_pickup_timeout,  # type: ignore[attr-defined]
            order.id,
        )

    # ------------------------------------------------------------------
    # Оплата (постоплата, §5)
    # ------------------------------------------------------------------

    async def client_says_paid(
        self, session: AsyncSession, order: Order, changed_by: Optional[int] = None
    ) -> None:
        """Покупатель нажал «Оплатил» — ждём подтверждения продавца."""
        if order.status != OrderStatus.READY.value:
            return
        # Сообщаем продавцам
        for sub in order.sub_orders:
            if sub.status != SubOrderStatus.READY.value or not sub.shop:
                continue
            from app.keyboards.seller_kb import payment_confirm_keyboard

            await self._notifier.notify_shop(
                session,
                sub.shop,
                f"💵 Покупатель сообщил об оплате заказа #{order.id} "
                f"({order.payment_method}). Подтвердите получение.",
                keyboard=payment_confirm_keyboard(order.id, sub.id),
            )
        await self._notifier.send(
            session,
            order.client.telegram_id,
            "👍 Отлично! Передал продавцу — он подтвердит получение оплаты.",
        )

    async def seller_confirmed_payment(
        self, session: AsyncSession, order: Order, changed_by: Optional[int] = None
    ) -> None:
        """Продавец нажал «Получил оплату» — заказ закрыт (S8)."""
        if order.status not in (OrderStatus.READY.value, OrderStatus.PAID.value):
            return
        self._scheduler.cancel(job_id_pickup(order.id))
        for sub in order.sub_orders:
            if sub.status == SubOrderStatus.READY.value:
                sub.status = SubOrderStatus.DONE.value
        order.payment_status = PaymentStatus.PAID.value
        order.paid_at = utcnow()
        order.status = OrderStatus.DONE.value
        await self._log_status(session, order, changed_by=changed_by)
        await session.flush()
        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"✅ Заказ #{order.id} оплачен и закрыт. Спасибо за покупку!",
        )

    async def seller_got_cash(
        self,
        session: AsyncSession,
        order: Order,
        sub_order: SubOrder,
        changed_by: Optional[int] = None,
    ) -> None:
        """Продавец подтвердил выдачу/получение денег при самовывозе."""
        sub_order.status = SubOrderStatus.PICKED_UP.value
        sub_order.picked_up_at = utcnow()
        await session.flush()
        # Если все подзаказы выданы — закрываем
        order = await self.get_order(session, order.id)
        if order is None:
            return
        if all(
            s.status in (SubOrderStatus.PICKED_UP.value, SubOrderStatus.DONE.value)
            for s in order.sub_orders
        ):
            await self.seller_confirmed_payment(session, order, changed_by=changed_by)

    # ------------------------------------------------------------------
    # Отмена и неявка
    # ------------------------------------------------------------------

    async def client_cancel(
        self,
        session: AsyncSession,
        order: Order,
        reason: str = "",
        changed_by: Optional[int] = None,
    ) -> None:
        """Отмена покупателем до сборки (C13) — без штрафа."""
        cancellable = {
            OrderStatus.DRAFT.value,
            OrderStatus.PENDING_SELLERS.value,
            OrderStatus.PENDING_CLIENT.value,
            OrderStatus.PREORDER_PENDING.value,
            OrderStatus.PREORDER_REMINDED.value,
            OrderStatus.PREORDER_SEARCHING.value,
        }
        if order.status not in cancellable:
            return
        self._scheduler.cancel_prefix(f"order:{order.id}:")
        order.status = (
            OrderStatus.PREORDER_CANCELLED.value
            if order.order_type == OrderType.PREORDER.value
            else OrderStatus.CANCELLED.value
        )
        for sub in order.sub_orders:
            sub.status = SubOrderStatus.CANCELLED.value
        await self._log_status(session, order, changed_by=changed_by)
        await session.flush()
        # Уведомляем магазины
        for sub in order.sub_orders:
            if sub.shop:
                await self._notifier.notify_shop(
                    session,
                    sub.shop,
                    f"❌ Покупатель отменил заказ #{order.id}.",
                )
        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"❌ Заказ #{order.id} отменён."
            + (f"\nПричина: {reason}" if reason else ""),
        )
        if order.order_type == OrderType.PREORDER.value:
            session.add(
                PreorderLog(order_id=order.id, event=PreorderEvent.CANCELLED.value)
            )
            await session.flush()

    async def admin_cancel(
        self,
        session: AsyncSession,
        order: Order,
        admin_id: int,
        reason: str = "",
    ) -> bool:
        """A4: ручное вмешательство администратора — отмена на любом этапе.

        В отличие от client_cancel не ограничивает статусы: администратор
        вправе остановить заказ и в сборке, и в доставке. Возвращает False,
        если заказ уже отменён или закрыт.
        """
        terminal = {
            OrderStatus.CANCELLED.value,
            OrderStatus.PREORDER_CANCELLED.value,
            OrderStatus.DONE.value,
        }
        if order.status in terminal:
            return False
        self._scheduler.cancel_prefix(f"order:{order.id}:")
        order.status = (
            OrderStatus.PREORDER_CANCELLED.value
            if order.order_type == OrderType.PREORDER.value
            else OrderStatus.CANCELLED.value
        )
        for sub in order.sub_orders:
            sub.status = SubOrderStatus.CANCELLED.value
        await self._log_status(session, order, changed_by=admin_id)
        await session.flush()
        for sub in order.sub_orders:
            if sub.shop:
                await self._notifier.notify_shop(
                    session,
                    sub.shop,
                    f"🛑 Заказ #{order.id} отменён администратором.",
                )
        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"🛑 Заказ #{order.id} отменён администратором."
            + (f"\nПричина: {reason}" if reason else ""),
        )
        if order.order_type == OrderType.PREORDER.value:
            session.add(
                PreorderLog(order_id=order.id, event=PreorderEvent.CANCELLED.value)
            )
            await session.flush()
        return True

    async def register_no_show(
        self, session: AsyncSession, order: Order, reason: str
    ) -> None:
        """Фиксирует неявку/неоплату и применяет бан-логику (§5.4)."""
        order.status = OrderStatus.NO_SHOW.value
        await self._log_status(session, order)
        await session.flush()
        client = await session.get(User, order.client_id)
        if client is not None:
            await self._ban.register_violation(
                session, client, reason=reason, order_id=order.id
            )

    # ------------------------------------------------------------------
    # Таймауты (вызываются TimeoutRunner)
    # ------------------------------------------------------------------

    async def handle_seller_confirm_timeout(
        self, session: AsyncSession, order_id: int, shop_id: int
    ) -> None:
        """Магазин не ответил за отведённое время — исключаем его (§11.1)."""
        order = await self.get_order(session, order_id)
        if order is None or order.status != OrderStatus.PENDING_SELLERS.value:
            return
        sub = next(
            (s for s in order.sub_orders if s.shop_id == shop_id), None
        )
        if sub is None or sub.status != SubOrderStatus.PENDING.value:
            return
        sub.status = SubOrderStatus.REJECTED.value
        await session.flush()
        logger.info("Магазин %s исключён из заказа %s (таймаут)", shop_id, order_id)
        await self._try_finalize_sellers(session, order)

    async def handle_client_approve_timeout(
        self, session: AsyncSession, order_id: int
    ) -> None:
        """Покупатель не утвердил предзаказ — отмена (§11.1)."""
        order = await self.get_order(session, order_id)
        if order is None or order.status != OrderStatus.PENDING_CLIENT.value:
            return
        order.status = OrderStatus.CANCELLED.value
        for sub in order.sub_orders:
            sub.status = SubOrderStatus.CANCELLED.value
        await self._log_status(session, order)
        await session.flush()
        await self._notifier.send(
            session,
            order.client.telegram_id,
            f"❌ Предзаказ #{order.id} отменён: не получено подтверждение вовремя.",
        )

    async def handle_assemble_timeout(
        self, session: AsyncSession, order_id: int, sub_order_id: int
    ) -> None:
        """Магазин не собрал — напоминание, затем отмена (§11.1)."""
        order = await self.get_order(session, order_id)
        if order is None:
            return
        sub = next((s for s in order.sub_orders if s.id == sub_order_id), None)
        if sub is None or sub.status != SubOrderStatus.ASSEMBLING.value:
            return
        # Первый таймаут — напоминание; повторный (когда job пересоздан) — отмена
        if not getattr(sub, "_assemble_reminded", False):
            # Используем note как маркер напоминания
            reminded = any(
                (i.note or "").startswith("напоминание") for i in sub.items
            )
            if not reminded:
                for item in sub.items:
                    item.note = "напоминание о сборке"
                if sub.shop:
                    from app.keyboards.seller_kb import sub_assembled_keyboard as _kb

                    await self._notifier.notify_shop(
                        session,
                        sub.shop,
                        f"⏰ Напоминание: заказ #{order.id} всё ещё ждёт сборки!",
                        keyboard=_kb(sub.id),
                    )
                self._scheduler.schedule_once(
                    job_id_assemble(order.id, sub.id),
                    self._settings.timeout_seller_assemble,
                    self._timeout_runner.on_assemble_timeout,  # type: ignore[attr-defined]
                    order.id,
                    sub.id,
                )
                return
        # Второй раз — отменяем подзаказ
        sub.status = SubOrderStatus.REJECTED.value
        await session.flush()
        # Если это был единственный — отменяем заказ
        if all(
            s.status in (SubOrderStatus.REJECTED.value, SubOrderStatus.CANCELLED.value)
            for s in order.sub_orders
        ):
            order.status = OrderStatus.CANCELLED.value
            await self._log_status(session, order)
            await self._notifier.send(
                session,
                order.client.telegram_id,
                f"❌ Заказ #{order.id} отменён: магазины не собрали вовремя.",
            )
        else:
            await self._notifier.send(
                session,
                order.client.telegram_id,
                f"⚠️ Магазин не собрал свою часть заказа #{order.id} вовремя. "
                "Остальные позиции продолжаем готовить.",
            )

    async def handle_pickup_timeout(
        self, session: AsyncSession, order_id: int
    ) -> None:
        """Покупатель не забрал/не оплатил — бан-логика (§5.4)."""
        order = await self.get_order(session, order_id)
        if order is None or order.status not in (
            OrderStatus.READY.value,
            OrderStatus.PAID.value,
        ):
            return
        await self.register_no_show(
            session,
            order,
            reason="Не забрал/не оплатил заказ вовремя",
        )
        for sub in order.sub_orders:
            if sub.shop:
                await self._notifier.notify_shop(
                    session,
                    sub.shop,
                    f"😔 Покупатель не забрал заказ #{order.id} вовремя. "
                    "Позиции можно вернуть на витрину.",
                )

    async def handle_courier_pickup_timeout(
        self, session: AsyncSession, order_id: int
    ) -> None:
        """Курьер не забрал заказ — напоминание админам."""
        order = await self.get_order(session, order_id)
        if order is None or order.status != OrderStatus.DELIVERING.value:
            return
        await self._notifier.notify_admins(
            session,
            self._settings.admin_ids,
            f"⚠️ Заказ #{order.id}: курьер не забрал вовремя. Проверьте доставку.",
        )

    async def handle_courier_deliver_timeout(
        self, session: AsyncSession, order_id: int
    ) -> None:
        """Курьер не доставил — напоминание."""
        order = await self.get_order(session, order_id)
        if order is None or order.status != OrderStatus.DELIVERING.value:
            return
        await self._notifier.notify_admins(
            session,
            self._settings.admin_ids,
            f"⚠️ Заказ #{order.id}: курьер не доставил вовремя.",
        )
        # Напоминаем самому курьеру
        if order.courier_id:
            courier = await session.get(User, order.courier_id)
            if courier:
                await self._notifier.send(
                    session,
                    courier.telegram_id,
                    f"⏰ Заказ #{order.id}: доставка задерживается. Отметьте доставку.",
                )

    # ------------------------------------------------------------------
    # Повтор заказа (C12)
    # ------------------------------------------------------------------

    async def repeat_order(
        self, session: AsyncSession, client: User, source_order: Order
    ) -> tuple[Optional[Order], str]:
        """Формирует новый заказ по позициям прошлого (кнопка «Повторить»)."""
        lines: list[str] = []
        for sub in source_order.sub_orders:
            for item in sub.items:
                unit = item.unit or ""
                qty = item.qty or 0
                lines.append(f"{item.raw_name} {qty:g} {unit}")
        if not lines:
            return None, "В старом заказе нечего повторять."
        text = ", ".join(lines)
        return await self.create_from_text(session, client, text)

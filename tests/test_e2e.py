"""E2E-сценарий целиком: реальный Dispatcher + мок-бот Telegram (Э6).

Прогоняет сценарий §4.1 ТЗ от списка покупателя до закрытия заказа — через
настоящие роутеры, middleware, FSM и сервисы. Telegram API перехватывается
FakeTelegramSession: вызовы записываются, ответы подставляются моделями aiogram.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Chat
from aiogram.types import Message as TgMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
    SubOrderStatus,
)
from app.db.models import Order, SubOrder
from tests.conftest import make_product, make_shop, make_user
from tests.services_fixture import ServiceStack

# ----------------------------------------------------------------------
# Мок-сессия Telegram API
# ----------------------------------------------------------------------


def _fake_response(method: Any) -> Any:
    """Ответ Telegram API для перехваченного метода."""
    from aiogram.methods import EditMessageText, SendMessage, SendPhoto

    now = dt.datetime.now(dt.timezone.utc)
    if isinstance(method, (SendMessage, SendPhoto)):
        chat_id = int(getattr(method, "chat_id", 0) or 0)
        return TgMessage(
            message_id=abs(hash(type(method).__name__)) % 100000,
            date=now,
            chat=Chat(id=chat_id, type="private"),
            text=getattr(method, "text", None) or getattr(method, "caption", None),
        )
    if isinstance(method, EditMessageText):
        return TgMessage(
            message_id=int(getattr(method, "message_id", 1) or 1),
            date=now,
            chat=Chat(id=int(getattr(method, "chat_id", 0) or 0), type="private"),
            text=method.text,
        )
    return True


class FakeTelegramSession:
    """Сессия-заглушка: записывает API-вызовы, сеть не используется."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def __call__(self, bot: Bot, method: Any, timeout: float | None = None) -> Any:
        self.calls.append(method)
        return _fake_response(method)

    async def close(self) -> None:
        """Ничего не делает."""

    async def stream_content(self, *args: Any, **kwargs: Any):  # pragma: no cover
        """Не используется в E2E."""
        return b""


# ----------------------------------------------------------------------
# Драйвер апдейтов
# ----------------------------------------------------------------------


@dataclass
class E2EDriver:
    """Кормит дискетчер сырыми апдейтами от имени пользователей."""

    dp: Dispatcher
    bot: Bot
    api: FakeTelegramSession
    stack: ServiceStack
    db: AsyncSession
    _update_id: int = 1000
    _message_id: int = 5000

    def _base_message(self, user_id: int) -> dict[str, Any]:
        self._message_id += 1
        return {
            "message_id": self._message_id,
            "date": "2026-10-17T10:00:00+00:00",
            "chat": {"id": user_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": f"U{user_id}"},
        }

    async def text(self, user_id: int, text: str) -> None:
        """Текстовое сообщение от пользователя."""
        self._update_id += 1
        raw = {"update_id": self._update_id, "message": {**self._base_message(user_id), "text": text}}
        await self.dp.feed_raw_update(bot=self.bot, update=raw)

    async def cb(self, user_id: int, data: str) -> None:
        """Нажатие inline-кнопки от пользователя."""
        self._update_id += 1
        message = self._base_message(user_id)
        message["from"] = {"id": 42, "is_bot": True, "first_name": "Овощной"}
        message["text"] = f"callback {data}"
        raw = {
            "update_id": self._update_id,
            "callback_query": {
                "id": f"cbq{self._update_id}",
                "from": {"id": user_id, "is_bot": False, "first_name": f"U{user_id}"},
                "chat_instance": "ci",
                "data": data,
                "message": message,
            },
        }
        await self.dp.feed_raw_update(bot=self.bot, update=raw)

    async def location(self, user_id: int, lat: float, lon: float) -> None:
        """Геолокация от пользователя (скрепка 📎 → Геопозиция)."""
        self._update_id += 1
        raw = {
            "update_id": self._update_id,
            "message": {
                **self._base_message(user_id),
                "location": {"latitude": lat, "longitude": lon},
            },
        }
        await self.dp.feed_raw_update(bot=self.bot, update=raw)

    # --- Выходящие сообщения (перехваченные API-вызовы) ---

    def sent_texts(self, chat_id: int) -> list[str]:
        """Тексты, отправленные ботом в указанный чат."""
        from aiogram.methods import SendMessage

        return [
            m.text
            for m in self.api.calls
            if isinstance(m, SendMessage) and int(m.chat_id) == chat_id and m.text
        ]

    def sent_edits(self, chat_id: int) -> list[str]:
        """Тексты сообщений, отредактированных ботом в указанном чате."""
        from aiogram.methods import EditMessageText

        return [
            m.text
            for m in self.api.calls
            if isinstance(m, EditMessageText)
            and int(m.chat_id or 0) == chat_id
            and m.text
        ]

    def sent_pins(self, chat_id: int) -> list[tuple[float, float]]:
        """Координаты точек (гео-пинов), отправленных ботом в чат."""
        from aiogram.methods import SendLocation

        return [
            (m.latitude, m.longitude)
            for m in self.api.calls
            if isinstance(m, SendLocation) and int(m.chat_id) == chat_id
        ]

    def sent_photos(self, chat_id: int) -> list[str]:
        """file_id фото, отправленные ботом в указанный чат."""
        from aiogram.methods import SendPhoto

        return [
            m.photo
            for m in self.api.calls
            if isinstance(m, SendPhoto) and int(m.chat_id) == chat_id
        ]

    # --- Чтение состояния БД (с обновлением кэша сессии) ---

    async def get_order(self, order_id: int) -> Order:
        """Заказ из свежего состояния БД."""
        self.db.expire_all()
        order = await self.db.get(Order, order_id)
        assert order is not None
        return order

    async def get_sub(self, sub_id: int) -> SubOrder:
        """Подзаказ из свежего состояния БД."""
        self.db.expire_all()
        sub = await self.db.get(SubOrder, sub_id)
        assert sub is not None
        return sub


# Фикстура `e2e` определена в tests/conftest.py — она общая для всех E2E-модулей


# ----------------------------------------------------------------------
# Сценарий §4.1: список → подтверждения → сборка с фактом → закрытие
# ----------------------------------------------------------------------


async def test_e2e_full_pickup_scenario(e2e: E2EDriver) -> None:
    """Полный путь заказа самовывозом: 2 магазина, один отказал, факт-вес, оплата."""
    db = e2e.db
    _client = await make_user(db, telegram_id=1001, lat=45.0443, lon=38.9763)
    shop_a = await make_shop(db, telegram_id=2001, name="У Пети")
    shop_b = await make_shop(db, telegram_id=2002, name="У Маши")
    await make_product(db, shop_a, name="Капуста", price=50, unit="кг")
    await make_product(db, shop_a, name="Помидоры", price=200, unit="кг")
    await make_product(db, shop_b, name="Морковь", price=80, unit="кг")
    await db.commit()
    shop_a_id, shop_b_id = shop_a.id, shop_b.id
    client_tg, a_tg, b_tg = 1001, 2001, 2002

    # --- t=0: покупатель открывает новый заказ и шлёт список ---
    await e2e.text(client_tg, "📝 Новый заказ")
    await e2e.text(client_tg, "капуста 4 кг, помидоры 2 кг, морковь 1 кг")

    db.expire_all()
    orders = list((await db.execute(select(Order))).scalars())
    assert len(orders) == 1
    order = orders[0]
    order_id = order.id
    assert order.status == OrderStatus.PENDING_SELLERS.value
    assert len(order.sub_orders) == 2
    sub_a = next(s for s in order.sub_orders if s.shop_id == shop_a_id)
    sub_b = next(s for s in order.sub_orders if s.shop_id == shop_b_id)
    sub_a_id, sub_b_id = sub_a.id, sub_b.id
    # А: капуста 200 + помидоры 400 = 600; Б: морковь 80
    assert float(sub_a.subtotal_estimated) == 600.0
    assert float(sub_b.subtotal_estimated) == 80.0
    # Оба магазина получили запрос с кнопками подтверждения
    assert any("капуста" in m.lower() for m in e2e.stack.notifier.texts_for(a_tg))
    assert any("морковь" in m.lower() for m in e2e.stack.notifier.texts_for(b_tg))

    # --- продавец А подтверждает, Б отклоняет ---
    await e2e.cb(a_tg, f"s:y:{sub_a_id}")
    await e2e.cb(b_tg, f"s:n:{sub_b_id}")

    order = await e2e.get_order(order_id)
    assert order.status == OrderStatus.PENDING_CLIENT.value
    # Жив только магазин А
    assert float(order.total_estimated) == 600.0
    sub_a = next(s for s in order.sub_orders if s.id == sub_a_id)
    assert sub_a.status == SubOrderStatus.CONFIRMED.value
    # Покупатель получил сводку и уведомление об отклонённом магазине
    client_msgs = e2e.stack.notifier.texts_for(client_tg)
    assert any("Отклонены" in m and "У Маши" in m for m in client_msgs)

    # --- покупатель подтверждает: самовывоз + наличные ---
    await e2e.cb(client_tg, f"oa:{order_id}")
    await e2e.cb(client_tg, f"od:{order_id}:pickup")
    await e2e.cb(client_tg, f"op:{order_id}:cash")

    order = await e2e.get_order(order_id)
    assert order.status == OrderStatus.ASSEMBLING.value
    assert order.delivery_type == "pickup"
    assert order.payment_method == PaymentMethod.CASH.value
    # Продавец А получил задание собрать
    assert any(
        "Собирайте" in m for m in e2e.stack.notifier.texts_for(a_tg)
    )

    # --- продавец А собирает, вводя фактический вес (§4.1) ---
    await e2e.cb(a_tg, f"s:a:{sub_a_id}")
    sent = e2e.sent_texts(a_tg)
    assert any("Сборка 1/2" in t and "капуста" in t.lower() for t in sent)

    await e2e.text(a_tg, "4")  # капуста — совпало с планом
    sent = e2e.sent_texts(a_tg)
    assert any("Сборка 2/2" in t and "помидоры" in t.lower() for t in sent)

    await e2e.text(a_tg, "1.2")  # помидоры — фактически 1.2 кг
    order = await e2e.get_order(order_id)
    sub_a = next(s for s in order.sub_orders if s.id == sub_a_id)
    assert order.status == OrderStatus.READY.value
    assert sub_a.status == SubOrderStatus.READY.value
    # Капуста 4×50=200, помидоры 1.2×200=240 → итого 440
    assert float(sub_a.subtotal_actual) == 440.0
    assert float(order.total_actual) == 440.0
    assert any(
        "440" in m for m in e2e.stack.notifier.texts_for(client_tg)
    )

    # --- покупатель «оплатил», продавец подтверждает получение ---
    await e2e.cb(client_tg, f"orp:{order_id}")
    assert any(
        "Покупатель сообщил об оплате" in m
        for m in e2e.stack.notifier.texts_for(a_tg)
    )
    await e2e.cb(a_tg, f"s:p:{order_id}:{sub_a_id}")

    order = await e2e.get_order(order_id)
    assert order.status == OrderStatus.DONE.value
    assert order.payment_status == PaymentStatus.PAID.value
    assert any(
        "оплачен и закрыт" in m for m in e2e.stack.notifier.texts_for(client_tg)
    )
    # Фактические количества закреплены на позициях
    from app.db.models import OrderItem

    db.expire_all()
    items = list(
        (
            await db.execute(
                select(OrderItem).where(
                    OrderItem.sub_order_id == sub_a_id
                ).execution_options(populate_existing=True)
            )
        ).scalars()
    )
    actual_by_name = {
        (i.product.name if i.product else i.raw_name): float(i.actual_qty or 0)
        for i in items
    }
    assert actual_by_name["Капуста"] == 4.0
    assert actual_by_name["Помидоры"] == 1.2

    # --- мок-сессия перехватила вызовы Telegram API ---
    assert len(e2e.api.calls) > 0
    assert e2e.sent_photos(1001) == []  # фото не было — не отправлялись


async def test_e2e_buyer_sees_photo_when_ready(e2e: E2EDriver) -> None:
    """Мини-E2E: карточка с фото → покупатель получает фото при готовности."""
    db = e2e.db
    _client = await make_user(db, telegram_id=1101, lat=45.0443, lon=38.9763)
    shop = await make_shop(db, telegram_id=2101, name="Фруктовый")
    product = await make_product(db, shop, name="Яблоки", price=90, unit="кг")
    product.photo_file_id = "AgACAgIAAxkB-test-photo"
    await db.commit()

    await e2e.text(1101, "📝 Новый заказ")
    await e2e.text(1101, "яблоки 2 кг")
    db.expire_all()
    order = (await db.execute(select(Order))).scalars().one()
    sub_id = order.sub_orders[0].id

    await e2e.cb(2101, f"s:y:{sub_id}")
    await e2e.cb(1101, f"oa:{order.id}")
    await e2e.cb(1101, f"od:{order.id}:pickup")
    await e2e.cb(1101, f"op:{order.id}:cash")
    await e2e.cb(2101, f"s:a:{sub_id}")
    await e2e.text(2101, "ок")  # единственный товар — факт = план

    order = await e2e.get_order(order.id)
    assert order.status == OrderStatus.READY.value
    # Уведомления продавцам/покупателю идут через Notifier (в тестах — MockNotifier)
    photos = e2e.stack.notifier.photos_for(1101)
    assert [p["photo"] for p in photos] == ["AgACAgIAAxkB-test-photo"]
    assert e2e.sent_photos(1101) == []  # напрямую через bot API фото не шлись

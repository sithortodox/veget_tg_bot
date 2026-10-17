"""SQLAlchemy-модели. Схема полностью соответствует §13 ТЗ."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from geoalchemy2 import Geography
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Interval,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    """Пользователи: покупатели, продавцы, курьеры, админы."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)  # client|seller|courier|admin
    name: Mapped[Optional[str]] = mapped_column(String(100))
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    address_text: Mapped[Optional[str]] = mapped_column(Text)
    # Точка адреса в системе координат WGS84 (PostGIS GEOGRAPHY)
    address_geo: Mapped[Optional[str]] = mapped_column(Geography("POINT", srid=4326))
    address_components: Mapped[Optional[dict]] = mapped_column(JSONB)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    banned_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ban_count: Mapped[int] = mapped_column(Integer, default=0)
    ban_reason: Mapped[Optional[str]] = mapped_column(Text)
    banned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Персональный радиус поиска магазинов, G4 (300–800 м), метры
    search_radius_m: Mapped[int] = mapped_column(
        Integer, nullable=False, default=800, server_default="800"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (Index("idx_users_geo", "address_geo", postgresql_using="gist"),)


class Shop(Base):
    """Магазины-продавцы."""

    __tablename__ = "shops"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    telegram_id: Mapped[Optional[int]] = mapped_column(BigInteger, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address_text: Mapped[Optional[str]] = mapped_column(Text)
    address_geo: Mapped[Optional[str]] = mapped_column(Geography("POINT", srid=4326))
    address_components: Mapped[Optional[dict]] = mapped_column(JSONB)
    phone: Mapped[Optional[str]] = mapped_column(String(20))
    payment_details: Mapped[Optional[str]] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    moderation_status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending|approved|rejected
    # Средние времена ответа и сборки (INTERVAL, как в init.sql §13)
    avg_response_time: Mapped[Optional[object]] = mapped_column(Interval)
    avg_assemble_time: Mapped[Optional[object]] = mapped_column(Interval)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    products: Mapped[list["Product"]] = relationship(
        back_populates="shop", lazy="selectin"
    )

    __table_args__ = (Index("idx_shops_geo", "address_geo", postgresql_using="gist"),)


class Product(Base):
    """Карточка товара на витрине магазина."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    synonyms: Mapped[Optional[list[str]]] = mapped_column(ARRAY(Text))
    category: Mapped[Optional[str]] = mapped_column(String(100))
    description: Mapped[Optional[str]] = mapped_column(Text)
    price: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)  # кг|шт|вилок|...
    photo_file_id: Mapped[Optional[str]] = mapped_column(String(300))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    shop: Mapped["Shop"] = relationship(back_populates="products", lazy="selectin")

    __table_args__ = (
        Index("idx_products_shop", "shop_id", "is_active"),
        Index("idx_products_synonyms", "synonyms", postgresql_using="gin"),
    )


class Order(Base):
    """Заказ (может содержать подзаказы в нескольких магазинах)."""

    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(30), default="draft")
    order_type: Mapped[str] = mapped_column(String(20), default="now")  # now|preorder
    delivery_type: Mapped[Optional[str]] = mapped_column(String(20))  # pickup|courier
    delivery_address: Mapped[Optional[str]] = mapped_column(Text)
    delivery_geo: Mapped[Optional[str]] = mapped_column(Geography("POINT", srid=4326))
    delivery_cost: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    total_estimated: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    total_actual: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    payment_method: Mapped[Optional[str]] = mapped_column(String(20))  # cash|card|sbp
    payment_status: Mapped[Optional[str]] = mapped_column(String(20))
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    assembled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    picked_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    seller_confirmed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    search_other_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # Курьер, взявший доставку (null, пока никто не взял)
    courier_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # selectin — чтобы в async не было lazy-load (MissingGreenlet)
    sub_orders: Mapped[list["SubOrder"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", lazy="selectin"
    )
    client: Mapped["User"] = relationship(foreign_keys=[client_id], lazy="selectin")
    courier: Mapped[Optional["User"]] = relationship(
        foreign_keys=[courier_id], lazy="selectin"
    )

    __table_args__ = (
        Index("idx_orders_client", "client_id", "created_at"),
        Index("idx_orders_status", "status"),
        Index(
            "idx_orders_scheduled",
            "scheduled_at",
            postgresql_where="order_type = 'preorder'",
        ),
    )


class SubOrder(Base):
    """Подзаказ — позиции в конкретном магазине."""

    __tablename__ = "sub_orders"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"))
    shop_id: Mapped[int] = mapped_column(ForeignKey("shops.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    subtotal_estimated: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    subtotal_actual: Mapped[Optional[float]] = mapped_column(Numeric(12, 2))
    assembled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    picked_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    order: Mapped["Order"] = relationship(back_populates="sub_orders", lazy="selectin")
    shop: Mapped["Shop"] = relationship(lazy="selectin")
    items: Mapped[list["OrderItem"]] = relationship(
        back_populates="sub_order", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (Index("idx_sub_orders_shop", "shop_id", "status"),)


class OrderItem(Base):
    """Позиция заказа: что просил покупатель и что нашли на витрине."""

    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sub_order_id: Mapped[int] = mapped_column(
        ForeignKey("sub_orders.id", ondelete="CASCADE")
    )
    product_id: Mapped[Optional[int]] = mapped_column(ForeignKey("products.id"))
    raw_name: Mapped[str] = mapped_column(String(200))
    qty: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    unit: Mapped[Optional[str]] = mapped_column(String(20))
    price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    # Фактические значения после сборки (вес мог отличаться)
    actual_qty: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    actual_price: Mapped[Optional[float]] = mapped_column(Numeric(10, 2))
    status: Mapped[Optional[str]] = mapped_column(String(20))  # found|not_found|analog
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    sub_order: Mapped["SubOrder"] = relationship(back_populates="items", lazy="selectin")
    product: Mapped[Optional["Product"]] = relationship(lazy="selectin")


class OrderStatusLog(Base):
    """Лог переходов статусов заказа."""

    __tablename__ = "order_status_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    status: Mapped[str] = mapped_column(String(30))
    changed_by: Mapped[Optional[int]] = mapped_column(BigInteger)  # telegram_id
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class BanHistory(Base):
    """История банов."""

    __tablename__ = "ban_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    banned_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    banned_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class PreorderLog(Base):
    """Лог событий предзаказа."""

    __tablename__ = "preorder_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    event: Mapped[str] = mapped_column(String(50))
    shop_id: Mapped[Optional[int]] = mapped_column(ForeignKey("shops.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MessageLog(Base):
    """Лог исходящих/входящих сообщений для поддержки и разбора споров."""

    __tablename__ = "message_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(BigInteger)  # telegram_id
    direction: Mapped[Optional[str]] = mapped_column(String(10))  # out|in
    text: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

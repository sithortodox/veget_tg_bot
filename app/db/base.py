"""Базовый декларативный класс SQLAlchemy."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Единая база для всех моделей приложения."""

    pass

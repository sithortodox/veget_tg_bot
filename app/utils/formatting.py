"""Утилиты: форматирование сумм, дат, человекочитаемых величин."""

from __future__ import annotations

from datetime import datetime


def fmt_money(value: float | None) -> str:
    """Форматирует деньги: 440 → «440 ₽»."""
    if value is None:
        return "0 ₽"
    return f"{value:.0f} ₽".replace(".00", "")


def fmt_qty(qty: float | None) -> str:
    """Форматирует количество без лишних нулей: 2.0 → «2», 1.2 → «1.2»."""
    if qty is None:
        return "—"
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:g}"


def fmt_dt(dt: datetime | None, fmt: str = "%d.%m %H:%M") -> str:
    """Форматирует дату или заглушку."""
    if dt is None:
        return "—"
    return dt.strftime(fmt)

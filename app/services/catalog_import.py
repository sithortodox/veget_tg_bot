"""Массовый импорт каталога из файлов прайс-листа — Excel/CSV (S4).

Поддерживаемые форматы:
- .xlsx (openpyxl) и .csv (utf-8-sig / cp1251);
- заголовки на английском или русском (name/наименование, price/цена, ...);
- колонки: name, price, unit, description, synonyms.

Существующие товары с таким же названием (без учёта регистра) обновляются,
а не дублируются.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import VALID_UNITS
from app.db.models import Product, Shop

# Синонимы заголовков колонок (нормализация к каноническим именам)
_ALIASES: dict[str, str] = {
    "name": "name",
    "наименование": "name",
    "товар": "name",
    "название": "name",
    "price": "price",
    "цена": "price",
    "unit": "unit",
    "ед": "unit",
    "ед.": "unit",
    "единица": "unit",
    "единица измерения": "unit",
    "description": "description",
    "описание": "description",
    "synonyms": "synonyms",
    "синонимы": "synonyms",
}


def _normalize_header(raw: Any) -> str:
    """Приводит заголовок к каноническому имени колонки."""
    key = str(raw or "").strip().lower()
    return _ALIASES.get(key, key)


def _parse_rows(headers: list[Any], data_rows: list[list[Any]]) -> list[dict[str, str]]:
    """Собирает строки словарями по нормализованным заголовкам."""
    keys = [_normalize_header(h) for h in headers]
    rows: list[dict[str, str]] = []
    for raw_row in data_rows:
        row: dict[str, str] = {}
        for i, key in enumerate(keys):
            value = raw_row[i] if i < len(raw_row) else None
            row[key] = str(value).strip() if value is not None else ""
        if row.get("name"):
            rows.append(row)
    return rows


def parse_catalog_rows(raw: bytes, filename: str) -> list[dict[str, str]]:
    """Разбирает содержимое файла прайс-листа в список строк.

    Бросает ValueError с русским текстом, если формат непонятен.
    """
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        return _parse_xlsx(raw)
    if name.endswith(".csv"):
        return _parse_csv(raw)
    raise ValueError("Ожидается файл .xlsx или .csv.")


def _parse_csv(raw: bytes) -> list[dict[str, str]]:
    """CSV: первая строка — заголовки, кодировки utf-8-sig / cp1251."""
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("cp1251")
        except UnicodeDecodeError as exc:
            raise ValueError("Не удалось прочитать кодировку CSV (utf-8/cp1251).") from exc
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("Файл CSV пуст.")
    return _parse_rows(rows[0], rows[1:])


def _parse_xlsx(raw: bytes) -> list[dict[str, str]]:
    """Excel: первый лист, первая строка — заголовки."""
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover — зависимость заявлена в requirements
        raise ValueError("Модуль openpyxl не установлен.") from exc
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — openpyxl кидает разные типы ошибок
        raise ValueError("Файл не похож на валидный Excel (.xlsx).") from exc
    try:
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        try:
            headers = next(rows_iter)
        except StopIteration:
            raise ValueError("Файл Excel пуст.") from None
        data_rows = [list(r) for r in rows_iter]
    finally:
        wb.close()
    return _parse_rows(list(headers), data_rows)


async def import_catalog_rows(
    session: AsyncSession, shop: Shop, rows: list[dict[str, str]]
) -> tuple[int, int]:
    """Импортирует строки в витрину магазина. Возвращает (добавлено, обновлено)."""
    existing = {
        p.name.lower(): p
        for p in (
            await session.execute(select(Product).where(Product.shop_id == shop.id))
        ).scalars()
    }
    added = updated = 0
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        try:
            price = float((row.get("price") or "0").replace(",", "."))
        except ValueError:
            continue
        unit = (row.get("unit") or "шт").strip().lower()
        if unit not in VALID_UNITS:
            unit = "шт"
        description = (row.get("description") or "").strip() or None
        synonyms_raw = row.get("synonyms") or ""
        synonyms = (
            [s.strip() for s in synonyms_raw.replace(";", ",").split(",") if s.strip()]
            or None
        )

        found = existing.get(name.lower())
        if found is not None:
            # Обновляем позицию вместо дубля
            found.price = price
            found.unit = unit
            found.description = description
            if synonyms is not None:
                found.synonyms = synonyms
            updated += 1
        else:
            product = Product(
                shop_id=shop.id,
                name=name,
                price=price,
                unit=unit,
                description=description,
                synonyms=synonyms,
                is_active=True,
            )
            session.add(product)
            existing[name.lower()] = product
            added += 1
    await session.flush()
    return added, updated

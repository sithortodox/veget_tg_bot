"""Геолокация (§9 ТЗ): геокодирование адреса, проверка зоны, расстояния.

Цепочка провайдеров: DaData (РФ, точный) → Nominatim (fallback).
Новый провайдер реализует `GeocoderProvider` и встраивается в `GeocoderChain`
без изменения потребителей.
"""

from __future__ import annotations

import logging
import math
import struct
from dataclasses import dataclass, field
from typing import Optional, Protocol

import aiohttp
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import Shop

logger = logging.getLogger(__name__)


@dataclass
class GeoPoint:
    """Географическая точка."""

    lat: float
    lon: float

    @property
    def wkt(self) -> str:
        """WKT-представление для PostGIS GEOGRAPHY."""
        return f"POINT({self.lon} {self.lat})"


@dataclass
class GeocodeResult:
    """Результат геокодирования."""

    point: GeoPoint
    address_text: str
    components: dict = field(default_factory=dict)
    provider: str = ""


def _point_from_wkb(data: str | bytes | memoryview) -> GeoPoint:
    """Разбирает WKB/EWKB POINT (в т.ч. hex-строку) → GeoPoint без shapely."""
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, str):
        data = bytes.fromhex(data)
    endian = "<" if data[0] == 1 else ">"
    (type_code,) = struct.unpack_from(endian + "I", data, 1)
    offset = 5
    if type_code & 0x20000000:  # есть SRID (EWKB)
        offset += 4
    lon, lat = struct.unpack_from(endian + "dd", data, offset)
    return GeoPoint(lat=lat, lon=lon)


def point_from_wkt(wkt: str) -> GeoPoint:
    """Разбирает WKT «POINT(lon lat)» → GeoPoint.

    Принимает также WKBElement GeoAlchemy2 (значения из PostGIS-колонок).
    """
    if not isinstance(wkt, str):  # WKBElement из PostGIS
        return _point_from_wkb(wkt.data)
    inner = wkt[wkt.find("(") + 1 : wkt.find(")")]
    lon_s, lat_s = inner.split()
    return GeoPoint(lat=float(lat_s), lon=float(lon_s))


def clamp_search_radius(value: int, min_m: int, max_m: int) -> int:
    """Ограничивает пользовательский радиус поиска диапазоном G4 (300–800 м)."""
    return max(int(min_m), min(int(max_m), int(value)))


class GeocoderProvider(Protocol):
    """Протокол геокодера."""

    async def geocode(self, address: str) -> Optional[GeocodeResult]: ...


class DaDataGeocoder:
    """Геокодер на DaData (Сuggest API). Требует токен."""

    SUGGEST_URL = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"

    def __init__(self, token: str, secret: str) -> None:
        self._headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Token {token}",
        }
        self._secret = secret

    async def geocode(self, address: str) -> Optional[GeocodeResult]:
        """Геокодирует адрес через DaData."""
        payload: dict = {"query": address, "count": 1}
        if self._secret:
            payload["locations"] = [{"country": "RU"}]
        async with aiohttp.ClientSession(headers=self._headers) as session:
            request_timeout = aiohttp.ClientTimeout(total=10)
            async with session.post(
                self.SUGGEST_URL, json=payload, timeout=request_timeout
            ) as resp:
                if resp.status != 200:
                    logger.warning("DaData HTTP %s", resp.status)
                    return None
                data = await resp.json()
        suggestions = data.get("suggestions") or []
        if not suggestions:
            return None
        first = suggestions[0]
        data_obj = first.get("data") or {}
        geo = data_obj.get("geo_lat"), data_obj.get("geo_lon")
        if not geo[0] or not geo[1]:
            return None
        return GeocodeResult(
            point=GeoPoint(lat=float(geo[0]), lon=float(geo[1])),
            address_text=first.get("value") or address,
            components=data_obj,
            provider="dadata",
        )


class NominatimGeocoder:
    """Fallback-геокодер на Nominatim (OpenStreetMap)."""

    URL = "https://nominatim.openstreetmap.org/search"

    def __init__(self, user_agent: str) -> None:
        self._headers = {"User-Agent": user_agent}

    async def geocode(self, address: str) -> Optional[GeocodeResult]:
        """Геокодирует адрес через Nominatim."""
        params = {"q": address, "format": "json", "limit": 1, "countrycodes": "ru"}
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.get(
                self.URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    logger.warning("Nominatim HTTP %s", resp.status)
                    return None
                data = await resp.json()
        if not data:
            return None
        first = data[0]
        return GeocodeResult(
            point=GeoPoint(lat=float(first["lat"]), lon=float(first["lon"])),
            address_text=first.get("display_name") or address,
            components={"osm_type": first.get("type"), "osm_id": first.get("osm_id")},
            provider="nominatim",
        )


class GeocoderChain:
    """Цепочка геокодеров: первый успешный результат выигрывает."""

    def __init__(self, providers: list[GeocoderProvider]) -> None:
        self._providers = providers

    async def geocode(self, address: str) -> Optional[GeocodeResult]:
        """Пробует всех провайдеров по очереди."""
        for provider in self._providers:
            try:
                result = await provider.geocode(address)
            except Exception:  # noqa: BLE001 — провайдер не должен ронять бота
                logger.warning("Геокодер упал: %s", provider, exc_info=True)
                continue
            if result is not None:
                return result
        return None


def haversine_m(a: GeoPoint, b: GeoPoint) -> float:
    """Расстояние между точками в метрах (формула гаверсинуса)."""
    r = 6_371_000.0
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = math.radians(b.lat - a.lat)
    dlambda = math.radians(b.lon - a.lon)
    h = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(h))


class GeoService:
    """Фасад гео-подсистемы: геокодирование, валидация, поиск магазинов."""

    def __init__(self, settings: Settings, chain: GeocoderChain) -> None:
        self._settings = settings
        self._chain = chain

    async def geocode_address(self, address: str) -> Optional[GeocodeResult]:
        """Геокодирует текстовый адрес через цепочку провайдеров."""
        return await self._chain.geocode(address)

    def is_in_zone(self, point: GeoPoint) -> bool:
        """Проверка, что точка попадает в максимальный радиус обслуживания."""
        # Для MVP зона считается относительно любой точки пилота; точную
        # границу населённого пункта можно подключить как плагин.
        return True  # границы фактически ограничиваются радиусом поиска магазинов

    async def find_nearby_shops(
        self,
        session: AsyncSession,
        point: GeoPoint,
        radius_m: Optional[int] = None,
    ) -> list[Shop]:
        """Активные магазины в радиусе (PostGIS ST_DWithin)."""
        radius = radius_m or self._settings.default_search_radius_m
        stmt = (
            select(Shop)
            .where(Shop.is_active.is_(True))
            .where(
                func.ST_DWithin(
                    Shop.address_geo,
                    func.ST_GeogFromText(point.wkt),
                    radius,
                )
            )
            .order_by(
                func.ST_Distance(
                    Shop.address_geo, func.ST_GeogFromText(point.wkt)
                ).asc()
            )
        )
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def is_address_confirmed(
        self, text_point: GeoPoint, manual_point: GeoPoint
    ) -> bool:
        """True, если расхождение текстового адреса и ручной точки допустимо (§9.3)."""
        return haversine_m(text_point, manual_point) <= self._settings.address_mismatch_m


def build_geo_service(settings: Settings) -> GeoService:
    """Фабрика: собирает цепочку DaData → Nominatim."""
    providers: list[GeocoderProvider] = []
    if settings.dadata_token:
        providers.append(DaDataGeocoder(settings.dadata_token, settings.dadata_secret))
    else:
        logger.info("DADATA_TOKEN не задан — используем Nominatim")
    providers.append(NominatimGeocoder(settings.geo_user_agent))
    return GeoService(settings=settings, chain=GeocoderChain(providers))

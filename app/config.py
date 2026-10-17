"""Конфигурация приложения.

Все настройки читаются из окружения (.env). Таймауты соответствуют §11 ТЗ.
Значения можно переопределить без правки кода — это основа расширяемости.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Единый объект настроек, загружаемый из .env / окружения."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    bot_token: str = Field(alias="BOT_TOKEN")
    # ID админов через запятую; могут задаваться и кодом в admin.py
    admin_telegram_ids: str = Field(default="", alias="ADMIN_TELEGRAM_IDS")

    # --- БД и Redis ---
    database_url: str = Field(
        default="postgresql+asyncpg://ovoshnoy:ovoshnoy@localhost:5432/ovoshnoy",
        alias="DATABASE_URL",
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    # --- Геолокация ---
    dadata_token: str = Field(default="", alias="DADATA_TOKEN")
    dadata_secret: str = Field(default="", alias="DADATA_SECRET")
    default_search_radius_m: int = Field(default=800, alias="DEFAULT_SEARCH_RADIUS_M")
    min_search_radius_m: int = Field(default=300, alias="MIN_SEARCH_RADIUS_M")
    max_search_radius_m: int = Field(default=800, alias="MAX_SEARCH_RADIUS_M")
    delivery_radius_m: int = Field(default=800, alias="DELIVERY_RADIUS_M")
    delivery_cost: float = Field(default=100.0, alias="DELIVERY_COST")
    # Порог расхождения текстового адреса и ручной точки (§9.3)
    address_mismatch_m: int = Field(default=500, alias="ADDRESS_MISMATCH_M")
    geo_user_agent: str = Field(
        default="ovoshnoy-bot/1.0 (local market)", alias="GEO_USER_AGENT"
    )

    # --- Парсинг ---
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    llm_base_url: str = Field(default="", alias="LLM_BASE_URL")
    max_positions: int = Field(default=30, alias="MAX_POSITIONS")
    # Порог rapidfuzz для сопоставления с витриной (§8.3)
    match_threshold: int = Field(default=80, alias="MATCH_THRESHOLD")

    # --- Таймауты обычного заказа (секунды, §11.1) ---
    timeout_seller_confirm: int = Field(default=180, alias="TIMEOUT_SELLER_CONFIRM")
    timeout_client_approve: int = Field(default=120, alias="TIMEOUT_CLIENT_APPROVE")
    timeout_seller_assemble: int = Field(default=300, alias="TIMEOUT_SELLER_ASSEMBLE")
    timeout_client_pickup: int = Field(default=900, alias="TIMEOUT_CLIENT_PICKUP")
    timeout_courier_pickup: int = Field(default=300, alias="TIMEOUT_COURIER_PICKUP")
    timeout_courier_deliver: int = Field(default=900, alias="TIMEOUT_COURIER_DELIVER")
    timeout_client_payment: int = Field(default=300, alias="TIMEOUT_CLIENT_PAYMENT")

    # --- Таймауты предзаказа (часы, §11.2) ---
    preorder_reminder_hours: int = Field(default=12, alias="PREORDER_REMINDER_HOURS")
    preorder_seller_confirm_hours: int = Field(
        default=2, alias="PREORDER_SELLER_CONFIRM_HOURS"
    )
    preorder_search_other_hours: int = Field(
        default=1, alias="PREORDER_SEARCH_OTHER_HOURS"
    )
    preorder_client_confirm_hours: int = Field(
        default=1, alias="PREORDER_CLIENT_CONFIRM_HOURS"
    )

    debug: bool = Field(default=False, alias="DEBUG")

    @field_validator("admin_telegram_ids", mode="before")
    @classmethod
    def _strip_ids(cls, v: object) -> object:
        """Нормализуем список ID админов."""
        return str(v).strip() if v is not None else ""

    # --- Вычисляемые значения ---

    @property
    def admin_ids(self) -> list[int]:
        """Список telegram_id администраторов из конфига."""
        if not self.admin_telegram_ids:
            return []
        result: list[int] = []
        for part in self.admin_telegram_ids.split(","):
            part = part.strip()
            if part.isdigit():
                result.append(int(part))
        return result


@lru_cache
def get_settings() -> Settings:
    """Кэшированный доступ к настройкам (создаём объект один раз)."""
    return Settings()

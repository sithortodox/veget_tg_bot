"""Точка расширения: плагины подключаются без правки ядра.

Как добавить плагин:
1. Создайте модуль `app/plugins/my_feature.py` с функцией:

       async def register(ctx: PluginContext) -> None: ...

2. Добавьте имя модуля в список PLUGINS ниже.
3. В `register(ctx)` можно:
   - добавлять роутеры: ctx.dp.include_router(...)
   - регистрировать сервисы: ctx.services.register("name", obj)
   - ставить задачи планировщика: ctx.scheduler.schedule_once(...)
   - подписываться на события заказов, мутируя сервисы-обёртки

Ядро при этом не изменяется.
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

    from app.config import Settings
    from app.services.registry import ServiceRegistry
    from app.services.scheduler import SchedulerService

logger = logging.getLogger(__name__)

# Список плагинов (имена модулей в этом же пакете)
PLUGINS: list[str] = [
    # "example_plugin",
]


@dataclass
class PluginContext:
    """Контекст, передаваемый каждому плагину при старте."""

    bot: "Bot"
    dp: "Dispatcher"
    services: "ServiceRegistry"
    scheduler: "SchedulerService"
    settings: "Settings"
    session_factory: Any


async def load_plugins(ctx: PluginContext) -> None:
    """Загружает и инициализирует все перечисленные плагины."""
    for name in PLUGINS:
        try:
            module = importlib.import_module(f"app.plugins.{name}")
            register = getattr(module, "register", None)
            if register is None:
                logger.warning("Плагин %s без register(ctx) — пропущен", name)
                continue
            await register(ctx)
            logger.info("Плагин %s подключён", name)
        except Exception:  # noqa: BLE001 — ошибка плагина не должна блокировать бота
            logger.exception("Ошибка загрузки плагина %s", name)

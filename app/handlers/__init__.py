"""Пакет хендлеров.

`register_routers` подключает все роутеры к диспетчеру. Новый модуль
с экспортом `router` подключается автоматически — ядро не правится.
"""

from __future__ import annotations

import importlib
import pkgutil

from aiogram import Dispatcher

# Модули, подключаемые в строгом порядке (порядок влияет на приоритет фильтров)
CORE_MODULES = [
    "app.handlers.common",
    "app.handlers.client",
    "app.handlers.seller",
    "app.handlers.courier",
    "app.handlers.admin",
]


def register_routers(dp: Dispatcher) -> None:
    """Регистрирует все роутеры: сначала core, затем плагины-хендлеры из пакета."""
    for module_name in CORE_MODULES:
        module = importlib.import_module(module_name)
        router = getattr(module, "router", None)
        if router is not None:
            dp.include_router(router)

    # Автоподхват: любой новый модуль в пакете handlers с атрибутом router
    package = importlib.import_module("app.handlers")
    for _, name, _ in pkgutil.iter_modules(package.__path__):
        module_name = f"app.handlers.{name}"
        if module_name in CORE_MODULES:
            continue
        try:
            module = importlib.import_module(module_name)
        except Exception:  # noqa: BLE001 — сломанный плагин не должен ронять бота
            continue
        router = getattr(module, "router", None)
        if router is not None:
            dp.include_router(router)

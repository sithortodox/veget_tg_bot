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


def iter_routers():
    """Генерирует все роутеры: core-модули, затем плагины с атрибутом router.

    Источник правды для `register_routers` и тестов (перепривязка к новому
    диспетчеру в E2E-фикстуре).
    """
    package = importlib.import_module("app.handlers")
    names = list(CORE_MODULES)
    names.extend(
        f"app.handlers.{name}"
        for _, name, _ in pkgutil.iter_modules(package.__path__)
        if f"app.handlers.{name}" not in CORE_MODULES
    )

    seen: set[int] = set()
    for module_name in names:
        if module_name in CORE_MODULES:
            module = importlib.import_module(module_name)  # core обязателен
        else:
            try:
                module = importlib.import_module(module_name)
            except Exception:  # noqa: BLE001 — сломанный плагин не должен ронять бота
                continue
        router = getattr(module, "router", None)
        if router is None or id(router) in seen:
            continue
        seen.add(id(router))
        yield router


def register_routers(dp: Dispatcher) -> None:
    """Регистрирует все роутеры: сначала core, затем плагины-хендлеры из пакета."""
    for router in iter_routers():
        dp.include_router(router)

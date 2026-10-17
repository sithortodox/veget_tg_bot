"""Реестр сервисов — точка инъекции зависимостей.

Хендлеры и плагины получают сервисы из `data["services"]` (подставляется
middleware), не импортируя конкретные реализации напрямую. Это позволяет
заменять сервис на альтернативный (тест, новый провайдер) без правки ядра.
"""

from __future__ import annotations

from typing import Any


class ServiceRegistry:
    """Простой контейнер сервисов: имя → объект."""

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}

    def register(self, name: str, service: Any) -> None:
        """Регистрирует сервис. Плагины могут переопределять существующие."""
        self._services[name] = service

    def get(self, name: str) -> Any:
        """Возвращает сервис по имени. Ошибка = незарегистрированная зависимость."""
        try:
            return self._services[name]
        except KeyError as exc:
            raise KeyError(f"Сервис '{name}' не зарегистрирован") from exc

    def __getitem__(self, name: str) -> Any:
        return self.get(name)

    @property
    def all(self) -> dict[str, Any]:
        """Снимок всех сервисов (для плагинов)."""
        return dict(self._services)

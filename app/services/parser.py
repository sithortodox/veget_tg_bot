"""Парсер списка продуктов (§8 ТЗ).

Цепочка: regex (количество/единицы) → LLM (структура, синонимы, нормализация).
Если LLM недоступен — откат на regex-only. Расширяемо: новый провайдер LLM
реализует протокол `LLMProvider` и подключается в конфиге/реестре.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, Protocol

from app.config import Settings

logger = logging.getLogger(__name__)

# Regex: количество + единица. Поддерживает «5 кг», «3кг», «2 кило», «10 шт»,
# числительные словами («один банан», «два кг огурцов») и «полкило»
_QTY_RE = re.compile(
    r"(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>кг|килограмм\w*|кило|кг\.|г|грамм\w*|шт|штук\w*|"
    r"упаков\w*|пачек|пачка|пучк\w*|вилк\w*|ящик\w*|литр\w*|л\.?)",
    re.IGNORECASE,
)

# Нормализация единиц к каноничным значениям products.unit
_UNIT_MAP: dict[str, str] = {
    "кг": "кг",
    "кило": "кг",
    "килограмм": "кг",
    "килограмма": "кг",
    "килограммов": "кг",
    "г": "кг",  # граммы приводим к кг, дробное количество
    "грамм": "кг",
    "грамма": "кг",
    "граммов": "кг",
    "шт": "шт",
    "штука": "шт",
    "штуки": "шт",
    "штук": "шт",
    "упаковка": "упаковка",
    "упаковки": "упаковка",
    "упаковок": "упаковка",
    "пачка": "упаковка",
    "пачки": "упаковка",
    "пачек": "упаковка",
    "пучок": "пучок",
    "пучка": "пучок",
    "пучков": "пучок",
    "вилок": "вилок",
    "вилка": "вилок",
    "вилки": "вилок",
    "ящик": "ящик",
    "ящика": "ящик",
    "ящиков": "ящик",
    "литр": "л",
    "литра": "л",
    "литров": "л",
    "л": "л",
}

# Счётные числительные словами («один банан», «два кг огурцов», «пара яблок»)
_NUM_WORDS: dict[str, float] = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "одно": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
    "сто": 100,
    "пара": 2,
    "полтора": 1.5,
    "полторы": 1.5,
    "дюжина": 12,
}

# Альтернации от длинных к коротким — иначе «два» сработает внутри «двадцать»
_NUM_ALT = "|".join(sorted((re.escape(w) for w in _NUM_WORDS), key=len, reverse=True))
_UNIT_ALT = "|".join(sorted((re.escape(u) for u in _UNIT_MAP), key=len, reverse=True))

# Числительное словом + необязательная единица: «один банан», «два кг огурцов»;
# составные вроде «тридцать два» складываются, «кг/шт» потребляется как единица
_WORD_QTY_RE = re.compile(
    rf"\b(?P<num>{_NUM_ALT})\b\s*(?:\b(?P<num2>{_NUM_ALT})\b)?\s*"
    rf"(?:\b(?P<u>{_UNIT_ALT})\b)?",
    re.IGNORECASE,
)

# «полкило», «пол кг», «пол-килограмма» → 0.5 кг
_HALF_KG_RE = re.compile(
    r"\bпол\s*-?\s*(?:кило(?:грамм(?:а|ов)?)?|кг)\b",
    re.IGNORECASE,
)

_LLM_PROMPT = (
    "Ты парсер списка продуктов. Извлеки позиции из текста. "
    "Верни строго JSON-объект: "
    '{{"items": [{{"name": "...", "qty": число|null, "unit": "кг|шт|ящик|null"}}]}}. '
    "Названия нормализуй в начальную форму (мн. число). "
    "Текст: {user_input}"
)


@dataclass
class ParsedPosition:
    """Одна позиция, извлечённая из свободного текста."""

    raw: str  # исходное название
    qty: Optional[float] = None
    unit: Optional[str] = None
    normalized: Optional[str] = None  # нормализованное название (LLM/regex)


class LLMProvider(Protocol):
    """Протокол провайдера LLM — заменяемый без правки парсера."""

    async def complete_json(self, prompt: str) -> dict: ...


class OpenAIProvider:
    """Провайдер на OpenAI API (совместим с любым OpenAI-совместимым endpoint)."""

    def __init__(self, api_key: str, model: str, base_url: str = "") -> None:
        # Импорт ленивый: без ключа пакет openai не требуется
        from openai import AsyncOpenAI

        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = AsyncOpenAI(**kwargs)
        self._model = model

    async def complete_json(self, prompt: str) -> dict:
        """Запрос к LLM в JSON-режиме."""
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        content = response.choices[0].message.content or "{}"
        return json.loads(content)


class ProductParser:
    """Основной парсер: regex + опциональный LLM."""

    def __init__(self, settings: Settings, llm: Optional[LLMProvider] = None) -> None:
        self._settings = settings
        self._llm = llm

    async def parse(self, text: str) -> list[ParsedPosition]:
        """Разбирает текст списка в позиции.

        Raises:
            ValueError: пустой ввод или превышено макс. число позиций (§8.5).
        """
        cleaned = (text or "").strip()
        if not cleaned:
            raise ValueError("Пустой ввод")
        if len(cleaned) > 2000:
            raise ValueError("Слишком длинный текст списка")

        # 1) Regex-проход: извлекаем «название + количество» по запятым/переносам
        positions = self._regex_parse(cleaned)

        # 2) LLM-проход: нормализация названий и восстановление структуры
        if self._llm is not None:
            try:
                llm_positions = await self._llm_parse(cleaned)
                # LLM — источник истины по составу позиций, regex дополняет
                positions = self._merge(positions, llm_positions)
            except Exception:  # noqa: BLE001 — LLM не должен ронять заказ
                logger.warning("LLM-парсинг не удался, используем regex", exc_info=True)

        if not positions:
            raise ValueError("Не удалось распознать ни одной позиции")
        if len(positions) > self._settings.max_positions:
            raise ValueError(
                f"Больше {self._settings.max_positions} позиций — разбейте список на части"
            )
        return positions

    def _regex_parse(self, text: str) -> list[ParsedPosition]:
        """Грубый regex-разбор: куски через запятую/перенос, число и единица."""
        chunks = re.split(r"[,\n;]+", text)
        result: list[ParsedPosition] = []
        for chunk in chunks:
            chunk = chunk.strip().strip(".")
            if not chunk:
                continue
            qty, unit, name = self._extract_qty(chunk)
            result.append(ParsedPosition(raw=name, qty=qty, unit=unit, normalized=name))
        return result

    @staticmethod
    def _extract_qty(chunk: str) -> tuple[Optional[float], Optional[str], str]:
        """Разбирает количество/единицу в куске: цифры → полслова → словом.

        Возвращает (qty, unit, название); unit нормализован к products.unit,
        граммы приводятся к кг. Название — кусок без числительного/единицы.
        """
        m = _QTY_RE.search(chunk)
        if m:
            raw_qty = float(m.group("qty").replace(",", "."))
            raw_unit = m.group("unit").lower().rstrip(".")
            unit = _UNIT_MAP.get(raw_unit, raw_unit)
            # граммы → кг
            if unit == "кг" and raw_unit in {"г", "грамм", "грамма", "граммов"}:
                raw_qty = raw_qty / 1000
            name = (chunk[: m.start()] + chunk[m.end() :]).strip(" ,.-")
            return raw_qty, unit, name or chunk

        m = _HALF_KG_RE.search(chunk)
        if m:
            name = (chunk[: m.start()] + chunk[m.end() :]).strip(" ,.-")
            return 0.5, "кг", name or chunk

        m = _WORD_QTY_RE.search(chunk)
        if m:
            qty = _NUM_WORDS[m.group("num").lower()]
            if m.group("num2"):
                qty += _NUM_WORDS[m.group("num2").lower()]
            raw_unit = (m.group("u") or "").lower()
            unit = _UNIT_MAP.get(raw_unit) if raw_unit else None
            if unit == "кг" and raw_unit in {"г", "грамм", "грамма", "граммов"}:
                qty = qty / 1000
            name = (chunk[: m.start()] + chunk[m.end() :]).strip(" ,.-")
            return qty, unit, name or chunk

        return None, None, chunk

    async def _llm_parse(self, text: str) -> list[ParsedPosition]:
        """Запрос к LLM и разбор ответа в позиции."""
        assert self._llm is not None
        data = await self._llm.complete_json(_LLM_PROMPT.format(user_input=text))
        items = data.get("items") or data.get("positions") or []
        result: list[ParsedPosition] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            qty = item.get("qty")
            try:
                qty_f = float(qty) if qty is not None else None
            except (TypeError, ValueError):
                qty_f = None
            unit = item.get("unit")
            unit_s = str(unit).strip().lower() if unit else None
            if unit_s:
                unit_s = _UNIT_MAP.get(unit_s, unit_s)
            result.append(
                ParsedPosition(raw=name, qty=qty_f, unit=unit_s, normalized=name)
            )
        return result

    @staticmethod
    def _merge(
        regex_positions: list[ParsedPosition], llm_positions: list[ParsedPosition]
    ) -> list[ParsedPosition]:
        """Сливает результаты: берём позиции LLM, подставляем qty/unit из regex
        по близкому названию, если LLM их не дала."""
        if not llm_positions:
            return regex_positions
        merged: list[ParsedPosition] = []
        used_regex: set[int] = set()
        for llm_p in llm_positions:
            best_i: Optional[int] = None
            best_score = -1
            # Простое сопоставление по вхождению подстрок
            for i, rx_p in enumerate(regex_positions):
                if i in used_regex:
                    continue
                if (
                    llm_p.normalized
                    and rx_p.normalized
                    and (
                        llm_p.normalized.lower() in rx_p.normalized.lower()
                        or rx_p.normalized.lower() in llm_p.normalized.lower()
                    )
                ):
                    score = min(len(llm_p.normalized), len(rx_p.normalized))
                    if score > best_score:
                        best_score = score
                        best_i = i
            if best_i is not None:
                rx = regex_positions[best_i]
                used_regex.add(best_i)
                merged.append(
                    ParsedPosition(
                        raw=rx.raw,
                        qty=llm_p.qty if llm_p.qty is not None else rx.qty,
                        unit=llm_p.unit or rx.unit,
                        normalized=llm_p.normalized or rx.normalized,
                    )
                )
            else:
                merged.append(llm_p)
        # Позиции из regex, которые LLM не упомянула, добавляем в конец
        for i, rx_p in enumerate(regex_positions):
            if i not in used_regex:
                merged.append(rx_p)
        return merged


def build_parser(settings: Settings) -> ProductParser:
    """Фабрика парсера: подключает LLM только при наличии ключа."""
    llm: Optional[LLMProvider] = None
    if settings.openai_api_key:
        llm = OpenAIProvider(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            base_url=settings.llm_base_url,
        )
    else:
        logger.info("OPENAI_API_KEY не задан — работает regex-only парсинг")
    return ProductParser(settings=settings, llm=llm)

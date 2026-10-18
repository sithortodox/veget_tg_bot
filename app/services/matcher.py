"""Матчер позиций запроса с витринами магазинов (§8.3 ТЗ).

Использует rapidfuzz: для каждой позиции ищем лучший товар среди активных
карточек магазинов в радиусе. Порог — из конфига (по умолчанию 80%).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from rapidfuzz import fuzz, process

from app.db.models import Product, Shop
from app.services.parser import ParsedPosition

_VOWELS = "ыиаеуюьйоёя"


def _stem(word: str) -> str:
    """Грубый стеммер: снимает типовые окончания, чтобы сверять ед./мн. число.

    «огурец»/«огурцы» → «огурц», «яблоко»/«яблоки» → «яблок»,
    «помидоров» → «помидор». Порог входа: слово длиннее 3 букв.
    """
    w = word.lower().replace("ё", "е")
    if len(w) > 4 and w.endswith("ец"):
        w = w[:-2] + "ц"
    if len(w) > 5 and w.endswith(("ов", "ев")):
        w = w[:-2]
    while len(w) > 3 and w[-1] in _VOWELS:
        w = w[:-1]
    return w


def _stem_text(text: str) -> str:
    """Стемминг всего названия (пословно)."""
    return " ".join(_stem(w) for w in text.split())


@dataclass
class MatchResult:
    """Результат сопоставления одной позиции с карточкой товара."""

    position: ParsedPosition
    product: Optional[Product]
    shop: Optional[Shop]
    score: float  # 0..100


class ProductMatcher:
    """Fuzzy-матчер по названиям и синонимам товаров."""

    def __init__(self, threshold: int = 80) -> None:
        self._threshold = threshold

    def match(
        self,
        positions: list[ParsedPosition],
        products: list[tuple[Product, Shop]],
    ) -> list[MatchResult]:
        """Сопоставляет каждую позицию с лучшим товаром из переданного набора.

        `products` — пары (товар, магазин), обычно уже отфильтрованные по радиусу.
        """
        if not products:
            return [
                MatchResult(position=p, product=None, shop=None, score=0.0)
                for p in positions
            ]

        # Словарь «ключ поиска → (product, shop)»; ключ включает синонимы
        # и стеммы (чтобы «огурец» находил «Огурцы» и наоборот)
        choices: dict[str, tuple[Product, Shop]] = {}
        for product, shop in products:
            keys = [product.name]
            if product.synonyms:
                keys.extend(product.synonyms)
            for key in keys:
                lk = key.lower()
                choices[lk] = (product, shop)
                sk = _stem_text(lk)
                if sk and sk not in choices:
                    choices[sk] = (product, shop)

        keys_list = list(choices.keys())
        results: list[MatchResult] = []
        for position in positions:
            query = (position.normalized or position.raw or "").lower().strip()
            if not query:
                results.append(
                    MatchResult(position=position, product=None, shop=None, score=0.0)
                )
                continue
            best = process.extractOne(
                query,
                keys_list,
                scorer=fuzz.WRatio,
                score_cutoff=self._threshold,
            )
            if best is None:
                # Запасной проход: тот же запрос по стеммам (ед./мн. число,
                # падежные окончания) — если «огурец» не нашёлся в «огурцы»
                stemmed = _stem_text(query)
                if stemmed and stemmed != query:
                    best = process.extractOne(
                        stemmed,
                        keys_list,
                        scorer=fuzz.WRatio,
                        score_cutoff=self._threshold,
                    )
            if best is None:
                results.append(
                    MatchResult(position=position, product=None, shop=None, score=0.0)
                )
                continue
            key, score, _ = best
            product, shop = choices[key]
            results.append(
                MatchResult(position=position, product=product, shop=shop, score=score)
            )
        return results

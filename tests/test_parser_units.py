"""Числительные словами, ед./мн. число в матче и уточнение единиц (§8).

Жалобы пользователей:
- «один банан» показывался как «1 кг» (слово не парсилось, unit брался из каталога);
- «один огурец» не находился в витрине «Огурцы» (score 75 < порога 80).
"""

from __future__ import annotations

from app.config import get_settings
from app.db.models import Product, Shop
from app.services.matcher import ProductMatcher, _stem_text
from app.services.parser import ParsedPosition, build_parser
from tests.conftest import make_product, make_shop, make_user
from tests.services_fixture import ServiceStack

# ======================================================================
# 1. Парсер: числительные словами
# ======================================================================


async def test_parse_word_numeral_strips_name() -> None:
    """«один банан» → название «банан», qty=1, единица не угадана."""
    parser = build_parser(get_settings())
    (p,) = await parser.parse("один банан")
    assert p.raw == "банан"
    assert p.qty == 1
    assert p.unit is None


async def test_parse_compound_word_numeral() -> None:
    """«тридцать два огурца» → qty=32 (сумма), а не 30 с именем «два огурца»."""
    parser = build_parser(get_settings())
    (p,) = await parser.parse("тридцать два огурца")
    assert p.raw == "огурца"
    assert p.qty == 32
    assert p.unit is None


async def test_parse_word_numeral_with_unit() -> None:
    """«два кг огурцов» → qty=2, unit=кг — единица потребляется."""
    parser = build_parser(get_settings())
    (p,) = await parser.parse("два кг огурцов")
    assert p.raw == "огурцов"
    assert p.qty == 2
    assert p.unit == "кг"


async def test_parse_half_kilo() -> None:
    """«полкило моркови» → 0.5 кг."""
    parser = build_parser(get_settings())
    (p,) = await parser.parse("полкило моркови")
    assert p.raw == "моркови"
    assert p.qty == 0.5
    assert p.unit == "кг"


async def test_parse_word_numeral_sht() -> None:
    """«две штуки огурцов» → qty=2, unit=шт."""
    parser = build_parser(get_settings())
    (p,) = await parser.parse("две штуки огурцов")
    assert p.raw == "огурцов"
    assert p.qty == 2
    assert p.unit == "шт"


async def test_parse_digits_still_work() -> None:
    """Цифровые количества не задеты: «помидоры 5 кг, банан»."""
    parser = build_parser(get_settings())
    positions = await parser.parse("помидоры 5 кг, банан")
    assert [(p.raw, p.qty, p.unit) for p in positions] == [
        ("помидоры", 5.0, "кг"),
        ("банан", None, None),
    ]


async def test_parse_no_false_numeral_inside_word() -> None:
    """«бородинский» содержит «один», но \b не даёт сработать."""
    parser = build_parser(get_settings())
    (p,) = await parser.parse("хлеб бородинский")
    assert p.raw == "хлеб бородинский"
    assert p.qty is None


# ======================================================================
# 2. Матчер: ед./мн. число и падежные окончания
# ======================================================================


def _shop() -> Shop:
    return Shop(
        telegram_id=1,
        name="Эко-ферма",
        address_text="г. Краснодар, ул. Ленина, 1",
        address_geo="POINT(38.9763 45.0443)",
        phone="+79991112233",
        payment_details="карта",
        is_active=True,
        moderation_status="approved",
    )


def _catalog() -> list[tuple[Product, Shop]]:
    shop = _shop()
    names = ["Огурцы", "Яблоки", "Помидоры", "Бананы", "Картофель", "Лук репчатый"]
    return [
        (
            Product(
                shop_id=1,
                name=n,
                price=100,
                unit="кг",
                synonyms=None,
                is_active=True,
            ),
            shop,
        )
        for n in names
    ]


def _match_one(query: str, catalog: list[tuple[Product, Shop]]):
    matcher = ProductMatcher(threshold=80)
    position = ParsedPosition(raw=query, normalized=query)
    return matcher.match([position], catalog)[0]


def test_match_singular_against_plural() -> None:
    """«огурец»/«яблоко»/«помидор» должны находить формы мн. числа."""
    catalog = _catalog()
    assert _match_one("огурец", catalog).product.name == "Огурцы"
    assert _match_one("яблоко", catalog).product.name == "Яблоки"
    assert _match_one("помидор", catalog).product.name == "Помидоры"


def test_match_genitive_case() -> None:
    """«картофеля» (род. падеж) → «Картофель»."""
    catalog = _catalog()
    assert _match_one("картофеля", catalog).product.name == "Картофель"


def test_match_raw_word_numeral_via_stem() -> None:
    """Даже без чистки парсера «один огурец» матчится через стем-ключ."""
    catalog = _catalog()
    result = _match_one("один огурец", catalog)
    assert result.product is not None
    assert result.product.name == "Огурцы"


def test_match_unknown_position_not_forced() -> None:
    """Чужая позиция по-прежнему не падает ниже порога."""
    catalog = _catalog()
    assert _match_one("зелёный лук", catalog).product is None


def test_match_nearest_shop_on_tie() -> None:
    """При одинаковых названиях побеждает ближайший магазин (§3.2 C5)."""
    near = Shop(
        id=1,
        telegram_id=11,
        name="Рядом",
        address_text="x",
        address_geo="POINT(0 0)",
        phone="1",
        payment_details="1",
        is_active=True,
        moderation_status="approved",
    )
    far = Shop(
        id=2,
        telegram_id=22,
        name="Далеко",
        address_text="x",
        address_geo="POINT(0 0)",
        phone="2",
        payment_details="2",
        is_active=True,
        moderation_status="approved",
    )
    p_far = Product(shop_id=2, name="Капуста", price=50, unit="кг", synonyms=None, is_active=True)
    p_near = Product(shop_id=1, name="Капуста", price=55, unit="кг", synonyms=None, is_active=True)
    position = ParsedPosition(raw="капуста", normalized="капуста")
    matcher = ProductMatcher(threshold=80)

    # Без дистанций — как раньше, первый кандидат из витрины
    (r0,) = matcher.match([position], [(p_far, far), (p_near, near)])
    assert r0.shop.id == 2

    # С дистанциями — ближайший, независимо от порядка витрины
    (r1,) = matcher.match(
        [position],
        [(p_far, far), (p_near, near)],
        distances={1: 100.0, 2: 500.0},
    )
    assert r1.shop.id == 1
    assert r1.score >= 80


def test_stem_collapses_forms() -> None:
    """Стеммер сводит ед./мн. число и род. падеж к общему корню."""
    assert _stem_text("огурец") == _stem_text("огурцы") == "огурц"
    assert _stem_text("яблоко") == _stem_text("яблоки") == "яблок"
    assert _stem_text("помидоров") == _stem_text("помидор") == "помидор"


# ======================================================================
# 3. Уточнение единиц: счётная форма весового товара (§8)
# ======================================================================


async def test_clarify_count_form_for_weight_product(
    db, stack: ServiceStack
) -> None:
    """«один банан» при товаре в кг → просим уточнить, заказ не создаётся."""
    client = await make_user(db, telegram_id=8101)
    shop = await make_shop(db, telegram_id=8111)
    await make_product(db, shop, name="Бананы", price=115, unit="кг")
    await db.commit()

    order, text = await stack.orders.create_from_text(db, client, "один банан")
    assert order is None
    assert "Уточните единицы" in text
    assert "банан" in text


async def test_clarify_compound_count_form(db, stack: ServiceStack) -> None:
    """«тридцать два огурца» — тоже уточнение, а не 32 кг."""
    client = await make_user(db, telegram_id=8201)
    shop = await make_shop(db, telegram_id=8211)
    await make_product(db, shop, name="Огурцы", price=150, unit="кг")
    await db.commit()

    order, text = await stack.orders.create_from_text(
        db, client, "тридцать два огурца"
    )
    assert order is None
    assert "Уточните единицы" in text


async def test_explicit_weight_unit_no_clarify(db, stack: ServiceStack) -> None:
    """«бананы 0.5 кг» — единица указана, заказ создаётся сразу."""
    client = await make_user(db, telegram_id=8301)
    shop = await make_shop(db, telegram_id=8311)
    await make_product(db, shop, name="Бананы", price=115, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "бананы 0.5 кг")
    assert order is not None
    item = order.sub_orders[0].items[0]
    assert item.qty == 0.5
    assert item.unit == "кг"


async def test_piece_product_count_form_ok(db, stack: ServiceStack) -> None:
    """Товар с unit=шт: счётная форма без уточнения, единица из каталога."""
    client = await make_user(db, telegram_id=8401)
    shop = await make_shop(db, telegram_id=8411)
    await make_product(db, shop, name="Бананы", price=15, unit="шт")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "один банан")
    assert order is not None
    item = order.sub_orders[0].items[0]
    assert item.qty == 1
    assert item.unit == "шт"
    assert item.raw_name == "банан"


async def test_bare_name_defaults_to_product_unit(db, stack: ServiceStack) -> None:
    """Без единицы и количества («помидоры») — 1 единица товара, без вопроса (§8.2)."""
    client = await make_user(db, telegram_id=8501)
    shop = await make_shop(db, telegram_id=8511)
    await make_product(db, shop, name="Помидоры", price=120, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "помидоры")
    assert order is not None
    item = order.sub_orders[0].items[0]
    assert item.qty == 1
    assert item.unit == "кг"


# ======================================================================
# 4. Заказ уходит ближайшему магазину (§3.2 C5)
# ======================================================================


async def test_order_goes_to_nearest_shop(db, stack: ServiceStack) -> None:
    """Из двух магазинов с одинаковым товаром позицию получает ближайший.

    Ближний создаётся первым (id меньше) — до фикса «последний в витрине»
    побеждал бы дальний, и продавец рядом с покупателем заказ не получал.
    """
    client = await make_user(db, telegram_id=8601)
    near = await make_shop(db, telegram_id=8611, name="Ближний")
    far = await make_shop(
        db, telegram_id=8612, name="Дальний", lat=45.0463, lon=38.9783
    )
    await make_product(db, near, name="Капуста", price=55, unit="кг")
    await make_product(db, far, name="Капуста", price=50, unit="кг")
    await db.commit()

    order, _ = await stack.orders.create_from_text(db, client, "капуста 2 кг")
    assert order is not None
    assert len(order.sub_orders) == 1
    assert order.sub_orders[0].shop.name == "Ближний"

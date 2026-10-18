"""Э9 (S3): редактирование карточки товара — цена, описание, фото, скрыть/показать."""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Product
from tests.conftest import make_product, make_shop, make_user
from tests.test_e2e import E2EDriver


async def _seller_with_product(e2e: E2EDriver) -> tuple[int, int]:
    """Продавец «У Теста» (tg=2101) с товаром «Капуста» 50 ₽/кг."""
    db = e2e.db
    await make_user(db, telegram_id=1101)
    shop = await make_shop(db, telegram_id=2101, name="У Теста")
    await make_product(db, shop, name="Капуста", price=50, unit="кг")
    await db.commit()
    product = (await db.execute(select(Product))).scalars().one()
    return shop.telegram_id, product.id


async def test_s3_edit_price(e2e: E2EDriver) -> None:
    """S3: продавец меняет цену через карточку → цена в БД обновилась."""
    shop_tg, product_id = await _seller_with_product(e2e)

    await e2e.cb(shop_tg, f"sp:price:{product_id}")
    card = e2e.sent_edits(shop_tg)[-1]
    assert "текущая цена 50" in card

    await e2e.text(shop_tg, "65")
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert float(product.price) == 65.0
    assert any("Цена обновлена" in t for t in e2e.sent_texts(shop_tg))


async def test_s3_edit_price_invalid(e2e: E2EDriver) -> None:
    """S3: не-число в цене → ошибка, цена не меняется, состояние живо."""
    shop_tg, product_id = await _seller_with_product(e2e)

    await e2e.cb(shop_tg, f"sp:price:{product_id}")
    await e2e.text(shop_tg, "дорого")
    assert any("Нужно число" in t for t in e2e.sent_texts(shop_tg))
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert float(product.price) == 50.0

    # Повторная попытка в том же состоянии проходит
    await e2e.text(shop_tg, "55")
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert float(product.price) == 55.0


async def test_s3_edit_description(e2e: E2EDriver) -> None:
    """S3: описание меняется, «-» убирает его."""
    shop_tg, product_id = await _seller_with_product(e2e)

    await e2e.cb(shop_tg, f"sp:desc:{product_id}")
    await e2e.text(shop_tg, "свежая из грядки")
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert product.description == "свежая из грядки"

    await e2e.cb(shop_tg, f"sp:desc:{product_id}")
    await e2e.text(shop_tg, "-")
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert product.description is None


async def test_s3_hide_and_show(e2e: E2EDriver) -> None:
    """S3: скрыть → is_active False, показать → True."""
    shop_tg, product_id = await _seller_with_product(e2e)

    await e2e.cb(shop_tg, f"sp:hide:{product_id}")
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert product.is_active is False
    assert "скрыт" in e2e.sent_edits(shop_tg)[-1]

    await e2e.cb(shop_tg, f"sp:show:{product_id}")
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert product.is_active is True


async def test_s3_catalog_lists_with_buttons(e2e: E2EDriver) -> None:
    """S3: «Моя витрина» — список с кнопками на каждую позицию."""
    from app.db.models import Shop

    shop_tg, product_id = await _seller_with_product(e2e)
    db = e2e.db
    shop = (
        (await db.execute(select(Shop).where(Shop.telegram_id == shop_tg)))
        .scalars()
        .one()
    )
    await make_product(db, shop, name="Морковь", price=40, unit="кг")
    await db.commit()

    await e2e.text(shop_tg, "🥕 Моя витрина")
    catalog = e2e.sent_texts(shop_tg)[-1]
    assert "Капуста" in catalog
    assert "Морковь" in catalog
    assert f"[id:{product_id}]" in catalog

    kb = e2e.api.calls[-1].reply_markup
    assert any(
        b.callback_data == f"sp:card:{product_id}"
        for row in kb.inline_keyboard
        for b in row
    )


async def test_s3_ownership_guard(e2e: E2EDriver) -> None:
    """S3: чужой продавец не может изменить карточку."""
    _shop_tg, product_id = await _seller_with_product(e2e)
    db = e2e.db
    await make_shop(db, telegram_id=2202, name="Чужой")
    await db.commit()

    await e2e.cb(2202, f"sp:price:{product_id}")
    e2e.db.expire_all()
    product = await e2e.db.get(Product, product_id)
    assert product is not None
    assert float(product.price) == 50.0
    # FSM не должен включиться — карточка не открылась
    assert not any("текущая цена" in t for t in e2e.sent_texts(2202))

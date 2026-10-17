"""FSM-состояния продавца."""

from aiogram.fsm.state import State, StatesGroup


class SellerReg(StatesGroup):
    """Регистрация магазина (S1)."""

    name = State()
    address = State()
    phone = State()
    payment_details = State()


class SellerProduct(StatesGroup):
    """Создание карточки товара (S2)."""

    name = State()
    price = State()
    unit = State()
    description = State()
    photo = State()  # прикрепить фото карточки или «-»


class SellerAssemble(StatesGroup):
    """Фактическое количество при сборке (§5.1)."""

    entering = State()  # ввод факта по очереди для каждой позиции


class SellerReject(StatesGroup):
    """Причина отмены своей части заказа (S6)."""

    reason = State()

"""FSM-состояния покупателя."""

from aiogram.fsm.state import State, StatesGroup


class ClientReg(StatesGroup):
    """Регистрация покупателя (C1)."""

    phone = State()
    address = State()
    geo = State()


class ClientOrder(StatesGroup):
    """Оформление заказа."""

    waiting_list = State()  # ждём текстовый список (C2)
    choose_time = State()  # сейчас / на дату (C3)
    entering_date = State()  # ввод даты предзаказа
    editing = State()  # редактирование позиций (C7)
    choosing_delivery = State()  # самовывоз / доставка (C9)
    choosing_payment = State()  # постоплата (C10)


class ClientProfile(StatesGroup):
    """Редактирование профиля."""

    address = State()
    radius = State()

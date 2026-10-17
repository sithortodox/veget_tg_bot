"""FSM-состояния курьера (фаза 2, MVP — минимальная регистрация)."""

from aiogram.fsm.state import State, StatesGroup


class CourierReg(StatesGroup):
    """Регистрация курьера (K1)."""

    name = State()
    phone = State()

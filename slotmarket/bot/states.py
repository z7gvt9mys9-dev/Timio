"""Состояния диалогов бота."""

from aiogram.fsm import state


class CalcStates(state.StatesGroup):
    """Шаги калькулятора потерь (B4.1), по одному на вопрос."""

    seats = state.State()
    hours_per_day = state.State()
    days_per_month = state.State()
    load_pct = state.State()
    revenue_per_hour = state.State()
    variable_cost = state.State()
    weak_slots = state.State()


class BusinessStates(state.StatesGroup):
    """Шаги регистрации бизнеса (B3.1, пункт 3)."""

    name = state.State()
    category = state.State()
    district = state.State()
    address = state.State()
    phone = state.State()


class ServiceStates(state.StatesGroup):
    """Шаги добавления услуги (B3.1, пункт 4)."""

    title = state.State()
    duration_min = state.State()
    regular_price = state.State()


class ServiceEditStates(state.StatesGroup):
    """Правка одного поля услуги (B4.2)."""

    value = state.State()


class NewSlotStates(state.StatesGroup):
    """Шаги создания слота (B3.1, пункт 6)."""

    service = state.State()
    day = state.State()
    time = state.State()
    min_price = state.State()
    hide_name = state.State()
    confirm = state.State()


class BookingStates(state.StatesGroup):
    """Ожидание телефона перед первой бронью (B3.2, пункт 2)."""

    phone = state.State()

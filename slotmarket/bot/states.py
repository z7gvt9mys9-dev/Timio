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

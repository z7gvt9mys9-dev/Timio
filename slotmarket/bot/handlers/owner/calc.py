"""Калькулятор потерь: диалог из семи вопросов (B4.1)."""

import aiogram
from aiogram import filters
from aiogram import types
from aiogram.fsm import context
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import keyboards
from bot import states
from bot.handlers.owner import shared
from core import businesses
from core import calculator
from core import feed

router = aiogram.Router()

_NUMERIC_STEPS = (
    (states.CalcStates.seats, calculator.CalcField.SEATS),
    (states.CalcStates.hours_per_day, calculator.CalcField.HOURS_PER_DAY),
    (states.CalcStates.days_per_month, calculator.CalcField.DAYS_PER_MONTH),
    (states.CalcStates.load_pct, calculator.CalcField.LOAD_PCT),
    (
        states.CalcStates.revenue_per_hour,
        calculator.CalcField.REVENUE_PER_HOUR,
    ),
    (states.CalcStates.variable_cost, calculator.CalcField.VARIABLE_COST),
)
_QUESTIONS = {
    calculator.CalcField.SEATS:
        "Сколько у вас рабочих мест (кресел, залов)?\nПример: 3",
    calculator.CalcField.HOURS_PER_DAY:
        "Сколько часов в день вы работаете?\nПример: 10",
    calculator.CalcField.DAYS_PER_MONTH:
        "Сколько дней в месяц вы работаете?\nПример: 26",
    calculator.CalcField.LOAD_PCT:
        "Какая у вас средняя загрузка, %?\nПример: 60",
    calculator.CalcField.REVENUE_PER_HOUR:
        "Какая средняя выручка за час, ₽?\nПример: 1500",
    calculator.CalcField.VARIABLE_COST:
        "Какие переменные расходы на час, ₽ "
        "(материалы, доля мастера)?\nПример: 600",
}
_INTRO_TEXT = "Посчитаем, сколько вы теряете на пустых часах.\n\n"
_WEAK_SLOTS_QUESTION = (
    "Какие дни и время у вас самые пустые?\nПример: вт, чт до 14:00\n\n"
    f"Если не знаете, нажмите «{keyboards.SKIP_TEXT}»."
)
_REGISTER_HINT_TEXT = (
    "Чтобы вернуть эти деньги, зарегистрируйте бизнес: /business"
)


async def start_calc(
    message: types.Message, state: context.FSMContext, user_id: int
) -> None:
    """Сбрасывает текущий диалог и начинает калькулятор с первого вопроса.

    Args:
        message: Сообщение, в чат которого задаётся вопрос.
        state: Состояние диалога.
        user_id: Пользователь, которому сохранится расчёт.
    """
    await state.clear()
    first_state, first_field = _NUMERIC_STEPS[0]
    await state.set_state(first_state)
    await state.update_data({shared.USER_ID_KEY: user_id})
    await message.answer(
        _INTRO_TEXT + _QUESTIONS[first_field],
        reply_markup=types.ReplyKeyboardRemove(),
    )


@router.message(
    filters.StateFilter(*(step for step, _ in _NUMERIC_STEPS)),
    shared.ANSWER_FILTER,
)
async def handle_numeric_answer(
    message: types.Message, state: context.FSMContext
) -> None:
    """Принимает числовой ответ и задаёт следующий вопрос.

    Args:
        message: Ответ пользователя.
        state: Состояние диалога.
    """
    step_index = _find_step_index(await state.get_state())
    field = _NUMERIC_STEPS[step_index][1]
    try:
        value = calculator.parse_field(field, message.text)
    except ValueError as error:
        await message.answer(f"{error}\n\n{_QUESTIONS[field]}")
        return
    await state.update_data({field.value: value})
    await _ask_next_question(message, state, step_index + 1)


@router.message(
    filters.StateFilter(states.CalcStates.weak_slots), shared.ANSWER_FILTER
)
async def handle_weak_slots(
    message: types.Message,
    state: context.FSMContext,
    session_factory: sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession],
) -> None:
    """Принимает слабые слоты, считает и показывает результат.

    Args:
        message: Ответ пользователя.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    is_skipped = message.text == keyboards.SKIP_TEXT
    weak_slots = None if is_skipped else message.text.strip()
    answers = await state.get_data()
    await state.clear()
    user_id = answers.pop(shared.USER_ID_KEY)
    calc_input = calculator.CalcInput(**answers, weak_slots=weak_slots)
    result = calculator.calculate(calc_input)
    async with session_factory() as db_session:
        await calculator.save_report(db_session, user_id, calc_input, result)
        business = await businesses.find_owner_business(db_session, user_id)
    text = _format_result(calc_input, result)
    if business is None:
        text += f"\n\n{_REGISTER_HINT_TEXT}"
    await message.answer(text, reply_markup=types.ReplyKeyboardRemove())


def _find_step_index(current_state: str | None) -> int:
    for index, (step, _) in enumerate(_NUMERIC_STEPS):
        if step.state == current_state:
            return index
    raise LookupError(f"Неизвестный шаг калькулятора: {current_state}")


async def _ask_next_question(
    message: types.Message, state: context.FSMContext, step_index: int
) -> None:
    if step_index < len(_NUMERIC_STEPS):
        next_state, next_field = _NUMERIC_STEPS[step_index]
        await state.set_state(next_state)
        await message.answer(_QUESTIONS[next_field])
        return
    await state.set_state(states.CalcStates.weak_slots)
    await message.answer(
        _WEAK_SLOTS_QUESTION, reply_markup=keyboards.build_skip_keyboard()
    )


def _format_result(
    calc_input: calculator.CalcInput, result: calculator.CalcResult
) -> str:
    sales_pct = calculator.SALES_SHARE * 100
    discount_pct = (1 - calculator.DISCOUNT_PRICE_SHARE) * 100
    lines = [
        "Ваши потери от простоев в месяц",
        "",
        f"Пустых часов: {round(result.empty_hours)}",
        f"Упущенная выручка: {feed.format_rubles(result.lost_revenue)}",
        f"Реально вернуть: {feed.format_rubles(result.recoverable)} "
        f"(если продать {sales_pct:.0f}% пустых часов "
        f"со скидкой {discount_pct:.0f}%)",
    ]
    if result.is_discount_unprofitable:
        lines.append(
            "Цена со скидкой ниже переменных расходов, поэтому продажа "
            "со скидкой убыточна и возврат равен 0 ₽."
        )
    lines.append(
        "Рекомендуемая минимальная цена часа: "
        f"{feed.format_rubles(result.min_price.price)}"
    )
    if result.min_price.is_below_floor:
        lines.append(
            "Это стартовая цена слота (выручка за час минус 15%). "
            "Такая цена не покрывает "
            "расходы с минимальной прибылью 10%."
        )
    if calc_input.weak_slots:
        lines.append(f"Самые слабые слоты: {calc_input.weak_slots}")
    return "\n".join(lines)

"""Калькулятор потерь от простоев (B4.1).

Формулы существуют только здесь: бот и сайт вызывают этот модуль.
"""

import dataclasses
import decimal
import enum

from sqlalchemy.ext import asyncio as sa_asyncio

from core import pricing
from db import models

# Допущение B4.1: какую долю пустых часов удастся продать.
# Заменить реальной цифрой после теста (B15, вопрос 5).
SALES_SHARE = decimal.Decimal("0.25")
# Цена со скидкой как доля выручки за час (B4.1).
DISCOUNT_PRICE_SHARE = decimal.Decimal("0.65")
# В калькуляторе нет услуги, поэтому цена считается за один час (B4.1).
CALC_DURATION_MIN = 60
EVENT_CALC_COMPLETED = "calc_completed"
_PERCENT = 100


class CalcField(enum.StrEnum):
    """Числовые поля калькулятора в порядке вопросов."""

    SEATS = "seats"
    HOURS_PER_DAY = "hours_per_day"
    DAYS_PER_MONTH = "days_per_month"
    LOAD_PCT = "load_pct"
    REVENUE_PER_HOUR = "revenue_per_hour"
    VARIABLE_COST = "variable_cost"


@dataclasses.dataclass(frozen=True)
class _FieldRule:
    min_value: int
    max_value: int | None
    error_text: str


_FIELD_RULES = {
    CalcField.SEATS: _FieldRule(
        1, None, "Число мест — целое число от 1. Пример: 3"
    ),
    CalcField.HOURS_PER_DAY: _FieldRule(
        1, 24, "Часов в день — целое число от 1 до 24. Пример: 10"
    ),
    CalcField.DAYS_PER_MONTH: _FieldRule(
        1, 31, "Дней в месяц — целое число от 1 до 31. Пример: 26"
    ),
    CalcField.LOAD_PCT: _FieldRule(
        0, 100, "Загрузка — целое число от 0 до 100. Пример: 60"
    ),
    CalcField.REVENUE_PER_HOUR: _FieldRule(
        1, None, "Выручка за час — целое число рублей от 1. Пример: 1500"
    ),
    CalcField.VARIABLE_COST: _FieldRule(
        0, None, "Расходы на час — целое число рублей от 0. Пример: 600"
    ),
}


@dataclasses.dataclass(frozen=True)
class CalcInput:
    """Входные данные калькулятора. Деньги — целые рубли."""

    seats: int
    hours_per_day: int
    days_per_month: int
    load_pct: int
    revenue_per_hour: int
    variable_cost: int
    weak_slots: str | None = None


@dataclasses.dataclass(frozen=True)
class CalcResult:
    """Результат калькулятора.

    Attributes:
        empty_hours: Пустые часы в месяц, без округления.
        lost_revenue: Упущенная выручка в месяц, ₽.
        recoverable: Реальный возврат в месяц, ₽. Не меньше 0.
        is_discount_unprofitable: Цена со скидкой не покрывает
            переменные расходы, поэтому возврат обнулён.
        min_price: Рекомендуемая минимальная цена за час.
    """

    empty_hours: decimal.Decimal
    lost_revenue: int
    recoverable: int
    is_discount_unprofitable: bool
    min_price: pricing.MinPriceRecommendation


def validate_field(field: CalcField, value: int) -> None:
    """Проверяет одно числовое поле.

    Args:
        field: Какое поле проверяется.
        value: Введённое значение.

    Raises:
        ValueError: Значение вне допустимого диапазона. Текст ошибки
            готов для показа пользователю и содержит пример.
    """
    rule = _FIELD_RULES[field]
    is_too_small = value < rule.min_value
    is_too_large = rule.max_value is not None and value > rule.max_value
    if is_too_small or is_too_large:
        raise ValueError(rule.error_text)


def parse_field(field: CalcField, raw_text: str) -> int:
    """Разбирает и проверяет ответ пользователя на одно поле.

    Args:
        field: Какое поле вводится.
        raw_text: Текст ответа.

    Returns:
        Целое значение поля.

    Raises:
        ValueError: Текст не число или вне диапазона. Текст ошибки
            готов для показа пользователю и содержит пример.
    """
    try:
        value = int(raw_text.strip())
    except ValueError as error:
        raise ValueError(_FIELD_RULES[field].error_text) from error
    validate_field(field, value)
    return value


def validate_input(calc_input: CalcInput) -> None:
    """Проверяет все числовые поля калькулятора.

    Args:
        calc_input: Входные данные.

    Raises:
        ValueError: Первое поле с неверным значением.
    """
    for field in CalcField:
        validate_field(field, getattr(calc_input, field.value))


def calculate(calc_input: CalcInput) -> CalcResult:
    """Считает потери и возврат по формулам B4.1.

    Args:
        calc_input: Проверенные входные данные.

    Returns:
        Результат расчёта.
    """
    available_hours = (
        calc_input.seats
        * calc_input.hours_per_day
        * calc_input.days_per_month
    )
    empty_share = 1 - decimal.Decimal(calc_input.load_pct) / _PERCENT
    empty_hours = available_hours * empty_share
    lost_revenue = empty_hours * calc_input.revenue_per_hour
    discount_price = calc_input.revenue_per_hour * DISCOUNT_PRICE_SHARE
    margin = discount_price - calc_input.variable_cost
    recoverable = empty_hours * SALES_SHARE * margin
    return CalcResult(
        empty_hours=empty_hours,
        lost_revenue=_to_rubles(lost_revenue),
        recoverable=max(_to_rubles(recoverable), 0),
        is_discount_unprofitable=margin < 0,
        min_price=pricing.recommend_min_price(
            calc_input.variable_cost,
            CALC_DURATION_MIN,
            calc_input.revenue_per_hour,
        ),
    )


async def save_report(
    db_session: sa_asyncio.AsyncSession,
    user_id: int,
    calc_input: CalcInput,
    result: CalcResult,
) -> models.CalcReport:
    """Сохраняет расчёт из бота и событие для метрик одной транзакцией.

    Args:
        db_session: Сессия базы.
        user_id: Пользователь, который прошёл калькулятор.
        calc_input: Входные данные.
        result: Результат расчёта.

    Returns:
        Сохранённый отчёт.
    """
    report = models.CalcReport(
        user_id=user_id,
        source=models.CalcSource.BOT,
        lost_revenue=result.lost_revenue,
        recoverable=result.recoverable,
        **dataclasses.asdict(calc_input),
    )
    db_session.add(report)
    await db_session.flush()
    db_session.add(
        models.Event(
            user_id=user_id,
            type=EVENT_CALC_COMPLETED,
            payload={"report_id": report.id},
        )
    )
    await db_session.commit()
    return report


def _to_rubles(amount: decimal.Decimal) -> int:
    return int(amount.to_integral_value(decimal.ROUND_HALF_UP))

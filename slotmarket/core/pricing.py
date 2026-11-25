"""Расчёт цен слотов (B4.3, B4.4)."""

import dataclasses
import datetime
import decimal

# Минимальная прибыль сверх переменных расходов, чтобы продажа не была
# «в ноль».
MIN_PROFIT_SHARE = decimal.Decimal("0.10")
# Границы минимальной цены относительно обычной (B4.3).
MIN_PRICE_LOWER_SHARE = decimal.Decimal("0.30")
MIN_PRICE_UPPER_SHARE = decimal.Decimal("0.90")
# Ставка комиссии только для справочного расчёта в отчёте (B4.8).
# В пол минимальной цены не входит, пока комиссия не взимается.
COMMISSION_SHARE = decimal.Decimal("0.15")
PRICE_STEP = 50
# Снижение цены (B4.4).
START_DISCOUNT_SHARE = decimal.Decimal("0.15")
FINAL_PRICE_PERIOD = datetime.timedelta(hours=2)
LATE_PUBLICATION_PERIOD = datetime.timedelta(hours=2, minutes=30)
PRICE_STEP_INTERVAL = datetime.timedelta(minutes=30)
_MINUTES_PER_HOUR = 60


@dataclasses.dataclass(frozen=True)
class MinPriceRecommendation:
    """Рекомендуемая минимальная цена слота.

    Attributes:
        price: Цена в целых рублях.
        is_below_floor: Цена упёрлась в стартовую цену слота и ниже
            пола «расходы + прибыль». Владельца нужно предупредить.
    """

    price: int
    is_below_floor: bool


def recommend_min_price(
    variable_cost_per_hour: int, duration_min: int, regular_price: int
) -> MinPriceRecommendation:
    """Считает рекомендуемую минимальную цену слота по формуле B4.3.

    Args:
        variable_cost_per_hour: Переменные расходы на час, ₽.
        duration_min: Длительность услуги, минуты.
        regular_price: Обычная цена услуги, ₽.

    Returns:
        Рекомендуемая цена и флаг предупреждения.
    """
    variable_cost = calc_variable_cost(variable_cost_per_hour, duration_min)
    floor_price = variable_cost * (1 + MIN_PROFIT_SHARE)
    rounded_price = _round_up_to_step(floor_price)
    price_range = calc_min_price_range(regular_price)
    price = min(max(rounded_price, price_range.lower), price_range.upper)
    return MinPriceRecommendation(
        price=price, is_below_floor=price < floor_price
    )


@dataclasses.dataclass(frozen=True)
class PriceRange:
    """Допустимый диапазон минимальной цены слота, ₽, включительно."""

    lower: int
    upper: int


def calc_min_price_range(regular_price: int) -> PriceRange:
    """Считает, какой может быть минимальная цена слота (B4.3).

    Не меньше 30% обычной, не больше 90% обычной и не выше стартовой.

    Args:
        regular_price: Обычная цена услуги, ₽.

    Returns:
        Диапазон в целых рублях.
    """
    lower = _to_rubles(
        regular_price * MIN_PRICE_LOWER_SHARE, decimal.ROUND_CEILING
    )
    upper = min(
        _to_rubles(regular_price * MIN_PRICE_UPPER_SHARE, decimal.ROUND_FLOOR),
        calc_start_price(regular_price),
    )
    return PriceRange(lower=lower, upper=upper)


def calc_variable_cost(
    variable_cost_per_hour: int, duration_min: int
) -> decimal.Decimal:
    """Считает переменные расходы на одну услугу.

    Args:
        variable_cost_per_hour: Переменные расходы на час из калькулятора.
        duration_min: Длительность услуги, минуты.

    Returns:
        Расходы в рублях, без округления.
    """
    return (
        decimal.Decimal(variable_cost_per_hour)
        * duration_min
        / _MINUTES_PER_HOUR
    )


def is_below_variable_cost(
    min_price: int, variable_cost_per_hour: int, duration_min: int
) -> bool:
    """Проверяет, ниже ли минимальная цена расходов на услугу (B4.3).

    Args:
        min_price: Минимальная цена слота, ₽.
        variable_cost_per_hour: Переменные расходы на час из калькулятора.
        duration_min: Длительность услуги, минуты.

    Returns:
        True, если владельца нужно предупредить.
    """
    return min_price < calc_variable_cost(
        variable_cost_per_hour, duration_min
    )


def _round_up_to_step(amount: decimal.Decimal) -> int:
    steps = (amount / PRICE_STEP).to_integral_value(decimal.ROUND_CEILING)
    return int(steps) * PRICE_STEP


def _to_rubles(amount: decimal.Decimal, rounding: str) -> int:
    return int(amount.to_integral_value(rounding))


def calc_start_price(regular_price: int) -> int:
    """Считает стартовую цену слота: обычная минус 15% (B4.4).

    Округляется вниз до 50 ₽, чтобы скидка была не меньше 15%.

    Args:
        regular_price: Обычная цена услуги, ₽.

    Returns:
        Стартовая цена, ₽.
    """
    discounted = regular_price * (1 - START_DISCOUNT_SHARE)
    steps = (discounted / PRICE_STEP).to_integral_value(decimal.ROUND_FLOOR)
    return int(steps) * PRICE_STEP


def calc_current_price(
    start_price: int,
    min_price: int,
    starts_at: datetime.datetime,
    published_at: datetime.datetime,
    now: datetime.datetime,
) -> int:
    """Считает текущую цену слота по формуле B4.4.

    Цена не хранит историю: она каждый раз считается заново из времени,
    поэтому после перезапуска бота пересчёт даёт тот же результат (B9).

    Args:
        start_price: Стартовая цена, ₽.
        min_price: Минимальная цена, ₽.
        starts_at: Начало слота, tz-aware.
        published_at: Время публикации, tz-aware.
        now: Текущее время, tz-aware.

    Returns:
        Цена в целых рублях, от min_price до start_price.
    """
    initial_lead = starts_at - published_at
    if initial_lead < LATE_PUBLICATION_PERIOD:
        return min_price
    # Цена меняется ступенями: время с публикации округляем вниз до
    # шага, значит время до начала — вверх.
    steps_passed = (now - published_at) // PRICE_STEP_INTERVAL
    stepped_now = published_at + steps_passed * PRICE_STEP_INTERVAL
    current_lead = starts_at - stepped_now
    share = decimal.Decimal(
        (current_lead - FINAL_PRICE_PERIOD) // datetime.timedelta(seconds=1)
    ) / decimal.Decimal(
        (initial_lead - FINAL_PRICE_PERIOD) // datetime.timedelta(seconds=1)
    )
    share = min(max(share, decimal.Decimal(0)), decimal.Decimal(1))
    raw_price = min_price + (start_price - min_price) * share
    steps = (raw_price / PRICE_STEP).to_integral_value(decimal.ROUND_HALF_UP)
    return min(max(int(steps) * PRICE_STEP, min_price), start_price)

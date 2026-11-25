"""Расчёт цен слотов (B4.3, B4.4)."""

import dataclasses
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
_MINUTES_PER_HOUR = 60


@dataclasses.dataclass(frozen=True)
class MinPriceRecommendation:
    """Рекомендуемая минимальная цена слота.

    Attributes:
        price: Цена в целых рублях.
        is_below_floor: Цена упёрлась в 90% обычной и ниже пола
            «расходы + прибыль». Владельца нужно предупредить.
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
    variable_cost = (
        decimal.Decimal(variable_cost_per_hour)
        * duration_min
        / _MINUTES_PER_HOUR
    )
    floor_price = variable_cost * (1 + MIN_PROFIT_SHARE)
    rounded_price = _round_up_to_step(floor_price)
    lower_bound = _to_rubles(
        regular_price * MIN_PRICE_LOWER_SHARE, decimal.ROUND_CEILING
    )
    upper_bound = _to_rubles(
        regular_price * MIN_PRICE_UPPER_SHARE, decimal.ROUND_FLOOR
    )
    price = min(max(rounded_price, lower_bound), upper_bound)
    return MinPriceRecommendation(
        price=price, is_below_floor=price < floor_price
    )


def _round_up_to_step(amount: decimal.Decimal) -> int:
    steps = (amount / PRICE_STEP).to_integral_value(decimal.ROUND_CEILING)
    return int(steps) * PRICE_STEP


def _to_rubles(amount: decimal.Decimal, rounding: str) -> int:
    return int(amount.to_integral_value(rounding))

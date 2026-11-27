"""Тесты цен слотов: минимальная (B4.3), стартовая и текущая (B4.4)."""

import datetime

import pytest

from core import pricing

_STARTS_AT = datetime.datetime(2026, 12, 1, 15, 0, tzinfo=datetime.UTC)
# Публикация за 26 ч: от публикации до финальных 2 ч ровно 24 ч.
_PUBLISHED_AT = _STARTS_AT - datetime.timedelta(hours=26)
_START_PRICE = 1250
_MIN_PRICE = 750


def test_recommend_min_price_b41_example_returns_700() -> None:
    recommendation = pricing.recommend_min_price(600, 60, 1500, 1250)
    assert recommendation.price == 700


def test_recommend_min_price_b41_example_has_no_warning() -> None:
    recommendation = pricing.recommend_min_price(600, 60, 1500, 1250)
    assert not recommendation.is_below_floor


def test_recommend_min_price_low_costs_clamped_to_30_percent() -> None:
    recommendation = pricing.recommend_min_price(100, 60, 1500, 1250)
    assert recommendation.price == 450


def test_recommend_min_price_high_costs_clamped_to_start_price() -> None:
    recommendation = pricing.recommend_min_price(1400, 60, 1500, 1250)
    assert recommendation.price == 1250


def test_recommend_min_price_high_costs_sets_warning() -> None:
    recommendation = pricing.recommend_min_price(1400, 60, 1500, 1250)
    assert recommendation.is_below_floor


def test_recommend_min_price_duration_scales_costs() -> None:
    # 600 ₽/ч × 90 мин = 900, плюс 10% = 990, вверх до 50 = 1000.
    recommendation = pricing.recommend_min_price(600, 90, 2000, 1700)
    assert recommendation.price == 1000


def test_recommend_min_price_clamped_to_owner_start_price() -> None:
    # 1400 + 10% = 1540 → 1550, но стартовая цена владельца 1500.
    recommendation = pricing.recommend_min_price(1400, 60, 1500, 1500)
    assert recommendation.price == 1500


def _price_after(
    elapsed: datetime.timedelta,
    min_price: int = _MIN_PRICE,
    published_at: datetime.datetime = _PUBLISHED_AT,
) -> int:
    return pricing.calc_current_price(
        _START_PRICE, min_price, _STARTS_AT, published_at,
        published_at + elapsed,
    )


@pytest.mark.parametrize(
    ("regular_price", "start_price"), [(1500, 1250), (1000, 850), (100, 50)]
)
def test_calc_start_price_rounds_down_to_50(
    regular_price: int, start_price: int
) -> None:
    assert pricing.calc_start_price(regular_price) == start_price


def test_calc_current_price_at_publication_equals_start() -> None:
    assert _price_after(datetime.timedelta(0)) == _START_PRICE


def test_calc_current_price_middle_is_halfway() -> None:
    # Через 12 ч из 24: доля 0,5 → 750 + 500 × 0,5 = 1000.
    assert _price_after(datetime.timedelta(hours=12)) == 1000


def test_calc_current_price_inside_step_unchanged() -> None:
    assert _price_after(datetime.timedelta(minutes=29)) == _START_PRICE


def test_calc_current_price_rounds_up_to_nearest_50() -> None:
    # 2 ступени: 1250 − 500 × 1/24 = 1229,17 → 1250.
    assert _price_after(datetime.timedelta(hours=1)) == 1250


def test_calc_current_price_rounds_down_to_nearest_50() -> None:
    # 3 ступени: 1250 − 500 × 1,5/24 = 1218,75 → 1200.
    assert _price_after(datetime.timedelta(hours=1, minutes=30)) == 1200


def test_calc_current_price_exactly_2_hours_before_is_min() -> None:
    assert _price_after(datetime.timedelta(hours=24)) == _MIN_PRICE


def test_calc_current_price_last_2_hours_stays_min() -> None:
    assert _price_after(datetime.timedelta(hours=25)) == _MIN_PRICE


def test_calc_current_price_late_publication_is_min() -> None:
    published_at = _STARTS_AT - datetime.timedelta(hours=2, minutes=20)
    price = _price_after(datetime.timedelta(0), published_at=published_at)
    assert price == _MIN_PRICE


def test_calc_current_price_publication_at_2_5_hours_starts_high() -> None:
    published_at = _STARTS_AT - datetime.timedelta(hours=2, minutes=30)
    price = _price_after(datetime.timedelta(0), published_at=published_at)
    assert price == _START_PRICE


def test_calc_current_price_rounded_below_min_clamped_to_min() -> None:
    # Последняя ступень до финала: 760 + 490 × 0,5/24 = 770,2 → 750,
    # но минимум владельца 760.
    price = _price_after(datetime.timedelta(hours=23, minutes=30), 760)
    assert price == 760


def test_calc_current_price_never_above_start() -> None:
    # Часы сервера отстали: «сейчас» раньше публикации.
    assert _price_after(-datetime.timedelta(hours=1)) == _START_PRICE


def test_calc_min_price_range_upper_is_start_price() -> None:
    range_ = pricing.calc_min_price_range(1500, 1250)
    assert range_ == pricing.PriceRange(450, 1250)


def test_calc_min_price_range_owner_start_above_old_90_percent() -> None:
    range_ = pricing.calc_min_price_range(1500, 1500)
    assert range_ == pricing.PriceRange(450, 1500)


def test_calc_min_price_range_rounds_lower_up() -> None:
    # 30% от 1499 = 449,7 → 450.
    assert pricing.calc_min_price_range(1499, 1250).lower == 450


def test_calc_start_price_range_from_30_percent_to_regular() -> None:
    range_ = pricing.calc_start_price_range(1500)
    assert range_ == pricing.PriceRange(450, 1500)


def test_calc_current_price_falls_from_owner_start_price() -> None:
    # Середина пути от 1450 до 750: 1100.
    price = pricing.calc_current_price(
        1450, 750, _STARTS_AT, _PUBLISHED_AT,
        _PUBLISHED_AT + datetime.timedelta(hours=12),
    )
    assert price == 1100


def test_is_below_variable_cost_below_returns_true() -> None:
    # 600 ₽/ч × 90 мин = 900 ₽ расходов.
    assert pricing.is_below_variable_cost(850, 600, 90)


def test_is_below_variable_cost_equal_returns_false() -> None:
    assert not pricing.is_below_variable_cost(900, 600, 90)

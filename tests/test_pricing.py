"""Тесты рекомендуемой минимальной цены (B4.3)."""

from core import pricing


def test_recommend_min_price_b41_example_returns_700() -> None:
    recommendation = pricing.recommend_min_price(600, 60, 1500)
    assert recommendation.price == 700


def test_recommend_min_price_b41_example_has_no_warning() -> None:
    recommendation = pricing.recommend_min_price(600, 60, 1500)
    assert not recommendation.is_below_floor


def test_recommend_min_price_low_costs_clamped_to_30_percent() -> None:
    recommendation = pricing.recommend_min_price(100, 60, 1500)
    assert recommendation.price == 450


def test_recommend_min_price_high_costs_clamped_to_90_percent() -> None:
    recommendation = pricing.recommend_min_price(1400, 60, 1500)
    assert recommendation.price == 1350


def test_recommend_min_price_high_costs_sets_warning() -> None:
    recommendation = pricing.recommend_min_price(1400, 60, 1500)
    assert recommendation.is_below_floor


def test_recommend_min_price_duration_scales_costs() -> None:
    # 600 ₽/ч × 90 мин = 900, плюс 10% = 990, вверх до 50 = 1000.
    recommendation = pricing.recommend_min_price(600, 90, 2000)
    assert recommendation.price == 1000

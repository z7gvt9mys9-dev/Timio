"""Тесты калькулятора потерь (B4.1)."""

import collections.abc
import decimal
import pathlib

import pytest
import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from core import calculator
from db import models
from db import session

_B41_EXAMPLE = calculator.CalcInput(
    seats=3,
    hours_per_day=10,
    days_per_month=26,
    load_pct=60,
    revenue_per_hour=1500,
    variable_cost=600,
    weak_slots="вт, чт до 14:00",
)


def _example_with(**changes: int) -> calculator.CalcInput:
    fields = {
        field: getattr(_B41_EXAMPLE, field)
        for field in calculator.CalcInput.__dataclass_fields__
    }
    fields.update(changes)
    return calculator.CalcInput(**fields)


def test_calculate_b41_example_empty_hours_312() -> None:
    result = calculator.calculate(_B41_EXAMPLE)
    assert result.empty_hours == 312


def test_calculate_b41_example_lost_revenue_468000() -> None:
    result = calculator.calculate(_B41_EXAMPLE)
    assert result.lost_revenue == 468_000


def test_calculate_b41_example_recoverable_29250() -> None:
    result = calculator.calculate(_B41_EXAMPLE)
    assert result.recoverable == 29_250


def test_calculate_b41_example_min_price_700() -> None:
    result = calculator.calculate(_B41_EXAMPLE)
    assert result.min_price.price == 700


def test_calculate_full_load_no_losses() -> None:
    result = calculator.calculate(_example_with(load_pct=100))
    assert result.lost_revenue == 0


def test_calculate_fractional_hours_rounded_to_rubles() -> None:
    # 1 × 1 × 1 × 33% пустых = 0,33 ч; 0,33 × 1001 = 330,33 ₽.
    result = calculator.calculate(
        _example_with(
            seats=1, hours_per_day=1, days_per_month=1, load_pct=67,
            revenue_per_hour=1001,
        )
    )
    assert result.lost_revenue == 330


def test_calculate_discount_below_costs_recoverable_zero() -> None:
    # Цена со скидкой 975 ₽ ниже расходов 1000 ₽.
    result = calculator.calculate(_example_with(variable_cost=1000))
    assert result.recoverable == 0


def test_calculate_discount_below_costs_sets_unprofitable() -> None:
    result = calculator.calculate(_example_with(variable_cost=1000))
    assert result.is_discount_unprofitable


def test_calculate_b41_example_discount_profitable() -> None:
    result = calculator.calculate(_B41_EXAMPLE)
    assert not result.is_discount_unprofitable


def test_validate_field_load_above_100_raises() -> None:
    with pytest.raises(ValueError, match="Пример: 60"):
        calculator.validate_field(calculator.CalcField.LOAD_PCT, 101)


def test_validate_field_zero_seats_raises() -> None:
    with pytest.raises(ValueError, match="Пример: 3"):
        calculator.validate_field(calculator.CalcField.SEATS, 0)


def test_validate_field_zero_variable_cost_accepted() -> None:
    calculator.validate_field(calculator.CalcField.VARIABLE_COST, 0)


def test_validate_input_b41_example_accepted() -> None:
    calculator.validate_input(_B41_EXAMPLE)


def test_validate_input_25_hours_per_day_raises() -> None:
    with pytest.raises(ValueError, match="от 1 до 24"):
        calculator.validate_input(_example_with(hours_per_day=25))


def test_parse_field_number_with_spaces_returns_int() -> None:
    value = calculator.parse_field(calculator.CalcField.SEATS, " 3 ")
    assert value == 3


def test_parse_field_not_a_number_raises_with_example() -> None:
    with pytest.raises(ValueError, match="Пример: 1500"):
        calculator.parse_field(
            calculator.CalcField.REVENUE_PER_HOUR, "полторы тысячи"
        )


def test_parse_field_out_of_range_raises_with_example() -> None:
    with pytest.raises(ValueError, match="Пример: 60"):
        calculator.parse_field(calculator.CalcField.LOAD_PCT, "120")


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[
    sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _save_example(
    session_factory: sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession],
) -> int:
    async with session_factory() as db_session:
        user = models.User(tg_id=1, name="Иван")
        db_session.add(user)
        await db_session.flush()
        result = calculator.calculate(_B41_EXAMPLE)
        await calculator.save_report(
            db_session, user.id, _B41_EXAMPLE, result
        )
        return user.id


async def test_save_report_stores_bot_report_with_results(
    session_factory: sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession],
) -> None:
    await _save_example(session_factory)
    async with session_factory() as db_session:
        report = await db_session.scalar(
            sqlalchemy.select(models.CalcReport)
        )
    assert (report.source, report.lost_revenue, report.recoverable) == (
        models.CalcSource.BOT, 468_000, 29_250,
    )


async def test_save_report_writes_calc_completed_event(
    session_factory: sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession],
) -> None:
    await _save_example(session_factory)
    async with session_factory() as db_session:
        report = await db_session.scalar(
            sqlalchemy.select(models.CalcReport)
        )
        event = await db_session.scalar(sqlalchemy.select(models.Event))
    assert (event.type, event.payload) == (
        models.EventType.CALC_COMPLETED, {"report_id": report.id},
    )



async def test_find_latest_variable_cost_no_reports_returns_none(
    session_factory: sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession],
) -> None:
    async with session_factory() as db_session:
        cost = await calculator.find_latest_variable_cost(db_session, 1)
    assert cost is None


async def test_find_latest_variable_cost_returns_last_report(
    session_factory: sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession],
) -> None:
    user_id = await _save_example(session_factory)
    newer_input = _example_with(variable_cost=700)
    async with session_factory() as db_session:
        await calculator.save_report(
            db_session, user_id, newer_input,
            calculator.calculate(newer_input),
        )
        cost = await calculator.find_latest_variable_cost(db_session, user_id)
    assert cost == 700

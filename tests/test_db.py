"""Тесты подключения к базе и модели данных."""

import collections.abc
import datetime
import pathlib

import pytest
import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from db import models
from db import session

_MOSCOW_TZ = datetime.timezone(datetime.timedelta(hours=3))


@pytest.fixture
async def engine(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[sa_asyncio.AsyncEngine]:
    db_engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(db_engine)
    yield db_engine
    await db_engine.dispose()


async def _add_slot(
    engine: sa_asyncio.AsyncEngine, starts_at: datetime.datetime
) -> int:
    session_factory = session.create_session_factory(engine)
    async with session_factory() as db_session:
        user = models.User(tg_id=1, name="Иван")
        db_session.add(user)
        await db_session.flush()
        business = models.Business(
            owner_id=user.id, name="Борода", category="барбершоп",
            district="Центр", address="ул. Ленина, 1", phone="+70000000000",
        )
        db_session.add(business)
        await db_session.flush()
        service = models.Service(
            business_id=business.id, title="Стрижка",
            duration_min=60, regular_price=1500,
        )
        db_session.add(service)
        await db_session.flush()
        slot = models.Slot(
            service_id=service.id, starts_at=starts_at, start_price=1275,
            min_price=900, current_price=1275,
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


async def test_create_engine_new_database_uses_wal(
    engine: sa_asyncio.AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        mode = await connection.scalar(sqlalchemy.text("PRAGMA journal_mode"))
    assert mode == "wal"


async def test_create_engine_new_database_enforces_foreign_keys(
    engine: sa_asyncio.AsyncEngine,
) -> None:
    async with engine.connect() as connection:
        enabled = await connection.scalar(
            sqlalchemy.text("PRAGMA foreign_keys")
        )
    assert enabled == 1


async def test_slot_status_stored_as_lowercase_value(
    engine: sa_asyncio.AsyncEngine,
) -> None:
    await _add_slot(engine, datetime.datetime.now(datetime.UTC))
    async with engine.connect() as connection:
        raw_status = await connection.scalar(
            sqlalchemy.text("SELECT status FROM slots")
        )
    assert raw_status == "open"


async def test_utc_datetime_aware_value_read_back_as_utc(
    engine: sa_asyncio.AsyncEngine,
) -> None:
    moscow_time = datetime.datetime(2026, 12, 1, 15, 0, tzinfo=_MOSCOW_TZ)
    slot_id = await _add_slot(engine, moscow_time)
    session_factory = session.create_session_factory(engine)
    async with session_factory() as db_session:
        slot = await db_session.get(models.Slot, slot_id)
    assert slot.starts_at == datetime.datetime(
        2026, 12, 1, 12, 0, tzinfo=datetime.UTC
    )


async def test_utc_datetime_naive_value_raises(
    engine: sa_asyncio.AsyncEngine,
) -> None:
    with pytest.raises(sqlalchemy.exc.StatementError):
        await _add_slot(engine, datetime.datetime(2026, 12, 1, 15, 0))

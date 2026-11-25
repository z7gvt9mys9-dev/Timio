"""Тесты регистрации пользователей (B5)."""

import collections.abc
import pathlib

import pytest
import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def test_register_user_new_tg_id_sets_consent_at(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 1, "Иван")
    assert user.consent_at is not None


async def test_register_user_new_tg_id_is_not_owner(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 1, "Иван")
    assert not user.is_owner


async def test_register_user_twice_returns_same_user(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        first = await users.register_user(db_session, 1, "Иван")
        second = await users.register_user(db_session, 1, "Иван")
    assert first.id == second.id


async def test_register_user_twice_creates_one_row(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        await users.register_user(db_session, 1, "Иван")
        await users.register_user(db_session, 1, "Иван")
        count = await db_session.scalar(
            sqlalchemy.select(sqlalchemy.func.count(models.User.id))
        )
    assert count == 1


async def test_register_user_twice_keeps_first_consent_at(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        first = await users.register_user(db_session, 1, "Иван")
        first_consent_at = first.consent_at
    async with session_factory() as db_session:
        second = await users.register_user(db_session, 1, "Иван")
    assert second.consent_at == first_consent_at


async def test_find_user_unknown_tg_id_returns_none(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        user = await users.find_user(db_session, 999)
    assert user is None


async def test_find_user_registered_tg_id_returns_user(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        registered = await users.register_user(db_session, 1, "Иван")
    async with session_factory() as db_session:
        found = await users.find_user(db_session, 1)
    assert found.id == registered.id


async def test_mark_owner_sets_is_owner(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 1, "Иван")
        await users.mark_owner(db_session, user.id)
    async with session_factory() as db_session:
        found = await users.find_user(db_session, 1)
    assert found.is_owner


async def test_set_phone_contact_number_normalized(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 1, "Иван")
        await users.set_phone(db_session, user.id, "79001234567")
    async with session_factory() as db_session:
        found = await users.find_user(db_session, 1)
    assert found.phone == "+7(900)123-45-67"


async def test_set_phone_foreign_number_stored_as_plus_digits(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 1, "Иван")
        await users.set_phone(db_session, user.id, "380501234567")
    async with session_factory() as db_session:
        found = await users.find_user(db_session, 1)
    assert found.phone == "+380501234567"


async def test_set_phone_not_a_phone_raises_and_keeps_empty(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 1, "Иван")
        with pytest.raises(ValueError, match="Пример"):
            await users.set_phone(db_session, user.id, "12345")
    async with session_factory() as db_session:
        found = await users.find_user(db_session, 1)
    assert found.phone is None

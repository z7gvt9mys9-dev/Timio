"""Тесты регистрации пользователей (B5)."""

import collections.abc
import datetime
import pathlib

import pytest
import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from core import businesses
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


_NOW = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.UTC)
_BLOCKED_UNTIL = datetime.datetime(2026, 12, 20, 9, 0, tzinfo=datetime.UTC)


async def _register_buyer(session_factory: _SessionFactory) -> models.User:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 1, "Иван")
        await users.set_phone(db_session, user.id, "79001234567")
    return user


async def _add_booking(
    session_factory: _SessionFactory,
    buyer_id: int,
    starts_at: datetime.datetime,
    status: models.BookingStatus,
) -> int:
    async with session_factory() as db_session:
        owner = await users.register_user(db_session, 2, "Владелец")
        business = await businesses.create_business(
            db_session, owner.id,
            businesses.BusinessInput(
                "Борода", "барбершоп", "Центр", "ул. Ленина, 1",
                "+7(900)765-43-21",
            ),
        )
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Стрижка", 60, 1500),
        )
        slot = models.Slot(
            service_id=service.id, starts_at=starts_at, start_price=1250,
            min_price=750, current_price=900,
            status=models.SlotStatus.BOOKED, published_at=_NOW,
        )
        db_session.add(slot)
        await db_session.flush()
        booking = models.Booking(
            slot_id=slot.id, buyer_id=buyer_id, price=900, status=status
        )
        db_session.add(booking)
        await db_session.commit()
        return booking.id


async def _load_user(
    session_factory: _SessionFactory, user_id: int
) -> models.User:
    async with session_factory() as db_session:
        return await db_session.get(models.User, user_id)


async def test_delete_personal_data_no_bookings_returns_deleted(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    async with session_factory() as db_session:
        outcome = await users.delete_personal_data(db_session, user.id, _NOW)
    assert outcome == users.DeleteOutcome.DELETED


async def test_delete_personal_data_clears_name_phone_and_consent(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, user.id, _NOW)
    deleted = await _load_user(session_factory, user.id)
    assert (deleted.name, deleted.phone, deleted.consent_at) == (
        users.DELETED_NAME, None, None,
    )


async def test_delete_personal_data_find_user_returns_none(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, user.id, _NOW)
    async with session_factory() as db_session:
        found = await users.find_user(db_session, 1)
    assert found is None


async def test_delete_personal_data_past_booking_keeps_price_and_status(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    booking_id = await _add_booking(
        session_factory, user.id, _NOW - datetime.timedelta(days=1),
        models.BookingStatus.DONE,
    )
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, user.id, _NOW)
    async with session_factory() as db_session:
        booking = await db_session.get(models.Booking, booking_id)
    assert (booking.buyer_id, booking.price, booking.status) == (
        user.id, 900, models.BookingStatus.DONE,
    )


async def test_delete_personal_data_upcoming_booking_refused(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    await _add_booking(
        session_factory, user.id, _NOW + datetime.timedelta(hours=3),
        models.BookingStatus.ACTIVE,
    )
    async with session_factory() as db_session:
        outcome = await users.delete_personal_data(db_session, user.id, _NOW)
    assert outcome == users.DeleteOutcome.HAS_UPCOMING_BOOKINGS


async def test_delete_personal_data_upcoming_booking_keeps_phone(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    await _add_booking(
        session_factory, user.id, _NOW + datetime.timedelta(hours=3),
        models.BookingStatus.ACTIVE,
    )
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, user.id, _NOW)
    kept = await _load_user(session_factory, user.id)
    assert kept.phone == "+7(900)123-45-67"


async def test_delete_personal_data_cancelled_upcoming_booking_allowed(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    await _add_booking(
        session_factory, user.id, _NOW + datetime.timedelta(hours=3),
        models.BookingStatus.CANCELLED,
    )
    async with session_factory() as db_session:
        outcome = await users.delete_personal_data(db_session, user.id, _NOW)
    assert outcome == users.DeleteOutcome.DELETED


async def test_delete_personal_data_records_event(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, user.id, _NOW)
        event = await db_session.scalar(sqlalchemy.select(models.Event))
    assert (event.user_id, event.type) == (
        user.id, models.EventType.DATA_DELETED,
    )


async def test_register_user_after_delete_restores_consent_and_name(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, user.id, _NOW)
    async with session_factory() as db_session:
        again = await users.register_user(db_session, 1, "Пётр")
    assert (again.id, again.name, again.consent_at is not None) == (
        user.id, "Пётр", True,
    )


async def test_register_user_after_delete_keeps_block(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.User)
            .where(models.User.id == user.id)
            .values(blocked_until=_BLOCKED_UNTIL, no_show_count=1)
        )
        await db_session.commit()
        await users.delete_personal_data(db_session, user.id, _NOW)
    async with session_factory() as db_session:
        again = await users.register_user(db_session, 1, "Иван")
    assert (again.blocked_until, again.no_show_count) == (_BLOCKED_UNTIL, 1)


async def test_register_user_after_delete_phone_stays_empty(
    session_factory: _SessionFactory,
) -> None:
    user = await _register_buyer(session_factory)
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, user.id, _NOW)
    async with session_factory() as db_session:
        again = await users.register_user(db_session, 1, "Иван")
    assert again.phone is None

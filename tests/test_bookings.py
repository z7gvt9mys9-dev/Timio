"""Тесты бронирования по B11: гонка, лимит, отмена, блокировка (B4.5)."""

import asyncio
import collections.abc
import dataclasses
import datetime
import pathlib

import pytest
import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from core import bookings
from core import businesses
from core import pricing
from core import slots
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_NOW = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.UTC)
_HOUR = datetime.timedelta(hours=1)
_MINUTE = datetime.timedelta(minutes=1)
_OWNER_TG_ID = 100
_BUYER_TG_ID = 200
_SECOND_BUYER_TG_ID = 300
_Outcome = bookings.BookOutcome


@dataclasses.dataclass(frozen=True)
class _World:
    owner_id: int
    service_id: int
    buyer_id: int
    second_buyer_id: int


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _register_buyer(
    session_factory: _SessionFactory, tg_id: int, has_phone: bool = True
) -> int:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, tg_id, "Покупатель")
        if has_phone:
            await users.set_phone(db_session, user.id, f"7900{tg_id:07d}")
    return user.id


@pytest.fixture
async def world(session_factory: _SessionFactory) -> _World:
    async with session_factory() as db_session:
        owner = await users.register_user(db_session, _OWNER_TG_ID, "Иван")
        business = await businesses.create_business(
            db_session, owner.id,
            businesses.BusinessInput(
                "Борода", "барбершоп", "Хамовники", "ул. Ленина, 1",
                "+7(900)123-45-67",
            ),
        )
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Стрижка", 60, 1500),
        )
        await businesses.approve_business(db_session, business.id)
    return _World(
        owner_id=owner.id,
        service_id=service.id,
        buyer_id=await _register_buyer(session_factory, _BUYER_TG_ID),
        second_buyer_id=await _register_buyer(
            session_factory, _SECOND_BUYER_TG_ID
        ),
    )


async def _insert_slot(
    session_factory: _SessionFactory,
    world: _World,
    starts_in: datetime.timedelta = 5 * _HOUR,
    status: models.SlotStatus = models.SlotStatus.OPEN,
) -> int:
    async with session_factory() as db_session:
        slot = models.Slot(
            service_id=world.service_id, starts_at=_NOW + starts_in,
            start_price=1250, min_price=750, current_price=1250,
            status=status, published_at=_NOW - 20 * _HOUR,
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


async def _book(
    session_factory: _SessionFactory,
    buyer_id: int,
    slot_id: int,
    now: datetime.datetime = _NOW,
) -> bookings.BookResult:
    async with session_factory() as db_session:
        return await bookings.book_slot(db_session, buyer_id, slot_id, now)


async def _cancel(
    session_factory: _SessionFactory,
    buyer_id: int,
    booking_id: int,
    now: datetime.datetime = _NOW,
) -> bookings.CancelResult:
    async with session_factory() as db_session:
        return await bookings.cancel_booking(
            db_session, buyer_id, booking_id, now
        )


async def _block(
    session_factory: _SessionFactory,
    user_id: int,
    blocked_until: datetime.datetime,
) -> None:
    # Блокировку после неявок выставит шаг 7, здесь — вручную.
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.User)
            .where(models.User.id == user_id)
            .values(blocked_until=blocked_until)
        )
        await db_session.commit()


async def _slot_status(
    session_factory: _SessionFactory, slot_id: int
) -> models.SlotStatus:
    async with session_factory() as db_session:
        slot = await db_session.get(models.Slot, slot_id)
    return slot.status


async def _booking(
    session_factory: _SessionFactory, booking_id: int
) -> models.Booking:
    async with session_factory() as db_session:
        return await db_session.get(models.Booking, booking_id)


async def _count_bookings(session_factory: _SessionFactory) -> int:
    async with session_factory() as db_session:
        return await db_session.scalar(
            sqlalchemy.select(sqlalchemy.func.count(models.Booking.id))
        )


async def test_book_slot_open_slot_booked(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    result = await _book(session_factory, world.buyer_id, slot_id)
    status = await _slot_status(session_factory, slot_id)
    assert (result.outcome, status) == (
        _Outcome.BOOKED, models.SlotStatus.BOOKED,
    )


async def test_book_slot_price_is_current_price_at_booking(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    result = await _book(session_factory, world.buyer_id, slot_id)
    expected = pricing.calc_current_price(
        1250, 750, _NOW + 5 * _HOUR, _NOW - 20 * _HOUR, _NOW
    )
    assert result.details.price == expected


async def test_book_slot_price_not_recalculated_later(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    result = await _book(session_factory, world.buyer_id, slot_id)
    async with session_factory() as db_session:
        await slots.recalc_prices(db_session, _NOW + 4 * _HOUR)
    booking = await _booking(session_factory, result.details.booking_id)
    assert booking.price == result.details.price


async def test_book_slot_details_reveal_business_contacts(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    details = (await _book(session_factory, world.buyer_id, slot_id)).details
    assert (details.business_address, details.business_phone,
            details.owner_tg_id) == (
        "ул. Ленина, 1", "+7(900)123-45-67", _OWNER_TG_ID,
    )


async def test_book_slot_race_of_two_buyers_one_wins(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    results = await asyncio.gather(
        _book(session_factory, world.buyer_id, slot_id),
        _book(session_factory, world.second_buyer_id, slot_id),
    )
    outcomes = sorted(result.outcome for result in results)
    assert outcomes == sorted([_Outcome.BOOKED, _Outcome.TAKEN])


async def test_book_slot_race_of_two_buyers_creates_one_booking(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    await asyncio.gather(
        _book(session_factory, world.buyer_id, slot_id),
        _book(session_factory, world.second_buyer_id, slot_id),
    )
    assert await _count_bookings(session_factory) == 1


async def test_book_slot_already_booked_taken(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    await _book(session_factory, world.buyer_id, slot_id)
    result = await _book(session_factory, world.second_buyer_id, slot_id)
    assert result.outcome == _Outcome.TAKEN


async def test_book_slot_third_active_booking_refused(
    session_factory: _SessionFactory, world: _World
) -> None:
    for hours in (3, 4):
        slot_id = await _insert_slot(session_factory, world, hours * _HOUR)
        await _book(session_factory, world.buyer_id, slot_id)
    third_id = await _insert_slot(session_factory, world, 6 * _HOUR)
    result = await _book(session_factory, world.buyer_id, third_id)
    assert result.outcome == _Outcome.LIMIT_REACHED


async def test_book_slot_after_cancel_limit_frees_up(
    session_factory: _SessionFactory, world: _World
) -> None:
    first_id = await _insert_slot(session_factory, world, 3 * _HOUR)
    first = await _book(session_factory, world.buyer_id, first_id)
    second_id = await _insert_slot(session_factory, world, 4 * _HOUR)
    await _book(session_factory, world.buyer_id, second_id)
    await _cancel(
        session_factory, world.buyer_id, first.details.booking_id
    )
    third_id = await _insert_slot(session_factory, world, 6 * _HOUR)
    result = await _book(session_factory, world.buyer_id, third_id)
    assert result.outcome == _Outcome.BOOKED


async def test_book_slot_started_slots_not_counted_in_limit(
    session_factory: _SessionFactory, world: _World
) -> None:
    for hours in (3, 4):
        slot_id = await _insert_slot(session_factory, world, hours * _HOUR)
        await _book(session_factory, world.buyer_id, slot_id)
    # Через 5 часов оба слота уже начались: визиты ещё не отмечены.
    later = _NOW + 5 * _HOUR
    third_id = await _insert_slot(session_factory, world, 8 * _HOUR)
    result = await _book(session_factory, world.buyer_id, third_id, later)
    assert result.outcome == _Outcome.BOOKED


async def test_book_slot_blocked_buyer_refused_with_date(
    session_factory: _SessionFactory, world: _World
) -> None:
    blocked_until = _NOW + 10 * 24 * _HOUR
    await _block(session_factory, world.buyer_id, blocked_until)
    slot_id = await _insert_slot(session_factory, world)
    result = await _book(session_factory, world.buyer_id, slot_id)
    assert (result.outcome, result.blocked_until) == (
        _Outcome.BLOCKED, blocked_until,
    )


async def test_book_slot_blocked_buyer_keeps_slot_open(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block(session_factory, world.buyer_id, _NOW + _HOUR)
    slot_id = await _insert_slot(session_factory, world)
    await _book(session_factory, world.buyer_id, slot_id)
    assert await _slot_status(session_factory, slot_id) == (
        models.SlotStatus.OPEN
    )


async def test_book_slot_block_expired_buyer_booked(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block(session_factory, world.buyer_id, _NOW - _MINUTE)
    slot_id = await _insert_slot(session_factory, world)
    result = await _book(session_factory, world.buyer_id, slot_id)
    assert result.outcome == _Outcome.BOOKED


async def test_book_slot_without_phone_refused(
    session_factory: _SessionFactory, world: _World
) -> None:
    no_phone_id = await _register_buyer(session_factory, 400, has_phone=False)
    slot_id = await _insert_slot(session_factory, world)
    result = await _book(session_factory, no_phone_id, slot_id)
    assert result.outcome == _Outcome.NO_PHONE


async def test_book_slot_own_business_refused(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        await users.set_phone(db_session, world.owner_id, "79001112233")
    slot_id = await _insert_slot(session_factory, world)
    result = await _book(session_factory, world.owner_id, slot_id)
    assert result.outcome == _Outcome.OWN_SLOT


async def test_cancel_booking_61_minutes_before_reopens_slot(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world, 61 * _MINUTE)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    result = await _cancel(
        session_factory, world.buyer_id, booked.details.booking_id
    )
    status = await _slot_status(session_factory, slot_id)
    assert (result.outcome, status) == (
        bookings.CancelOutcome.CANCELLED, models.SlotStatus.OPEN,
    )


async def test_cancel_booking_marked_cancelled_by_buyer(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    await _cancel(session_factory, world.buyer_id, booked.details.booking_id)
    booking = await _booking(session_factory, booked.details.booking_id)
    assert (booking.status, booking.cancelled_by, booking.cancelled_at) == (
        models.BookingStatus.CANCELLED, models.CancelledBy.BUYER, _NOW,
    )


async def test_cancel_booking_59_minutes_before_too_late(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world, 59 * _MINUTE)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    result = await _cancel(
        session_factory, world.buyer_id, booked.details.booking_id
    )
    status = await _slot_status(session_factory, slot_id)
    assert (result.outcome, status) == (
        bookings.CancelOutcome.TOO_LATE, models.SlotStatus.BOOKED,
    )


async def test_cancel_booking_other_buyer_not_found(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    result = await _cancel(
        session_factory, world.second_buyer_id, booked.details.booking_id
    )
    assert result.outcome == bookings.CancelOutcome.NOT_FOUND


async def test_cancel_booking_twice_second_not_found(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    await _cancel(session_factory, world.buyer_id, booked.details.booking_id)
    result = await _cancel(
        session_factory, world.buyer_id, booked.details.booking_id
    )
    assert result.outcome == bookings.CancelOutcome.NOT_FOUND


async def test_cancel_booking_reopened_slot_has_current_price(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    later = _NOW + 2 * _HOUR
    await _cancel(
        session_factory, world.buyer_id, booked.details.booking_id, later
    )
    async with session_factory() as db_session:
        slot = await db_session.get(models.Slot, slot_id)
    assert slot.current_price == pricing.calc_current_price(
        1250, 750, slot.starts_at, slot.published_at, later
    )


async def test_withdraw_booked_slot_cancels_booking_by_owner(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    async with session_factory() as db_session:
        details = await bookings.withdraw_booked_slot(
            db_session, world.owner_id, slot_id, _NOW
        )
    booking = await _booking(session_factory, booked.details.booking_id)
    assert (details.buyer_tg_id, booking.cancelled_by) == (
        _BUYER_TG_ID, models.CancelledBy.OWNER,
    )


async def test_withdraw_booked_slot_status_withdrawn(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    await _book(session_factory, world.buyer_id, slot_id)
    async with session_factory() as db_session:
        await bookings.withdraw_booked_slot(
            db_session, world.owner_id, slot_id, _NOW
        )
    assert await _slot_status(session_factory, slot_id) == (
        models.SlotStatus.WITHDRAWN
    )


async def test_withdraw_booked_slot_other_owner_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    await _book(session_factory, world.buyer_id, slot_id)
    async with session_factory() as db_session:
        details = await bookings.withdraw_booked_slot(
            db_session, world.second_buyer_id, slot_id, _NOW
        )
    assert details is None


async def test_withdraw_booked_slot_open_slot_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    async with session_factory() as db_session:
        details = await bookings.withdraw_booked_slot(
            db_session, world.owner_id, slot_id, _NOW
        )
    assert details is None


async def test_list_buyer_bookings_only_active_upcoming(
    session_factory: _SessionFactory, world: _World
) -> None:
    upcoming_id = await _insert_slot(session_factory, world, 3 * _HOUR)
    upcoming = await _book(session_factory, world.buyer_id, upcoming_id)
    cancelled_id = await _insert_slot(session_factory, world, 4 * _HOUR)
    cancelled = await _book(session_factory, world.buyer_id, cancelled_id)
    await _cancel(
        session_factory, world.buyer_id, cancelled.details.booking_id
    )
    async with session_factory() as db_session:
        listed = await bookings.list_buyer_bookings(
            db_session, world.buyer_id, _NOW
        )
    assert [item.booking_id for item in listed] == [
        upcoming.details.booking_id
    ]


@pytest.mark.parametrize(
    ("starts_in", "expected"),
    [(_HOUR, True), (_HOUR - _MINUTE, False)],
)
def test_can_cancel_one_hour_boundary(
    starts_in: datetime.timedelta, expected: bool
) -> None:
    assert bookings.can_cancel(_NOW + starts_in, _NOW) == expected


async def test_quote_price_booked_slot_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(
        session_factory, world, status=models.SlotStatus.BOOKED
    )
    async with session_factory() as db_session:
        assert await bookings.quote_price(db_session, slot_id, _NOW) is None


async def test_record_slot_opened_writes_event(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    async with session_factory() as db_session:
        await bookings.record_slot_opened(db_session, world.buyer_id, slot_id)
        event = await db_session.scalar(
            sqlalchemy.select(models.Event).where(
                models.Event.type == bookings.EVENT_SLOT_OPENED
            )
        )
    assert event.payload == {"slot_id": slot_id}


async def test_find_buyer_booking_own_active_returns_details(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    async with session_factory() as db_session:
        found = await bookings.find_buyer_booking(
            db_session, world.buyer_id, booked.details.booking_id
        )
    assert found == booked.details


async def test_find_buyer_booking_other_buyer_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    async with session_factory() as db_session:
        found = await bookings.find_buyer_booking(
            db_session, world.second_buyer_id, booked.details.booking_id
        )
    assert found is None


async def test_find_buyer_booking_cancelled_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    slot_id = await _insert_slot(session_factory, world)
    booked = await _book(session_factory, world.buyer_id, slot_id)
    await _cancel(session_factory, world.buyer_id, booked.details.booking_id)
    async with session_factory() as db_session:
        found = await bookings.find_buyer_booking(
            db_session, world.buyer_id, booked.details.booking_id
        )
    assert found is None


_Kind = bookings.NoticeKind


async def _booked(
    session_factory: _SessionFactory,
    world: _World,
    starts_in: datetime.timedelta = 5 * _HOUR,
    buyer_id: int | None = None,
) -> bookings.BookingDetails:
    slot_id = await _insert_slot(session_factory, world, starts_in)
    result = await _book(
        session_factory, buyer_id or world.buyer_id, slot_id
    )
    return result.details


async def _due_kinds(
    session_factory: _SessionFactory, now: datetime.datetime
) -> list[bookings.NoticeKind]:
    async with session_factory() as db_session:
        due = await bookings.due_notices(db_session, now)
    return [notice.kind for notice in due]


async def _visit(
    session_factory: _SessionFactory,
    owner_id: int,
    booking_id: int,
    has_come: bool,
    now: datetime.datetime,
) -> bookings.VisitResult:
    async with session_factory() as db_session:
        return await bookings.record_visit(
            db_session, owner_id, booking_id, has_come, now
        )


async def _buyer(session_factory: _SessionFactory, user_id: int) -> models.User:
    async with session_factory() as db_session:
        return await db_session.get(models.User, user_id)


async def test_due_notices_two_hour_reminder_at_1_59(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    now = details.starts_at - (2 * _HOUR - _MINUTE)
    assert await _due_kinds(session_factory, now) == [_Kind.REMINDER_2H]


async def test_due_notices_nothing_at_2_01(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    now = details.starts_at - (2 * _HOUR + _MINUTE)
    assert await _due_kinds(session_factory, now) == []


async def test_due_notices_only_30_minute_reminder_at_0_29(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    now = details.starts_at - 29 * _MINUTE
    assert await _due_kinds(session_factory, now) == [_Kind.REMINDER_30M]


async def test_due_notices_late_booking_skips_two_hour_reminder(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world, starts_in=_HOUR)
    now = details.starts_at - 59 * _MINUTE
    assert await _due_kinds(session_factory, now) == []


async def test_due_notices_sent_reminder_not_due_again(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    async with session_factory() as db_session:
        await bookings.mark_notified(
            db_session, _Kind.REMINDER_2H, details.booking_id
        )
    now = details.starts_at - 90 * _MINUTE
    assert await _due_kinds(session_factory, now) == []


async def test_due_notices_cancelled_booking_gets_nothing(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    await _cancel(session_factory, world.buyer_id, details.booking_id)
    now = details.starts_at - 29 * _MINUTE
    assert await _due_kinds(session_factory, now) == []


async def test_due_notices_visit_question_one_hour_after_start(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    now = details.starts_at + _HOUR
    assert await _due_kinds(session_factory, now) == [_Kind.VISIT_ASKED]


async def test_due_notices_no_visit_question_at_59_minutes(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    now = details.starts_at + 59 * _MINUTE
    assert await _due_kinds(session_factory, now) == []


async def test_due_notices_visit_question_asked_once(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    async with session_factory() as db_session:
        await bookings.mark_notified(
            db_session, _Kind.VISIT_ASKED, details.booking_id
        )
    now = details.starts_at + 2 * _HOUR
    assert await _due_kinds(session_factory, now) == []


async def test_record_visit_came_marks_slot_and_booking_done(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    await _visit(session_factory, world.owner_id, details.booking_id, True,
                 details.starts_at + _HOUR)
    booking = await _booking(session_factory, details.booking_id)
    status = await _slot_status(session_factory, details.slot_id)
    assert (booking.status, status) == (
        models.BookingStatus.DONE, models.SlotStatus.DONE,
    )


async def test_record_visit_no_show_marks_and_counts(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    result = await _visit(session_factory, world.owner_id,
                          details.booking_id, False,
                          details.starts_at + _HOUR)
    status = await _slot_status(session_factory, details.slot_id)
    buyer = await _buyer(session_factory, world.buyer_id)
    assert (status, buyer.no_show_count, result.buyer_blocked_until) == (
        models.SlotStatus.NO_SHOW, 1, None,
    )


async def test_record_visit_second_no_show_blocks_for_30_days(
    session_factory: _SessionFactory, world: _World
) -> None:
    first = await _booked(session_factory, world, 3 * _HOUR)
    second = await _booked(session_factory, world, 4 * _HOUR)
    answer_time = second.starts_at + _HOUR
    for details in (first, second):
        result = await _visit(session_factory, world.owner_id,
                              details.booking_id, False, answer_time)
    buyer = await _buyer(session_factory, world.buyer_id)
    blocked_until = answer_time + datetime.timedelta(days=30)
    assert (result.buyer_blocked_until, buyer.blocked_until,
            buyer.no_show_count) == (blocked_until, blocked_until, 0)


async def test_record_visit_after_block_buyer_cannot_book(
    session_factory: _SessionFactory, world: _World
) -> None:
    first = await _booked(session_factory, world, 3 * _HOUR)
    second = await _booked(session_factory, world, 4 * _HOUR)
    answer_time = second.starts_at + _HOUR
    for details in (first, second):
        await _visit(session_factory, world.owner_id, details.booking_id,
                     False, answer_time)
    slot_id = await _insert_slot(session_factory, world, 30 * _HOUR)
    result = await _book(session_factory, world.buyer_id, slot_id,
                         answer_time)
    assert result.outcome == _Outcome.BLOCKED


async def test_record_visit_twice_second_not_found(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    now = details.starts_at + _HOUR
    await _visit(session_factory, world.owner_id, details.booking_id, True,
                 now)
    second = await _visit(session_factory, world.owner_id,
                          details.booking_id, False, now)
    booking = await _booking(session_factory, details.booking_id)
    assert (second.outcome, booking.status) == (
        bookings.VisitOutcome.NOT_FOUND, models.BookingStatus.DONE,
    )


async def test_record_visit_stranger_not_found(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    result = await _visit(session_factory, world.second_buyer_id,
                          details.booking_id, False,
                          details.starts_at + _HOUR)
    assert result.outcome == bookings.VisitOutcome.NOT_FOUND


async def test_record_visit_before_start_not_found(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    result = await _visit(session_factory, world.owner_id,
                          details.booking_id, False,
                          details.starts_at - _MINUTE)
    assert result.outcome == bookings.VisitOutcome.NOT_FOUND


async def test_auto_confirm_due_after_25_hours_marks_done(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    async with session_factory() as db_session:
        confirmed = await bookings.auto_confirm_due(
            db_session, details.starts_at + 25 * _HOUR
        )
    status = await _slot_status(session_factory, details.slot_id)
    assert (confirmed, status) == (
        [details.booking_id], models.SlotStatus.DONE,
    )


async def test_auto_confirm_due_at_24_59_waits(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    async with session_factory() as db_session:
        confirmed = await bookings.auto_confirm_due(
            db_session, details.starts_at + 25 * _HOUR - _MINUTE
        )
    assert confirmed == []


async def test_auto_confirm_due_keeps_owner_answer(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    await _visit(session_factory, world.owner_id, details.booking_id, False,
                 details.starts_at + _HOUR)
    async with session_factory() as db_session:
        confirmed = await bookings.auto_confirm_due(
            db_session, details.starts_at + 26 * _HOUR
        )
    booking = await _booking(session_factory, details.booking_id)
    assert (confirmed, booking.status) == ([], models.BookingStatus.NO_SHOW)


async def _withdraw(
    session_factory: _SessionFactory, owner_id: int, slot_id: int
) -> None:
    async with session_factory() as db_session:
        await bookings.withdraw_booked_slot(
            db_session, owner_id, slot_id, _NOW
        )


async def test_count_owner_withdrawals_counts_cancels_by_owner(
    session_factory: _SessionFactory, world: _World
) -> None:
    buyers = (world.buyer_id, world.buyer_id, world.second_buyer_id)
    for hours, buyer_id in zip((3, 4, 5), buyers):
        details = await _booked(session_factory, world, hours * _HOUR,
                                buyer_id)
        await _withdraw(session_factory, world.owner_id, details.slot_id)
    async with session_factory() as db_session:
        count = await bookings.count_owner_withdrawals(
            db_session, world.owner_id
        )
    assert count == 3


async def test_count_owner_withdrawals_ignores_buyer_cancels(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    await _cancel(session_factory, world.buyer_id, details.booking_id)
    async with session_factory() as db_session:
        count = await bookings.count_owner_withdrawals(
            db_session, world.owner_id
        )
    assert count == 0


async def test_count_owner_withdrawals_other_business_not_counted(
    session_factory: _SessionFactory, world: _World
) -> None:
    details = await _booked(session_factory, world)
    await _withdraw(session_factory, world.owner_id, details.slot_id)
    async with session_factory() as db_session:
        count = await bookings.count_owner_withdrawals(
            db_session, world.second_buyer_id
        )
    assert count == 0


@pytest.mark.parametrize(
    ("withdrawals", "expected"), [(2, False), (3, True), (4, True)]
)
def test_should_alert_admins_from_third_withdrawal(
    withdrawals: int, expected: bool
) -> None:
    assert bookings.should_alert_admins(withdrawals) == expected

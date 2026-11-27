"""Тесты слотов: переходы B7, создание, снятие, закрытие, цена (B4.3)."""

import collections.abc
import dataclasses
import datetime
import pathlib
import zoneinfo

import pytest
import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from core import businesses
from core import slots
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
# 12:00 по Москве.
_NOW = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.UTC)
_OWNER_TG_ID = 100
_OTHER_TG_ID = 200
_MIN_PRICE = 750
_Status = models.SlotStatus


@dataclasses.dataclass(frozen=True)
class _Owner:
    user_id: int
    service_id: int


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _create_owner(
    session_factory: _SessionFactory,
    tg_id: int = _OWNER_TG_ID,
    is_approved: bool = True,
) -> _Owner:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, tg_id, "Иван")
        business = await businesses.create_business(
            db_session, user.id,
            businesses.BusinessInput(
                "Борода", "барбершоп", "Хамовники", "ул. Ленина, 1",
                "+7(900)123-45-67",
            ),
        )
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Стрижка", 60, 1500),
        )
        if is_approved:
            await businesses.approve_business(db_session, business.id)
    return _Owner(user_id=user.id, service_id=service.id)


@pytest.fixture
async def owner(session_factory: _SessionFactory) -> _Owner:
    return await _create_owner(session_factory)


def _input(
    owner: _Owner,
    lead: datetime.timedelta = datetime.timedelta(hours=26),
    min_price: int = _MIN_PRICE,
    start_price: int = 1250,
) -> slots.SlotInput:
    return slots.SlotInput(
        service_id=owner.service_id, starts_at=_NOW + lead,
        start_price=start_price, min_price=min_price, hide_name=False,
    )


async def _create(
    session_factory: _SessionFactory,
    owner: _Owner,
    slot_input: slots.SlotInput,
    now: datetime.datetime = _NOW,
) -> models.Slot:
    async with session_factory() as db_session:
        return await slots.create_slot(
            db_session, owner.user_id, slot_input, now
        )


async def _insert(
    session_factory: _SessionFactory,
    owner: _Owner,
    starts_at: datetime.datetime,
    status: models.SlotStatus = _Status.OPEN,
) -> int:
    # Слот в прошлом через create_slot не создать: окно 2–48 часов.
    async with session_factory() as db_session:
        slot = models.Slot(
            service_id=owner.service_id, starts_at=starts_at,
            start_price=1250, min_price=_MIN_PRICE, current_price=1250,
            status=status, published_at=starts_at - datetime.timedelta(days=1),
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


async def _status(session_factory: _SessionFactory, slot_id: int) -> str:
    async with session_factory() as db_session:
        slot = await db_session.get(models.Slot, slot_id)
    return slot.status


@pytest.mark.parametrize(
    ("from_status", "to_status"), sorted(slots.ALLOWED_TRANSITIONS)
)
def test_check_transition_b7_pair_allowed(
    from_status: models.SlotStatus, to_status: models.SlotStatus
) -> None:
    slots.check_transition(from_status, to_status)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (_Status.WITHDRAWN, _Status.OPEN),
        (_Status.EXPIRED, _Status.OPEN),
        (_Status.DONE, _Status.OPEN),
        (_Status.NO_SHOW, _Status.DONE),
        (_Status.OPEN, _Status.DONE),
        (_Status.OPEN, _Status.NO_SHOW),
    ],
)
def test_check_transition_not_in_b7_raises(
    from_status: models.SlotStatus, to_status: models.SlotStatus
) -> None:
    with pytest.raises(slots.InvalidTransitionError):
        slots.check_transition(from_status, to_status)


def test_build_starts_at_city_time_converted_to_utc() -> None:
    starts_at = slots.build_starts_at(
        datetime.date(2026, 12, 1), "15:00", _CITY_TZ
    )
    assert starts_at.astimezone(datetime.UTC) == datetime.datetime(
        2026, 12, 1, 12, 0, tzinfo=datetime.UTC
    )


@pytest.mark.parametrize("raw_time", ["25:00", "15-00", "в три"])
def test_build_starts_at_bad_format_raises(raw_time: str) -> None:
    with pytest.raises(ValueError, match="Пример: 15:00"):
        slots.build_starts_at(datetime.date(2026, 12, 1), raw_time, _CITY_TZ)


def test_parse_min_price_inside_range_returns_int() -> None:
    assert slots.parse_min_price(" 800 ", 1500, 1250) == 800


@pytest.mark.parametrize("raw_price", ["449", "1251", "дёшево"])
def test_parse_min_price_outside_range_raises(raw_price: str) -> None:
    with pytest.raises(ValueError, match="от 450 до 1250"):
        slots.parse_min_price(raw_price, 1500, 1250)


def test_parse_min_price_up_to_owner_start_price_accepted() -> None:
    assert slots.parse_min_price("1400", 1500, 1500) == 1400


def test_parse_start_price_up_to_regular_returns_int() -> None:
    assert slots.parse_start_price(" 1500 ", 1500) == 1500


@pytest.mark.parametrize("raw_price", ["449", "1501", "дорого"])
def test_parse_start_price_outside_range_raises(raw_price: str) -> None:
    with pytest.raises(ValueError, match="от 450 до 1500"):
        slots.parse_start_price(raw_price, 1500)


def test_parse_start_price_error_suggests_default_start() -> None:
    with pytest.raises(ValueError, match="Пример: 1250"):
        slots.parse_start_price("1501", 1500)


async def test_create_slot_valid_input_is_open(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    assert slot.status == _Status.OPEN


async def test_create_slot_prices_from_regular_price(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    assert (slot.start_price, slot.min_price, slot.current_price) == (
        1250, _MIN_PRICE, 1250,
    )


async def test_create_slot_stores_owner_start_price(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_input = _input(owner, start_price=1500)
    slot = await _create(session_factory, owner, slot_input)
    assert (slot.start_price, slot.current_price) == (1500, 1500)


@pytest.mark.parametrize("start_price", [449, 1501])
async def test_create_slot_start_price_outside_range_rejected(
    session_factory: _SessionFactory, owner: _Owner, start_price: int
) -> None:
    slot_input = _input(owner, min_price=449, start_price=start_price)
    with pytest.raises(slots.SlotRejectedError, match="от 450 до 1500"):
        await _create(session_factory, owner, slot_input)


async def test_create_slot_min_price_above_start_rejected(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_input = _input(owner, min_price=1100, start_price=1000)
    with pytest.raises(slots.SlotRejectedError, match="от 450 до 1000"):
        await _create(session_factory, owner, slot_input)


async def test_create_slot_late_publication_starts_at_min(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_input = _input(owner, datetime.timedelta(hours=2, minutes=10))
    slot = await _create(session_factory, owner, slot_input)
    assert slot.current_price == _MIN_PRICE


async def test_create_slot_stored_in_utc(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    starts_at = slots.build_starts_at(
        datetime.date(2026, 12, 1), "18:00", _CITY_TZ
    )
    slot_input = dataclasses.replace(_input(owner), starts_at=starts_at)
    slot = await _create(session_factory, owner, slot_input)
    async with session_factory() as db_session:
        stored = await db_session.get(models.Slot, slot.id)
    assert stored.starts_at == datetime.datetime(
        2026, 12, 1, 15, 0, tzinfo=datetime.UTC
    )


async def test_create_slot_writes_slot_created_event(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    async with session_factory() as db_session:
        event = await db_session.scalar(sqlalchemy.select(models.Event))
    assert (event.type, event.payload) == (
        models.EventType.SLOT_CREATED, {"slot_id": slot.id},
    )


@pytest.mark.parametrize(
    "lead",
    [
        datetime.timedelta(hours=1, minutes=59),
        datetime.timedelta(hours=48, minutes=1),
    ],
)
async def test_create_slot_outside_window_rejected(
    session_factory: _SessionFactory,
    owner: _Owner,
    lead: datetime.timedelta,
) -> None:
    with pytest.raises(slots.SlotRejectedError, match="за 2 часа"):
        await _create(session_factory, owner, _input(owner, lead))


@pytest.mark.parametrize(
    "lead", [datetime.timedelta(hours=2), datetime.timedelta(hours=48)]
)
async def test_create_slot_window_edges_accepted(
    session_factory: _SessionFactory,
    owner: _Owner,
    lead: datetime.timedelta,
) -> None:
    slot = await _create(session_factory, owner, _input(owner, lead))
    assert slot.status == _Status.OPEN


@pytest.mark.parametrize("min_price", [449, 1251])
async def test_create_slot_min_price_outside_range_rejected(
    session_factory: _SessionFactory, owner: _Owner, min_price: int
) -> None:
    slot_input = _input(owner, min_price=min_price)
    with pytest.raises(slots.SlotRejectedError, match="от 450 до 1250"):
        await _create(session_factory, owner, slot_input)


async def test_create_slot_pending_business_rejected(
    session_factory: _SessionFactory,
) -> None:
    pending_owner = await _create_owner(session_factory, is_approved=False)
    with pytest.raises(slots.SlotRejectedError, match="одобрения"):
        await _create(session_factory, pending_owner, _input(pending_owner))


async def test_create_slot_hidden_service_rejected(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    async with session_factory() as db_session:
        await businesses.set_service_active(
            db_session, owner.user_id, owner.service_id, is_active=False
        )
    with pytest.raises(slots.SlotRejectedError, match="скрыта"):
        await _create(session_factory, owner, _input(owner))


async def test_create_slot_other_owner_service_rejected(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    other = await _create_owner(session_factory, _OTHER_TG_ID)
    foreign_input = _input(owner)
    with pytest.raises(slots.SlotRejectedError, match="не найдена"):
        await _create(session_factory, other, foreign_input)


async def test_create_slot_blocks_duration_change(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _create(session_factory, owner, _input(owner))
    async with session_factory() as db_session:
        assert await businesses.has_active_slots(
            db_session, owner.service_id
        )


async def test_withdraw_slot_open_becomes_withdrawn(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    async with session_factory() as db_session:
        await slots.withdraw_slot(db_session, owner.user_id, slot.id)
    assert await _status(session_factory, slot.id) == _Status.WITHDRAWN


async def test_withdraw_slot_twice_second_returns_false(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    async with session_factory() as db_session:
        await slots.withdraw_slot(db_session, owner.user_id, slot.id)
        is_withdrawn = await slots.withdraw_slot(
            db_session, owner.user_id, slot.id
        )
    assert not is_withdrawn


async def test_withdraw_slot_other_owner_keeps_open(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    other = await _create_owner(session_factory, _OTHER_TG_ID)
    async with session_factory() as db_session:
        await slots.withdraw_slot(db_session, other.user_id, slot.id)
    assert await _status(session_factory, slot.id) == _Status.OPEN


async def test_withdraw_slot_booked_keeps_booked(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_id = await _insert(
        session_factory, owner, _NOW + datetime.timedelta(hours=5),
        _Status.BOOKED,
    )
    async with session_factory() as db_session:
        await slots.withdraw_slot(db_session, owner.user_id, slot_id)
    assert await _status(session_factory, slot_id) == _Status.BOOKED


async def test_expire_due_slots_30_minutes_before_expired(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_id = await _insert(
        session_factory, owner, _NOW + datetime.timedelta(minutes=30)
    )
    async with session_factory() as db_session:
        await slots.expire_due_slots(db_session, _NOW)
    assert await _status(session_factory, slot_id) == _Status.EXPIRED


async def test_expire_due_slots_31_minutes_before_stays_open(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_id = await _insert(
        session_factory, owner, _NOW + datetime.timedelta(minutes=31)
    )
    async with session_factory() as db_session:
        await slots.expire_due_slots(db_session, _NOW)
    assert await _status(session_factory, slot_id) == _Status.OPEN


async def test_expire_due_slots_booked_untouched(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_id = await _insert(
        session_factory, owner, _NOW + datetime.timedelta(minutes=10),
        _Status.BOOKED,
    )
    async with session_factory() as db_session:
        await slots.expire_due_slots(db_session, _NOW)
    assert await _status(session_factory, slot_id) == _Status.BOOKED


async def test_expire_due_slots_returns_owner_for_notification(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    starts_at = _NOW + datetime.timedelta(minutes=20)
    slot_id = await _insert(session_factory, owner, starts_at)
    async with session_factory() as db_session:
        expired = await slots.expire_due_slots(db_session, _NOW)
    assert expired == [
        slots.ExpiredSlot(slot_id, _OWNER_TG_ID, "Стрижка", starts_at)
    ]


async def test_recalc_prices_after_12_hours_returns_changed(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    async with session_factory() as db_session:
        changed = await slots.recalc_prices(
            db_session, _NOW + datetime.timedelta(hours=12)
        )
    assert changed == [slot.id]


async def test_recalc_prices_after_12_hours_stores_halfway_price(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot = await _create(session_factory, owner, _input(owner))
    async with session_factory() as db_session:
        await slots.recalc_prices(
            db_session, _NOW + datetime.timedelta(hours=12)
        )
        stored = await db_session.get(
            models.Slot, slot.id, populate_existing=True
        )
    assert stored.current_price == 1000


async def test_recalc_prices_booked_slot_skipped(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _insert(
        session_factory, owner, _NOW + datetime.timedelta(hours=3),
        _Status.BOOKED,
    )
    async with session_factory() as db_session:
        changed = await slots.recalc_prices(db_session, _NOW)
    assert changed == []


async def test_list_owner_slots_only_upcoming_open_and_booked(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    hour = datetime.timedelta(hours=1)
    open_id = await _insert(session_factory, owner, _NOW + 3 * hour)
    booked_id = await _insert(
        session_factory, owner, _NOW + 4 * hour, _Status.BOOKED
    )
    await _insert(session_factory, owner, _NOW + 5 * hour, _Status.WITHDRAWN)
    await _insert(session_factory, owner, _NOW - hour)
    async with session_factory() as db_session:
        listed = await slots.list_owner_slots(db_session, owner.user_id, _NOW)
    assert [item.slot.id for item in listed] == [open_id, booked_id]


def _yesterday_at(hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 11, 30, hour, 0, tzinfo=_CITY_TZ)


async def _repeat(
    session_factory: _SessionFactory, owner: _Owner
) -> slots.RepeatResult:
    async with session_factory() as db_session:
        return await slots.repeat_yesterday(
            db_session, owner.user_id, _NOW, _CITY_TZ
        )


async def test_repeat_yesterday_copies_to_same_city_time_today(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _insert(session_factory, owner, _yesterday_at(18), _Status.DONE)
    await _repeat(session_factory, owner)
    async with session_factory() as db_session:
        listed = await slots.list_owner_slots(db_session, owner.user_id, _NOW)
    assert [item.slot.starts_at for item in listed] == [
        datetime.datetime(2026, 12, 1, 18, 0, tzinfo=_CITY_TZ)
    ]


async def test_repeat_yesterday_skips_outside_window(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    # Сегодня в 13:00 по Москве — через час, меньше 2 часов.
    await _insert(session_factory, owner, _yesterday_at(13), _Status.EXPIRED)
    result = await _repeat(session_factory, owner)
    assert result == slots.RepeatResult(created_ids=(), skipped=1)


async def test_repeat_yesterday_copies_start_price(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    slot_id = await _insert(
        session_factory, owner, _yesterday_at(18), _Status.DONE
    )
    await _update_slot(session_factory, slot_id, start_price=1400)
    await _repeat(session_factory, owner)
    async with session_factory() as db_session:
        listed = await slots.list_owner_slots(db_session, owner.user_id, _NOW)
    assert [item.slot.start_price for item in listed] == [1400]


async def test_repeat_yesterday_skips_start_above_new_regular_price(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    # Вчера стартовая 1250, сегодня обычная цена услуги снижена до 1000.
    await _insert(session_factory, owner, _yesterday_at(18), _Status.DONE)
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.Service)
            .where(models.Service.id == owner.service_id)
            .values(regular_price=1000)
        )
        await db_session.commit()
    result = await _repeat(session_factory, owner)
    assert result == slots.RepeatResult(created_ids=(), skipped=1)


async def _update_slot(
    session_factory: _SessionFactory, slot_id: int, **values: int
) -> None:
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.Slot)
            .where(models.Slot.id == slot_id)
            .values(**values)
        )
        await db_session.commit()


async def test_repeat_yesterday_ignores_withdrawn(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _insert(
        session_factory, owner, _yesterday_at(18), _Status.WITHDRAWN
    )
    result = await _repeat(session_factory, owner)
    assert result == slots.RepeatResult(created_ids=(), skipped=0)


async def test_repeat_yesterday_second_press_no_duplicates(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _insert(session_factory, owner, _yesterday_at(18), _Status.DONE)
    await _repeat(session_factory, owner)
    second = await _repeat(session_factory, owner)
    assert second == slots.RepeatResult(created_ids=(), skipped=1)


async def test_repeat_yesterday_returns_created_slot_ids(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _insert(session_factory, owner, _yesterday_at(18), _Status.DONE)
    result = await _repeat(session_factory, owner)
    async with session_factory() as db_session:
        listed = await slots.list_owner_slots(db_session, owner.user_id, _NOW)
    assert result.created_ids == tuple(item.slot.id for item in listed)

"""Тесты бизнесов, услуг и одобрения заявок (B4.2, B4.9)."""

import collections.abc
import datetime
import pathlib

import pytest
from sqlalchemy.ext import asyncio as sa_asyncio

from core import businesses
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_BUSINESS = businesses.BusinessInput(
    name="Борода",
    category="барбершоп",
    district="Центр",
    address="ул. Ленина, 1",
    phone="+7(900)123-45-67",
)
_SERVICE = businesses.ServiceInput(
    title="Стрижка", duration_min=60, regular_price=1500
)
_OWNER_TG_ID = 100
_OTHER_TG_ID = 200


@pytest.mark.parametrize(
    "raw_phone",
    ["+7 900 123-45-67", "8(900)1234567", "9001234567", "+79001234567"],
)
def test_parse_phone_common_formats_normalized(raw_phone: str) -> None:
    assert businesses.parse_phone(raw_phone) == "+7(900)123-45-67"


@pytest.mark.parametrize(
    ("raw_phone", "expected"),
    [("+380 50 123 45 67", "+380501234567"), ("19001234567", "+19001234567")],
)
def test_parse_phone_foreign_number_kept_as_plus_digits(
    raw_phone: str, expected: str
) -> None:
    assert businesses.parse_phone(raw_phone) == expected


@pytest.mark.parametrize(
    "raw_phone",
    ["90012345", "1234567890123456", "+7 900 abc 45 67", ""],
)
def test_parse_phone_not_a_phone_raises(raw_phone: str) -> None:
    with pytest.raises(ValueError, match="Пример: \\+79001234567"):
        businesses.parse_phone(raw_phone)


def test_parse_duration_valid_returns_minutes() -> None:
    assert businesses.parse_duration(" 60 ") == 60


@pytest.mark.parametrize("raw_duration", ["5", "481", "час"])
def test_parse_duration_invalid_raises(raw_duration: str) -> None:
    with pytest.raises(ValueError, match="Пример: 60"):
        businesses.parse_duration(raw_duration)


@pytest.mark.parametrize("raw_price", ["99", "100001", "дорого"])
def test_parse_price_invalid_raises(raw_price: str) -> None:
    with pytest.raises(ValueError, match="Пример: 1500"):
        businesses.parse_price(raw_price)


def test_parse_text_field_strips_spaces() -> None:
    text = businesses.parse_text_field(
        businesses.TextField.DISTRICT, "  Центр "
    )
    assert text == "Центр"


@pytest.mark.parametrize("raw_text", ["   ", "я" * 101])
def test_parse_text_field_empty_or_long_raises(raw_text: str) -> None:
    with pytest.raises(ValueError, match="Пример: Хамовники"):
        businesses.parse_text_field(businesses.TextField.DISTRICT, raw_text)


def test_parse_category_unknown_raises() -> None:
    with pytest.raises(ValueError):
        businesses.parse_category("автомойка")


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _register(session_factory: _SessionFactory, tg_id: int) -> int:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, tg_id, "Иван")
    return user.id


async def _create_business(
    session_factory: _SessionFactory, tg_id: int = _OWNER_TG_ID
) -> models.Business:
    owner_id = await _register(session_factory, tg_id)
    async with session_factory() as db_session:
        return await businesses.create_business(
            db_session, owner_id, _BUSINESS
        )


async def test_create_business_new_owner_is_pending(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    assert business.status == models.BusinessStatus.PENDING


async def test_create_business_second_for_owner_raises(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        with pytest.raises(businesses.BusinessAlreadyExistsError):
            await businesses.create_business(
                db_session, business.owner_id, _BUSINESS
            )


async def test_find_owner_business_registered_returns_it(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        found = await businesses.find_owner_business(
            db_session, business.owner_id
        )
    assert found.id == business.id


async def test_approve_business_pending_becomes_approved(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        decision = await businesses.approve_business(db_session, business.id)
    assert decision.business.status == models.BusinessStatus.APPROVED


async def test_approve_business_returns_owner_tg_id(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        decision = await businesses.approve_business(db_session, business.id)
    assert decision.owner_tg_id == _OWNER_TG_ID


async def test_approve_business_already_decided_returns_none(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        await businesses.approve_business(db_session, business.id)
    async with session_factory() as db_session:
        second = await businesses.approve_business(db_session, business.id)
    assert second is None


async def test_reject_business_pending_becomes_blocked(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        decision = await businesses.reject_business(db_session, business.id)
    assert decision.business.status == models.BusinessStatus.BLOCKED


async def test_reject_business_after_approve_keeps_approved(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        await businesses.approve_business(db_session, business.id)
        await businesses.reject_business(db_session, business.id)
    async with session_factory() as db_session:
        found = await businesses.find_owner_business(
            db_session, business.owner_id
        )
    assert found.status == models.BusinessStatus.APPROVED


async def test_list_pending_skips_decided(
    session_factory: _SessionFactory,
) -> None:
    approved = await _create_business(session_factory, _OWNER_TG_ID)
    pending = await _create_business(session_factory, _OTHER_TG_ID)
    async with session_factory() as db_session:
        await businesses.approve_business(db_session, approved.id)
        result = await businesses.list_pending(db_session)
    assert [business.id for business in result] == [pending.id]


async def test_add_service_new_service_is_active(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        service = await businesses.add_service(
            db_session, business.id, _SERVICE
        )
    assert service.is_active


async def test_set_service_active_false_hides_service(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        service = await businesses.add_service(
            db_session, business.id, _SERVICE
        )
        await businesses.set_service_active(
            db_session, business.owner_id, service.id, is_active=False
        )
    async with session_factory() as db_session:
        services = await businesses.list_services(db_session, business.id)
    assert not services[0].is_active


async def test_set_service_active_other_owner_returns_false(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    other_id = await _register(session_factory, _OTHER_TG_ID)
    async with session_factory() as db_session:
        service = await businesses.add_service(
            db_session, business.id, _SERVICE
        )
        is_changed = await businesses.set_service_active(
            db_session, other_id, service.id, is_active=False
        )
    assert not is_changed


async def test_set_service_active_other_owner_keeps_service_active(
    session_factory: _SessionFactory,
) -> None:
    business = await _create_business(session_factory)
    other_id = await _register(session_factory, _OTHER_TG_ID)
    async with session_factory() as db_session:
        service = await businesses.add_service(
            db_session, business.id, _SERVICE
        )
        await businesses.set_service_active(
            db_session, other_id, service.id, is_active=False
        )
    async with session_factory() as db_session:
        services = await businesses.list_services(db_session, business.id)
    assert services[0].is_active


async def _create_service(
    session_factory: _SessionFactory,
) -> models.Service:
    business = await _create_business(session_factory)
    async with session_factory() as db_session:
        return await businesses.add_service(
            db_session, business.id, _SERVICE
        )


async def _add_slot(
    session_factory: _SessionFactory,
    service_id: int,
    status: models.SlotStatus,
) -> None:
    async with session_factory() as db_session:
        db_session.add(
            models.Slot(
                service_id=service_id,
                starts_at=datetime.datetime.now(datetime.UTC),
                start_price=1275,
                min_price=700,
                current_price=1275,
                status=status,
            )
        )
        await db_session.commit()


async def _update(
    session_factory: _SessionFactory,
    service: models.Service,
    field: businesses.ServiceField,
    raw_text: str,
    owner_id: int | None = None,
) -> businesses.ServiceUpdateResult:
    async with session_factory() as db_session:
        if owner_id is None:
            business = await db_session.get(
                models.Business, service.business_id
            )
            owner_id = business.owner_id
        return await businesses.update_service(
            db_session, owner_id, service.id, field, raw_text
        )


async def _reload(
    session_factory: _SessionFactory, service: models.Service
) -> models.Service:
    async with session_factory() as db_session:
        return await db_session.get(models.Service, service.id)


async def test_update_service_title_changes_title(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    await _update(
        session_factory, service, businesses.ServiceField.TITLE, "Бритьё"
    )
    assert (await _reload(session_factory, service)).title == "Бритьё"


async def test_update_service_price_keeps_other_fields(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    await _update(
        session_factory, service, businesses.ServiceField.REGULAR_PRICE,
        "1800",
    )
    updated = await _reload(session_factory, service)
    assert (updated.regular_price, updated.title, updated.duration_min) == (
        1800, "Стрижка", 60,
    )


async def test_update_service_duration_without_slots_updated(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    result = await _update(
        session_factory, service, businesses.ServiceField.DURATION_MIN, "90"
    )
    assert result == businesses.ServiceUpdateResult.UPDATED


async def test_update_service_invalid_value_raises(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    with pytest.raises(ValueError, match="Пример: 60"):
        await _update(
            session_factory, service, businesses.ServiceField.DURATION_MIN,
            "5",
        )


@pytest.mark.parametrize(
    "status", [models.SlotStatus.OPEN, models.SlotStatus.BOOKED]
)
async def test_update_service_duration_with_active_slot_locked(
    session_factory: _SessionFactory, status: models.SlotStatus
) -> None:
    service = await _create_service(session_factory)
    await _add_slot(session_factory, service.id, status)
    result = await _update(
        session_factory, service, businesses.ServiceField.DURATION_MIN, "90"
    )
    assert result == businesses.ServiceUpdateResult.DURATION_LOCKED


async def test_update_service_duration_locked_keeps_duration(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    await _add_slot(session_factory, service.id, models.SlotStatus.OPEN)
    await _update(
        session_factory, service, businesses.ServiceField.DURATION_MIN, "90"
    )
    assert (await _reload(session_factory, service)).duration_min == 60


async def test_update_service_duration_with_done_slot_updated(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    await _add_slot(session_factory, service.id, models.SlotStatus.DONE)
    result = await _update(
        session_factory, service, businesses.ServiceField.DURATION_MIN, "90"
    )
    assert result == businesses.ServiceUpdateResult.UPDATED


async def test_update_service_price_with_open_slot_updated(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    await _add_slot(session_factory, service.id, models.SlotStatus.OPEN)
    result = await _update(
        session_factory, service, businesses.ServiceField.REGULAR_PRICE,
        "1800",
    )
    assert result == businesses.ServiceUpdateResult.UPDATED


async def test_update_service_other_owner_not_found(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    other_id = await _register(session_factory, _OTHER_TG_ID)
    result = await _update(
        session_factory, service, businesses.ServiceField.TITLE, "Бритьё",
        owner_id=other_id,
    )
    assert result == businesses.ServiceUpdateResult.NOT_FOUND


async def test_update_service_other_owner_keeps_title(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    other_id = await _register(session_factory, _OTHER_TG_ID)
    await _update(
        session_factory, service, businesses.ServiceField.TITLE, "Бритьё",
        owner_id=other_id,
    )
    assert (await _reload(session_factory, service)).title == "Стрижка"


async def test_update_service_other_owner_active_slots_not_found(
    session_factory: _SessionFactory,
) -> None:
    # Чужому владельцу не раскрываем, что у услуги есть слоты.
    service = await _create_service(session_factory)
    await _add_slot(session_factory, service.id, models.SlotStatus.OPEN)
    other_id = await _register(session_factory, _OTHER_TG_ID)
    result = await _update(
        session_factory, service, businesses.ServiceField.DURATION_MIN, "90",
        owner_id=other_id,
    )
    assert result == businesses.ServiceUpdateResult.NOT_FOUND


async def test_has_active_slots_no_slots_false(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    async with session_factory() as db_session:
        assert not await businesses.has_active_slots(db_session, service.id)


async def test_has_active_slots_booked_slot_true(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    await _add_slot(session_factory, service.id, models.SlotStatus.BOOKED)
    async with session_factory() as db_session:
        assert await businesses.has_active_slots(db_session, service.id)


async def test_find_owner_service_other_owner_returns_none(
    session_factory: _SessionFactory,
) -> None:
    service = await _create_service(session_factory)
    other_id = await _register(session_factory, _OTHER_TG_ID)
    async with session_factory() as db_session:
        found = await businesses.find_owner_service(
            db_session, other_id, service.id
        )
    assert found is None

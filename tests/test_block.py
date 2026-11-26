"""Тесты /block и /unblock: покупатель и бизнес (B4.9)."""

import collections.abc
import dataclasses
import datetime
import pathlib
import zoneinfo

import aiogram
import pytest
import sqlalchemy
from aiogram import methods
from aiogram import types
from sqlalchemy.ext import asyncio as sa_asyncio

import conftest
from bot import channel
from core import bookings
from core import businesses
from core import feed
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
_CHANNEL_ID = -100500
_NOW = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.UTC)
_HOUR = datetime.timedelta(hours=1)
_OWNER_TG_ID = 100
_BUYER_TG_ID = 200
_SECOND_BUYER_TG_ID = 300
_ADMIN_TG_ID = 900
_UNKNOWN_TG_ID = 999
_POST_MESSAGE_ID = 77
_BOOKED_POST_MESSAGE_ID = 78


@dataclasses.dataclass(frozen=True)
class _World:
    owner_id: int
    business_id: int
    buyer_id: int
    open_slot_id: int
    booking: bookings.BookingDetails


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _register_buyer(
    db_session: sa_asyncio.AsyncSession, tg_id: int
) -> int:
    user = await users.register_user(db_session, tg_id, "Покупатель")
    await users.set_phone(db_session, user.id, f"7900{tg_id:07d}")
    return user.id


def _slot(
    service_id: int, starts_in: datetime.timedelta, channel_msg_id: int
) -> models.Slot:
    return models.Slot(
        service_id=service_id, starts_at=_NOW + starts_in,
        start_price=1250, min_price=750, current_price=1250,
        published_at=_NOW - 20 * _HOUR, channel_msg_id=channel_msg_id,
    )


@pytest.fixture
async def world(session_factory: _SessionFactory) -> _World:
    """Одобренный бизнес: открытый слот с постом и слот с бронью."""
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
        open_slot = _slot(service.id, 5 * _HOUR, _POST_MESSAGE_ID)
        booked_slot = _slot(service.id, 6 * _HOUR, _BOOKED_POST_MESSAGE_ID)
        db_session.add_all([open_slot, booked_slot])
        await db_session.commit()
        buyer_id = await _register_buyer(db_session, _BUYER_TG_ID)
        result = await bookings.book_slot(
            db_session, buyer_id, booked_slot.id, _NOW
        )
    return _World(
        owner_id=owner.id, business_id=business.id, buyer_id=buyer_id,
        open_slot_id=open_slot.id, booking=result.details,
    )


async def _block_business(
    session_factory: _SessionFactory, business_id: int
) -> businesses.BlockedBusiness | None:
    async with session_factory() as db_session:
        return await businesses.block_business(db_session, business_id)


async def _unblock_business(
    session_factory: _SessionFactory, business_id: int
) -> models.Business | None:
    async with session_factory() as db_session:
        return await businesses.unblock_business(db_session, business_id)


async def _get(
    session_factory: _SessionFactory,
    model: type[models.Base],
    row_id: int,
) -> models.Base:
    async with session_factory() as db_session:
        return await db_session.get(model, row_id)


async def _second_buyer(session_factory: _SessionFactory) -> int:
    async with session_factory() as db_session:
        return await _register_buyer(db_session, _SECOND_BUYER_TG_ID)


async def _book(
    session_factory: _SessionFactory, buyer_id: int, slot_id: int
) -> bookings.BookResult:
    async with session_factory() as db_session:
        return await bookings.book_slot(db_session, buyer_id, slot_id, _NOW)


async def _event_types(session_factory: _SessionFactory) -> list[str]:
    async with session_factory() as db_session:
        return list(
            await db_session.scalars(
                sqlalchemy.select(models.Event.type).order_by(models.Event.id)
            )
        )


async def test_block_business_returns_open_slot_posts(
    session_factory: _SessionFactory, world: _World
) -> None:
    blocked = await _block_business(session_factory, world.business_id)
    assert blocked.channel_msg_ids == [_POST_MESSAGE_ID]


async def test_block_business_sets_blocked_status(
    session_factory: _SessionFactory, world: _World
) -> None:
    blocked = await _block_business(session_factory, world.business_id)
    assert blocked.business.status == models.BusinessStatus.BLOCKED


async def test_block_business_clears_open_slot_post_id(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    slot = await _get(session_factory, models.Slot, world.open_slot_id)
    assert slot.channel_msg_id is None


async def test_block_business_keeps_slot_statuses(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    open_slot = await _get(session_factory, models.Slot, world.open_slot_id)
    booked = await _get(session_factory, models.Slot, world.booking.slot_id)
    assert (open_slot.status, booked.status) == (
        models.SlotStatus.OPEN, models.SlotStatus.BOOKED,
    )


async def test_block_business_twice_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    assert await _block_business(session_factory, world.business_id) is None


async def test_block_business_unknown_id_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    assert await _block_business(session_factory, 404) is None


async def test_block_business_records_event(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    event_types = await _event_types(session_factory)
    assert event_types[-1] == models.EventType.BUSINESS_BLOCKED


async def test_book_slot_blocked_business_returns_unavailable(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    buyer_id = await _second_buyer(session_factory)
    result = await _book(session_factory, buyer_id, world.open_slot_id)
    assert result.outcome == bookings.BookOutcome.UNAVAILABLE


async def test_book_slot_blocked_business_leaves_slot_open(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    buyer_id = await _second_buyer(session_factory)
    await _book(session_factory, buyer_id, world.open_slot_id)
    slot = await _get(session_factory, models.Slot, world.open_slot_id)
    assert slot.status == models.SlotStatus.OPEN


async def test_quote_price_blocked_business_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    async with session_factory() as db_session:
        price = await bookings.quote_price(
            db_session, world.open_slot_id, _NOW
        )
    assert price is None


async def test_list_open_cards_hides_blocked_business_slots(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    async with session_factory() as db_session:
        cards = await feed.list_open_cards(db_session, _NOW, _CITY_TZ)
    assert cards == []


async def test_list_unposted_cards_hides_blocked_business_slots(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    async with session_factory() as db_session:
        cards = await feed.list_unposted_cards(db_session)
    assert cards == []


async def test_block_business_existing_booking_stays_active(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    booking = await _get(
        session_factory, models.Booking, world.booking.booking_id
    )
    assert booking.status == models.BookingStatus.ACTIVE


async def test_block_business_existing_booking_visit_recorded(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    async with session_factory() as db_session:
        result = await bookings.record_visit(
            db_session, world.owner_id, world.booking.booking_id, True,
            world.booking.starts_at + _HOUR,
        )
    assert result.outcome == bookings.VisitOutcome.RECORDED


async def test_unblock_business_sets_approved(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    business = await _unblock_business(session_factory, world.business_id)
    assert business.status == models.BusinessStatus.APPROVED


async def test_unblock_business_not_blocked_returns_none(
    session_factory: _SessionFactory, world: _World
) -> None:
    assert await _unblock_business(session_factory, world.business_id) is None


async def test_unblock_business_open_slot_waits_for_repost(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    await _unblock_business(session_factory, world.business_id)
    async with session_factory() as db_session:
        cards = await feed.list_unposted_cards(db_session)
    assert [card.slot_id for card in cards] == [world.open_slot_id]


async def test_unblock_business_records_event(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_business(session_factory, world.business_id)
    await _unblock_business(session_factory, world.business_id)
    event_types = await _event_types(session_factory)
    assert event_types[-1] == models.EventType.BUSINESS_UNBLOCKED


async def _block_user(session_factory: _SessionFactory, tg_id: int) -> bool:
    async with session_factory() as db_session:
        return await users.block_user(db_session, tg_id)


async def test_block_user_unknown_tg_id_returns_false(
    session_factory: _SessionFactory, world: _World
) -> None:
    assert not await _block_user(session_factory, _UNKNOWN_TG_ID)


async def test_block_user_sets_permanent_block(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_user(session_factory, _BUYER_TG_ID)
    buyer = await _get(session_factory, models.User, world.buyer_id)
    assert buyer.blocked_until == datetime.datetime(
        2100, 1, 1, tzinfo=datetime.UTC
    )


async def test_block_user_permanent_date_converts_to_city_time(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_user(session_factory, _BUYER_TG_ID)
    buyer = await _get(session_factory, models.User, world.buyer_id)
    assert buyer.blocked_until.astimezone(_CITY_TZ).year == 2100


async def test_block_user_after_delete_me_still_blocks(
    session_factory: _SessionFactory, world: _World
) -> None:
    buyer_id = await _second_buyer(session_factory)
    async with session_factory() as db_session:
        await users.delete_personal_data(db_session, buyer_id, _NOW)
    assert await _block_user(session_factory, _SECOND_BUYER_TG_ID)


async def test_book_slot_blocked_user_returns_blocked(
    session_factory: _SessionFactory, world: _World
) -> None:
    buyer_id = await _second_buyer(session_factory)
    await _block_user(session_factory, _SECOND_BUYER_TG_ID)
    result = await _book(session_factory, buyer_id, world.open_slot_id)
    assert result.outcome == bookings.BookOutcome.BLOCKED


async def test_unblock_user_clears_block(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_user(session_factory, _BUYER_TG_ID)
    async with session_factory() as db_session:
        await users.unblock_user(db_session, _BUYER_TG_ID)
    buyer = await _get(session_factory, models.User, world.buyer_id)
    assert buyer.blocked_until is None


async def test_block_and_unblock_user_record_events(
    session_factory: _SessionFactory, world: _World
) -> None:
    await _block_user(session_factory, _BUYER_TG_ID)
    async with session_factory() as db_session:
        await users.unblock_user(db_session, _BUYER_TG_ID)
    event_types = await _event_types(session_factory)
    assert event_types[-2:] == [
        models.EventType.USER_BLOCKED, models.EventType.USER_UNBLOCKED,
    ]


def test_is_permanent_block_admin_block_true() -> None:
    assert bookings.is_permanent_block(bookings.PERMANENT_BLOCK_UNTIL)


def test_is_permanent_block_no_show_block_false() -> None:
    assert not bookings.is_permanent_block(_NOW + bookings.BLOCK_PERIOD)


async def test_second_no_show_keeps_permanent_block(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.User)
            .where(models.User.id == world.buyer_id)
            .values(no_show_count=1)
        )
        await db_session.commit()
    await _block_user(session_factory, _BUYER_TG_ID)
    async with session_factory() as db_session:
        await bookings.record_visit(
            db_session, world.owner_id, world.booking.booking_id, False,
            world.booking.starts_at + _HOUR,
        )
    buyer = await _get(session_factory, models.User, world.buyer_id)
    assert buyer.blocked_until == bookings.PERMANENT_BLOCK_UNTIL


def _admin_command(text: str, tg_id: int = _ADMIN_TG_ID) -> types.Update:
    return types.Update(
        update_id=1,
        message=types.Message(
            message_id=1, date=datetime.datetime.now(datetime.UTC),
            chat=types.Chat(id=tg_id, type="private"),
            from_user=types.User(id=tg_id, is_bot=False, first_name="Админ"),
            text=text,
            entities=[
                types.MessageEntity(
                    type="bot_command", offset=0,
                    length=len(text.split()[0]),
                )
            ],
        ),
    )


async def _send_admin(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    text: str,
    tg_id: int = _ADMIN_TG_ID,
) -> conftest.FakeTelegramSession:
    fake_session = conftest.FakeTelegramSession()
    bot = aiogram.Bot("1:test", session=fake_session)
    await dispatcher.feed_update(
        bot, _admin_command(text, tg_id), session_factory=session_factory,
        city_tz=_CITY_TZ, admin_ids=frozenset({_ADMIN_TG_ID}),
        channel_config=channel.ChannelConfig(_CHANNEL_ID, _CITY_TZ),
    )
    return fake_session


async def _admin_reply(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    text: str,
) -> str:
    fake_session = await _send_admin(dispatcher, session_factory, text)
    [reply] = fake_session.sent_to(_ADMIN_TG_ID)
    return reply.text


@pytest.mark.parametrize(
    "text", ["/block", "/block user", "/block shop 5", "/block user abc"]
)
async def test_block_command_wrong_format_shows_example(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
    text: str,
) -> None:
    reply = await _admin_reply(dispatcher, session_factory, text)
    assert "Пример: /block user 123456789" in reply


async def test_unblock_command_wrong_format_shows_example(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    reply = await _admin_reply(dispatcher, session_factory, "/unblock")
    assert "Пример: /unblock user 123456789" in reply


async def test_block_command_unknown_user_says_not_found(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    reply = await _admin_reply(
        dispatcher, session_factory, f"/block user {_UNKNOWN_TG_ID}"
    )
    assert reply == f"Пользователь с Telegram ID {_UNKNOWN_TG_ID} не найден."


async def test_block_command_user_blocks_permanently(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    await _send_admin(
        dispatcher, session_factory, f"/block user {_BUYER_TG_ID}"
    )
    buyer = await _get(session_factory, models.User, world.buyer_id)
    assert bookings.is_permanent_block(buyer.blocked_until)


async def test_block_command_business_deletes_channel_post(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    fake_session = await _send_admin(
        dispatcher, session_factory, f"/block business {world.business_id}"
    )
    deleted = [
        (request.chat_id, request.message_id)
        for request in fake_session.requests
        if isinstance(request, methods.DeleteMessage)
    ]
    assert deleted == [(_CHANNEL_ID, _POST_MESSAGE_ID)]


async def test_block_command_business_reply_names_business(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    reply = await _admin_reply(
        dispatcher, session_factory, f"/block business {world.business_id}"
    )
    assert reply.startswith(
        f"Бизнес «Борода» (ID {world.business_id}) заблокирован."
    )


async def test_unblock_command_business_reposts_open_slot(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    await _block_business(session_factory, world.business_id)
    fake_session = await _send_admin(
        dispatcher, session_factory, f"/unblock business {world.business_id}"
    )
    assert len(fake_session.sent_to(_CHANNEL_ID)) == 1


async def test_block_command_from_non_admin_changes_nothing(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    await _send_admin(
        dispatcher, session_factory, f"/block business {world.business_id}",
        tg_id=_BUYER_TG_ID,
    )
    business = await _get(session_factory, models.Business, world.business_id)
    assert business.status == models.BusinessStatus.APPROVED

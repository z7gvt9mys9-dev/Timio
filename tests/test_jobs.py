"""Тесты планировщика: закрытие, цена, посты, уведомления (B4.6, B4.7, B9)."""

import collections.abc
import datetime
import pathlib
import zoneinfo

import pytest
from aiogram import exceptions
from aiogram import methods
from sqlalchemy.ext import asyncio as sa_asyncio

import conftest
import jobs
from bot import channel
from core import bookings
from core import businesses
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
_MakeBot = collections.abc.Callable[..., conftest.FakeBot]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
_CHANNEL_ID = -1001234567890
_CHANNEL = channel.ChannelConfig(channel_id=_CHANNEL_ID, city_tz=_CITY_TZ)
_OWNER_TG_ID = 100
_BLOCKED_TG_ID = 300
_POSTED_MESSAGE_ID = 77


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _create_service(
    session_factory: _SessionFactory, tg_id: int = _OWNER_TG_ID
) -> int:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, tg_id, "Иван")
        business = await businesses.create_business(
            db_session, user.id,
            businesses.BusinessInput(
                "Борода", "барбершоп", "Хамовники", "ул. Ленина, 1",
                "+7(900)123-45-67",
            ),
        )
        # Лента и бронь только для одобренного бизнеса (B4.9).
        await businesses.approve_business(db_session, business.id)
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Стрижка", 60, 1500),
        )
    return service.id


async def _insert_slot(
    session_factory: _SessionFactory,
    service_id: int,
    starts_in: datetime.timedelta,
    published_ago: datetime.timedelta = datetime.timedelta(hours=1),
    channel_msg_id: int | None = _POSTED_MESSAGE_ID,
) -> int:
    now = models.utc_now()
    async with session_factory() as db_session:
        slot = models.Slot(
            service_id=service_id, starts_at=now + starts_in,
            start_price=1250, min_price=750, current_price=1250,
            published_at=now - published_ago, channel_msg_id=channel_msg_id,
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


async def _load_slot(
    session_factory: _SessionFactory, slot_id: int
) -> models.Slot:
    async with session_factory() as db_session:
        return await db_session.get(models.Slot, slot_id)


def _owner_messages(bot: conftest.FakeBot) -> list[tuple[int, str]]:
    return [
        (message.chat_id, message.text)
        for message in bot.sent
        if message.chat_id != _CHANNEL_ID
    ]


async def _run(session_factory: _SessionFactory, bot: conftest.FakeBot) -> None:
    await jobs.run_slot_jobs(bot, session_factory, _CHANNEL)


async def test_run_slot_jobs_due_slot_expired(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    slot_id = await _insert_slot(
        session_factory, service_id, datetime.timedelta(minutes=20)
    )
    await _run(session_factory, make_fake_bot())
    slot = await _load_slot(session_factory, slot_id)
    assert slot.status == models.SlotStatus.EXPIRED


async def test_run_slot_jobs_due_slot_notifies_owner_in_city_time(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    slot_id = await _insert_slot(
        session_factory, service_id, datetime.timedelta(minutes=20)
    )
    bot = make_fake_bot()
    await _run(session_factory, bot)
    slot = await _load_slot(session_factory, slot_id)
    local_time = slot.starts_at.astimezone(_CITY_TZ).strftime("%d.%m %H:%M")
    assert _owner_messages(bot) == [
        (_OWNER_TG_ID, f"Слот «Стрижка» на {local_time} не продан и закрыт.")
    ]


async def test_run_slot_jobs_due_slot_post_marked_time_is_up(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    await _insert_slot(
        session_factory, service_id, datetime.timedelta(minutes=20)
    )
    bot = make_fake_bot()
    await _run(session_factory, bot)
    [edited] = bot.edited
    assert edited.text.startswith("Время вышло")


async def test_run_slot_jobs_due_slot_post_loses_button(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    await _insert_slot(
        session_factory, service_id, datetime.timedelta(minutes=20)
    )
    bot = make_fake_bot()
    await _run(session_factory, bot)
    [edited] = bot.edited
    assert edited.reply_markup is None


async def test_run_slot_jobs_far_slot_stays_open_without_notice(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    # Только что опубликован: цена ещё стартовая, править пост незачем.
    slot_id = await _insert_slot(
        session_factory, service_id, datetime.timedelta(hours=3),
        published_ago=datetime.timedelta(0),
    )
    bot = make_fake_bot()
    await _run(session_factory, bot)
    slot = await _load_slot(session_factory, slot_id)
    assert (slot.status, bot.sent, bot.edited) == (
        models.SlotStatus.OPEN, [], [],
    )


async def test_run_slot_jobs_updates_price_from_database(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    # Опубликован за 26 ч, прошло 13 ч: доля ≈ 0,46, 979 ₽ → 1000 ₽.
    service_id = await _create_service(session_factory)
    slot_id = await _insert_slot(
        session_factory, service_id,
        starts_in=datetime.timedelta(hours=13),
        published_ago=datetime.timedelta(hours=13, seconds=1),
    )
    await _run(session_factory, make_fake_bot())
    slot = await _load_slot(session_factory, slot_id)
    assert slot.current_price == 1000


async def test_run_slot_jobs_price_change_edits_post_with_button(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    slot_id = await _insert_slot(
        session_factory, service_id,
        starts_in=datetime.timedelta(hours=13),
        published_ago=datetime.timedelta(hours=13, seconds=1),
    )
    bot = make_fake_bot()
    await _run(session_factory, bot)
    [edited] = bot.edited
    [[button]] = edited.reply_markup.inline_keyboard
    assert ("Цена сейчас: 1 000 ₽" in edited.text, button.url) == (
        True,
        f"https://t.me/{conftest.FAKE_BOT_USERNAME}?start=slot_{slot_id}",
    )


async def test_run_slot_jobs_retries_unposted_open_slot(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    slot_id = await _insert_slot(
        session_factory, service_id, datetime.timedelta(hours=3),
        channel_msg_id=None,
    )
    bot = make_fake_bot()
    await _run(session_factory, bot)
    slot = await _load_slot(session_factory, slot_id)
    assert (len(bot.sent), slot.channel_msg_id) == (1, 1)


async def test_run_slot_jobs_channel_down_still_notifies_owner(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    await _insert_slot(
        session_factory, service_id, datetime.timedelta(minutes=20)
    )
    await _insert_slot(
        session_factory, service_id, datetime.timedelta(hours=3),
        channel_msg_id=None,
    )
    error = exceptions.TelegramForbiddenError(
        method=methods.EditMessageText(text="x"), message="Forbidden"
    )
    bot = make_fake_bot(
        blocked_chat_ids=frozenset({_CHANNEL_ID}), edit_error=error
    )
    await _run(session_factory, bot)
    assert [chat_id for chat_id, _ in _owner_messages(bot)] == [_OWNER_TG_ID]


async def test_run_slot_jobs_blocked_owner_does_not_stop_others(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    blocked_service_id = await _create_service(
        session_factory, _BLOCKED_TG_ID
    )
    service_id = await _create_service(session_factory)
    await _insert_slot(
        session_factory, blocked_service_id, datetime.timedelta(minutes=10)
    )
    await _insert_slot(
        session_factory, service_id, datetime.timedelta(minutes=20)
    )
    bot = make_fake_bot(blocked_chat_ids=frozenset({_BLOCKED_TG_ID}))
    await _run(session_factory, bot)
    assert [chat_id for chat_id, _ in _owner_messages(bot)] == [_OWNER_TG_ID]


async def test_create_scheduler_runs_every_5_minutes(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    scheduler = jobs.create_scheduler(
        make_fake_bot(), session_factory, _CHANNEL
    )
    job = scheduler.get_job(jobs.SLOT_JOBS_ID)
    assert job.trigger.interval == datetime.timedelta(minutes=5)


async def test_create_scheduler_first_run_immediately(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    before = datetime.datetime.now(datetime.UTC)
    scheduler = jobs.create_scheduler(
        make_fake_bot(), session_factory, _CHANNEL
    )
    job = scheduler.get_job(jobs.SLOT_JOBS_ID)
    assert job.next_run_time - before < datetime.timedelta(seconds=5)


_BUYER_TG_ID = 200
_SECOND_BUYER_TG_ID = 400


async def _booked(
    session_factory: _SessionFactory,
    starts_in: datetime.timedelta,
    buyer_tg_id: int = _BUYER_TG_ID,
    service_id: int | None = None,
) -> bookings.BookingDetails:
    # Бронь сделана за 3 часа до «сейчас»: напоминание «за 2 часа» ей
    # положено.
    if service_id is None:
        service_id = await _create_service(session_factory)
    slot_id = await _insert_slot(
        session_factory, service_id, starts_in,
        published_ago=datetime.timedelta(hours=5),
    )
    async with session_factory() as db_session:
        buyer = await users.register_user(db_session, buyer_tg_id, "Анна")
        await users.set_phone(db_session, buyer.id, f"7900{buyer_tg_id:07d}")
        result = await bookings.book_slot(
            db_session, buyer.id, slot_id,
            models.utc_now() - datetime.timedelta(hours=3),
        )
    return result.details


async def _run_bookings(
    session_factory: _SessionFactory, bot: conftest.FakeBot
) -> None:
    await jobs.run_booking_jobs(bot, session_factory, _CITY_TZ)


def _texts_to(bot: conftest.FakeBot, chat_id: int) -> list[str]:
    return [message.text for message in bot.sent if message.chat_id == chat_id]


async def test_run_booking_jobs_two_hour_reminder_with_contacts(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    await _booked(session_factory, datetime.timedelta(minutes=90))
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    [reminder] = _texts_to(bot, _BUYER_TG_ID)
    assert all(
        part in reminder
        for part in ("Напоминание о брони", "Адрес: ул. Ленина, 1",
                     "Телефон: +7(900)123-45-67", "Отменить можно до")
    )


async def test_run_booking_jobs_reminder_not_sent_twice(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    await _booked(session_factory, datetime.timedelta(minutes=90))
    await _run_bookings(session_factory, make_fake_bot())
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    assert bot.sent == []


async def test_run_booking_jobs_30_minute_reminder_without_cancel(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    await _booked(session_factory, datetime.timedelta(minutes=20))
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    [reminder] = _texts_to(bot, _BUYER_TG_ID)
    assert ("Напоминание о брони" in reminder,
            "Отменить можно" in reminder) == (True, False)


async def test_run_booking_jobs_asks_owner_with_visit_buttons(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    details = await _booked(session_factory, -datetime.timedelta(minutes=61))
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    [question] = [message for message in bot.sent
                  if message.chat_id == _OWNER_TG_ID]
    buttons = [button.callback_data
               for button in question.reply_markup.inline_keyboard[0]]
    assert (question.text.startswith("Клиент пришёл?"), buttons) == (
        True,
        [f"visit:came:{details.booking_id}",
         f"visit:noshow:{details.booking_id}"],
    )


async def test_run_booking_jobs_silent_owner_auto_confirmed(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    details = await _booked(session_factory, -datetime.timedelta(hours=26))
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    async with session_factory() as db_session:
        booking = await db_session.get(models.Booking, details.booking_id)
    assert (booking.status, bot.sent) == (models.BookingStatus.DONE, [])


async def test_run_booking_jobs_blocked_buyer_not_retried(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    await _booked(session_factory, datetime.timedelta(minutes=90))
    await _run_bookings(
        session_factory,
        make_fake_bot(blocked_chat_ids=frozenset({_BUYER_TG_ID})),
    )
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    assert bot.sent == []


async def test_run_booking_jobs_network_error_retried_next_run(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    await _booked(session_factory, datetime.timedelta(minutes=90))
    await _run_bookings(
        session_factory,
        make_fake_bot(network_error_chat_ids=frozenset({_BUYER_TG_ID})),
    )
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    assert len(_texts_to(bot, _BUYER_TG_ID)) == 1


@pytest.mark.parametrize("failure", ["blocked", "network"])
async def test_run_booking_jobs_one_failure_does_not_stop_others(
    session_factory: _SessionFactory,
    make_fake_bot: _MakeBot,
    failure: str,
) -> None:
    service_id = await _create_service(session_factory)
    for buyer_tg_id in (_BUYER_TG_ID, _SECOND_BUYER_TG_ID):
        await _booked(
            session_factory, datetime.timedelta(minutes=90), buyer_tg_id,
            service_id,
        )
    failing = frozenset({_BUYER_TG_ID})
    bot = make_fake_bot(
        blocked_chat_ids=failing if failure == "blocked" else frozenset(),
        network_error_chat_ids=(
            failing if failure == "network" else frozenset()
        ),
    )
    await _run_bookings(session_factory, bot)
    assert len(_texts_to(bot, _SECOND_BUYER_TG_ID)) == 1


async def test_run_slot_jobs_also_sends_booking_reminders(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    await _booked(session_factory, datetime.timedelta(minutes=90))
    bot = make_fake_bot()
    await _run(session_factory, bot)
    assert len(_texts_to(bot, _BUYER_TG_ID)) == 1


async def test_run_booking_jobs_texts_have_no_emoji_or_symbols(
    session_factory: _SessionFactory, make_fake_bot: _MakeBot
) -> None:
    service_id = await _create_service(session_factory)
    for starts_in, buyer_tg_id in (
        (datetime.timedelta(minutes=90), _BUYER_TG_ID),
        (-datetime.timedelta(minutes=61), _SECOND_BUYER_TG_ID),
    ):
        await _booked(session_factory, starts_in, buyer_tg_id, service_id)
    bot = make_fake_bot()
    await _run_bookings(session_factory, bot)
    assert bot.sent and not any(
        0x1F000 <= ord(char) <= 0x1FAFF or char in "\u2022\ufe0f"
        for message in bot.sent for char in message.text
    )

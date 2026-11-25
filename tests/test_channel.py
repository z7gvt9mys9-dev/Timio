"""Тесты постов в канале: отправка, правка, ошибки только в лог (B4.6, B9)."""

import collections.abc
import datetime
import logging
import pathlib
import zoneinfo

import pytest
from aiogram import exceptions
from aiogram import methods
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext import asyncio as sa_asyncio

import conftest
from bot import channel
from core import businesses
from core import feed
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
_MakeBot = collections.abc.Callable[..., conftest.FakeBot]

_CHANNEL_ID = -1001234567890
_CHANNEL = channel.ChannelConfig(
    channel_id=_CHANNEL_ID, city_tz=zoneinfo.ZoneInfo("Europe/Moscow")
)
_BUSINESS_NAME = "Барбершоп «Борода»"


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def service_id(session_factory: _SessionFactory) -> int:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, 100, "Иван")
        business = await businesses.create_business(
            db_session, user.id,
            businesses.BusinessInput(
                _BUSINESS_NAME, "барбершоп", "Хамовники", "ул. Ленина, 1",
                "+7(900)123-45-67",
            ),
        )
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Стрижка", 60, 1500),
        )
    return service.id


async def _insert_slot(
    session_factory: _SessionFactory,
    service_id: int,
    status: models.SlotStatus = models.SlotStatus.OPEN,
    channel_msg_id: int | None = None,
    hide_name: bool = False,
) -> int:
    now = models.utc_now()
    async with session_factory() as db_session:
        slot = models.Slot(
            service_id=service_id, starts_at=now + datetime.timedelta(hours=5),
            start_price=1250, min_price=750, current_price=1250,
            status=status, published_at=now, channel_msg_id=channel_msg_id,
            hide_name=hide_name,
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


async def _stored_message_id(
    session_factory: _SessionFactory, slot_id: int
) -> int | None:
    async with session_factory() as db_session:
        slot = await db_session.get(models.Slot, slot_id)
    return slot.channel_msg_id


async def test_publish_slots_sends_post_to_channel_with_button(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
) -> None:
    slot_id = await _insert_slot(session_factory, service_id)
    bot = make_fake_bot()
    await channel.publish_slots(bot, session_factory, _CHANNEL, [slot_id])
    [post] = bot.sent
    [[button]] = post.reply_markup.inline_keyboard
    assert (post.chat_id, button.url) == (
        _CHANNEL_ID,
        f"https://t.me/{conftest.FAKE_BOT_USERNAME}?start=slot_{slot_id}",
    )


async def test_publish_slots_stores_message_id(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
) -> None:
    slot_id = await _insert_slot(session_factory, service_id)
    await channel.publish_slots(
        make_fake_bot(), session_factory, _CHANNEL, [slot_id]
    )
    assert await _stored_message_id(session_factory, slot_id) == 1


async def test_publish_slots_hidden_name_not_in_post(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
) -> None:
    slot_id = await _insert_slot(session_factory, service_id, hide_name=True)
    bot = make_fake_bot()
    await channel.publish_slots(bot, session_factory, _CHANNEL, [slot_id])
    assert "Борода" not in bot.sent[0].text


async def test_publish_slots_skips_closed_and_already_posted(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
) -> None:
    withdrawn_id = await _insert_slot(
        session_factory, service_id, models.SlotStatus.WITHDRAWN
    )
    posted_id = await _insert_slot(
        session_factory, service_id, channel_msg_id=55
    )
    bot = make_fake_bot()
    await channel.publish_slots(
        bot, session_factory, _CHANNEL, [withdrawn_id, posted_id]
    )
    assert bot.sent == []


async def test_publish_slots_channel_down_logs_and_keeps_unposted(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slot_id = await _insert_slot(session_factory, service_id)
    bot = make_fake_bot(blocked_chat_ids=frozenset({_CHANNEL_ID}))
    with caplog.at_level(logging.WARNING):
        await channel.publish_slots(bot, session_factory, _CHANNEL, [slot_id])
    stored = await _stored_message_id(session_factory, slot_id)
    assert (stored, "не отправлен" in caplog.text) == (None, True)


async def test_publish_unposted_after_failed_save_sends_duplicate(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Дубль, о котором предупреждает код: пост ушёл, а ID не записался.
    slot_id = await _insert_slot(session_factory, service_id)
    bot = make_fake_bot()

    async def _fail_save(*args: object) -> None:
        raise sa_exc.OperationalError("UPDATE", {}, Exception("disk I/O"))

    with monkeypatch.context() as patch:
        patch.setattr(feed, "set_channel_msg_id", _fail_save)
        await channel.publish_slots(bot, session_factory, _CHANNEL, [slot_id])
    await channel.publish_unposted(bot, session_factory, _CHANNEL)
    assert len(bot.sent) == 2


async def test_refresh_posts_withdrawn_marked_without_button(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
) -> None:
    slot_id = await _insert_slot(
        session_factory, service_id, models.SlotStatus.WITHDRAWN, 55
    )
    bot = make_fake_bot()
    await channel.refresh_posts(bot, session_factory, _CHANNEL, [slot_id])
    [edited] = bot.edited
    assert (edited.message_id, edited.text.startswith("Слот снят"),
            edited.reply_markup) == (55, True, None)


async def test_refresh_posts_skips_slot_without_post(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
) -> None:
    slot_id = await _insert_slot(
        session_factory, service_id, models.SlotStatus.WITHDRAWN
    )
    bot = make_fake_bot()
    await channel.refresh_posts(bot, session_factory, _CHANNEL, [slot_id])
    assert bot.edited == []


async def test_refresh_posts_not_modified_is_silent(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slot_id = await _insert_slot(session_factory, service_id, channel_msg_id=55)
    error = exceptions.TelegramBadRequest(
        method=methods.EditMessageText(text="x"),
        message="Bad Request: message is not modified",
    )
    with caplog.at_level(logging.WARNING):
        await channel.refresh_posts(
            make_fake_bot(edit_error=error), session_factory, _CHANNEL,
            [slot_id],
        )
    assert caplog.text == ""


async def test_refresh_posts_deleted_post_logs_warning(
    session_factory: _SessionFactory,
    service_id: int,
    make_fake_bot: _MakeBot,
    caplog: pytest.LogCaptureFixture,
) -> None:
    slot_id = await _insert_slot(session_factory, service_id, channel_msg_id=55)
    error = exceptions.TelegramBadRequest(
        method=methods.EditMessageText(text="x"),
        message="Bad Request: message to edit not found",
    )
    with caplog.at_level(logging.WARNING):
        await channel.refresh_posts(
            make_fake_bot(edit_error=error), session_factory, _CHANNEL,
            [slot_id],
        )
    assert "не обновлён" in caplog.text

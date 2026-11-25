"""Тесты /slots: текст, кнопки, без согласия, скрытое название (B4.6)."""

import collections.abc
import dataclasses
import datetime
import pathlib
import re
import zoneinfo

import aiogram
import pytest
from aiogram import methods
from aiogram import types
from sqlalchemy.ext import asyncio as sa_asyncio

import conftest
from bot import keyboards
from bot.handlers import buyer
from core import businesses
from core import feed
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
_NOW = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.UTC)
_BUYER_TG_ID = 4242
_SYMBOLS = re.compile(
    "[•←-⇿⌀-➿■-◿⬀-⯿"
    "️\U0001f000-\U0001faff]"
)
_CARD = feed.SlotCard(
    slot_id=12, status=models.SlotStatus.OPEN,
    starts_at=datetime.datetime(2026, 12, 1, 15, 0, tzinfo=datetime.UTC),
    current_price=1250, regular_price=1500, hide_name=False,
    channel_msg_id=None, service_title="Мужская стрижка",
    category="барбершоп", business_name="Барбершоп «Борода»",
    district="Хамовники",
)
_HIDDEN_CARD = dataclasses.replace(_CARD, hide_name=True)


def _text(
    cards: list[feed.SlotCard],
    total: int | None = None,
    category: str | None = None,
) -> str:
    return buyer.build_slots_text(
        cards, len(cards) if total is None else total, category, _NOW,
        _CITY_TZ,
    )


def test_build_slots_text_hidden_name_not_revealed() -> None:
    assert "Борода" not in _text([_HIDDEN_CARD])


def test_build_slots_text_numbers_items_with_today_label() -> None:
    assert "\n\n1. Сегодня, 18:00 — Мужская стрижка" in _text([_CARD])


def test_build_slots_text_second_item_tomorrow_label() -> None:
    tomorrow = dataclasses.replace(
        _CARD, starts_at=_CARD.starts_at + datetime.timedelta(days=1)
    )
    assert "\n\n2. Завтра, 18:00" in _text([_CARD, tomorrow])


def test_build_slots_text_empty_says_no_slots() -> None:
    assert "Свободных слотов на сегодня и завтра нет." in _text([])


def test_build_slots_text_category_in_header() -> None:
    text = _text([_CARD], category="барбершоп")
    assert text.startswith("Слоты на сегодня и завтра, категория: барбершоп")


def test_build_slots_text_more_than_shown_adds_note() -> None:
    assert "Показаны первые 1 из 25." in _text([_CARD], total=25)


def test_build_slots_text_has_no_emoji_or_symbols() -> None:
    text = _text([_CARD, _HIDDEN_CARD], total=25, category="барбершоп")
    assert not _SYMBOLS.search(text)


def test_build_slots_list_keyboard_booking_buttons_numbered() -> None:
    markup = keyboards.build_slots_list_keyboard(["https://t.me/b"], [])
    [[button]] = markup.inline_keyboard
    assert (button.text, button.url) == (
        "1. Забронировать", "https://t.me/b",
    )


def test_build_slots_list_keyboard_categories_start_with_all() -> None:
    markup = keyboards.build_slots_list_keyboard([], ["барбершоп"])
    [row] = markup.inline_keyboard
    assert [(item.text, item.callback_data) for item in row] == [
        ("Все", "slots:cat:"), ("барбершоп", "slots:cat:барбершоп"),
    ]


def test_build_slots_list_keyboard_nothing_returns_none() -> None:
    assert keyboards.build_slots_list_keyboard([], []) is None


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _create_slot(
    session_factory: _SessionFactory,
    tg_id: int,
    category: str,
    hide_name: bool,
) -> int:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, tg_id, "Владелец")
        business = await businesses.create_business(
            db_session, user.id,
            businesses.BusinessInput(
                f"Бизнес{tg_id}", category, "Хамовники", "ул. Ленина, 1",
                "+7(900)123-45-67",
            ),
        )
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Услуга", 60, 1500),
        )
        now = models.utc_now()
        slot = models.Slot(
            service_id=service.id, starts_at=now + datetime.timedelta(hours=3),
            start_price=1250, min_price=750, current_price=1250,
            hide_name=hide_name, published_at=now,
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


def _slots_update() -> types.Update:
    text = "/slots"
    return types.Update(
        update_id=1,
        message=types.Message(
            message_id=1,
            date=datetime.datetime.now(),
            chat=types.Chat(id=_BUYER_TG_ID, type="private"),
            from_user=types.User(
                id=_BUYER_TG_ID, is_bot=False, first_name="Покупатель"
            ),
            text=text,
            entities=[
                types.MessageEntity(
                    type="bot_command", offset=0, length=len(text)
                )
            ],
        ),
    )


async def _send_slots(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> methods.SendMessage:
    fake_session = conftest.FakeTelegramSession()
    bot = aiogram.Bot("1:test", session=fake_session)
    await dispatcher.feed_update(
        bot, _slots_update(), session_factory=session_factory,
        city_tz=_CITY_TZ, admin_ids=frozenset(),
    )
    [reply] = [
        request for request in fake_session.requests
        if isinstance(request, methods.SendMessage)
    ]
    return reply


async def test_slots_command_without_consent_hides_name(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    await _create_slot(session_factory, 100, "барбершоп", hide_name=True)
    reply = await _send_slots(dispatcher, session_factory)
    async with session_factory() as db_session:
        buyer_user = await users.find_user(db_session, _BUYER_TG_ID)
    assert (buyer_user, "Бизнес100" in reply.text) == (None, False)


async def test_slots_command_shows_regular_name_and_booking_link(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(
        session_factory, 100, "барбершоп", hide_name=False
    )
    reply = await _send_slots(dispatcher, session_factory)
    booking_button = reply.reply_markup.inline_keyboard[0][0]
    assert ("Бизнес100" in reply.text, booking_button.url) == (
        True,
        f"https://t.me/{conftest.FAKE_BOT_USERNAME}?start=slot_{slot_id}",
    )


async def test_slots_command_offers_present_categories(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    await _create_slot(session_factory, 100, "фотостудия", hide_name=False)
    reply = await _send_slots(dispatcher, session_factory)
    category_row = reply.reply_markup.inline_keyboard[-1]
    assert [button.text for button in category_row] == ["Все", "фотостудия"]

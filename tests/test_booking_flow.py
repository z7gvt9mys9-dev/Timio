"""Тесты брони в боте: deep link, согласие, телефон, уведомления (B3.2)."""

import collections.abc
import dataclasses
import datetime
import itertools
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
from bot import keyboards
from core import bookings
from core import businesses
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
_CHANNEL_ID = -1001234567890
_CHANNEL = channel.ChannelConfig(channel_id=_CHANNEL_ID, city_tz=_CITY_TZ)
_OWNER_TG_ID = 100
_BUYER_TG_ID = 200
_OTHER_TG_ID = 300
_ADMIN_TG_ID = 900
_POST_MESSAGE_ID = 77
_BUSINESS_NAME = "Барбершоп Борода"
_ADDRESS = "ул. Ленина, 1"
_BUSINESS_PHONE = "+7(900)123-45-67"
_UPDATE_IDS = itertools.count(1)


@dataclasses.dataclass
class _Chat:
    """Бот, сеть и база для одного прогона сценария."""

    dispatcher: aiogram.Dispatcher
    session_factory: _SessionFactory
    network: conftest.FakeTelegramSession
    bot: aiogram.Bot

    async def send(self, update: types.Update) -> None:
        await self.dispatcher.feed_update(
            self.bot, update, session_factory=self.session_factory,
            city_tz=_CITY_TZ, channel_config=_CHANNEL,
            admin_ids=frozenset({_ADMIN_TG_ID}),
        )

    def replies(self, chat_id: int = _BUYER_TG_ID) -> list[str]:
        return [request.text for request in self.network.sent_to(chat_id)]


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
def chat(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> _Chat:
    network = conftest.FakeTelegramSession()
    return _Chat(
        dispatcher, session_factory, network,
        aiogram.Bot("1:test", session=network),
    )


async def _create_slot(
    session_factory: _SessionFactory,
    hide_name: bool = True,
    starts_in: datetime.timedelta = datetime.timedelta(hours=5),
) -> int:
    async with session_factory() as db_session:
        owner = await users.register_user(db_session, _OWNER_TG_ID, "Иван")
        # Повторный вызов добавляет слот к уже созданной услуге.
        business = await businesses.find_owner_business(db_session, owner.id)
        if business is None:
            business = await businesses.create_business(
                db_session, owner.id,
                businesses.BusinessInput(
                    _BUSINESS_NAME, "барбершоп", "Хамовники", _ADDRESS,
                    _BUSINESS_PHONE,
                ),
            )
            # Лента и бронь только для одобренного бизнеса (B4.9).
            await businesses.approve_business(db_session, business.id)
            await businesses.add_service(
                db_session, business.id,
                businesses.ServiceInput("Стрижка", 60, 1500),
            )
        [service] = await businesses.list_services(db_session, business.id)
        now = models.utc_now()
        slot = models.Slot(
            service_id=service.id, starts_at=now + starts_in,
            start_price=1250, min_price=750, current_price=1250,
            hide_name=hide_name, published_at=now,
            channel_msg_id=_POST_MESSAGE_ID,
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


async def _register(
    session_factory: _SessionFactory, tg_id: int, phone: str | None = None
) -> int:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, tg_id, "Покупатель")
        if phone is not None:
            await users.set_phone(db_session, user.id, phone)
    return user.id


def _user(tg_id: int) -> types.User:
    return types.User(id=tg_id, is_bot=False, first_name="Покупатель")


def _message(
    tg_id: int, text: str | None = None, contact: types.Contact | None = None
) -> types.Update:
    entities = None
    if text is not None and text.startswith("/"):
        entities = [
            types.MessageEntity(
                type="bot_command", offset=0, length=len(text.split()[0])
            )
        ]
    return types.Update(
        update_id=next(_UPDATE_IDS),
        message=types.Message(
            message_id=next(_UPDATE_IDS),
            date=datetime.datetime.now(datetime.UTC),
            chat=types.Chat(id=tg_id, type="private"),
            from_user=_user(tg_id), text=text, contact=contact,
            entities=entities,
        ),
    )


def _press(tg_id: int, data: str) -> types.Update:
    return types.Update(
        update_id=next(_UPDATE_IDS),
        callback_query=types.CallbackQuery(
            id=str(next(_UPDATE_IDS)), from_user=_user(tg_id),
            chat_instance="test", data=data,
            message=types.Message(
                message_id=1, date=datetime.datetime.now(datetime.UTC),
                chat=types.Chat(id=tg_id, type="private"), text="...",
            ),
        ),
    )


def _contact(contact_user_id: int) -> types.Contact:
    return types.Contact(
        phone_number="79005554433", first_name="Покупатель",
        user_id=contact_user_id,
    )


async def _bookings(session_factory: _SessionFactory) -> list[models.Booking]:
    async with session_factory() as db_session:
        return list(await db_session.scalars(sqlalchemy.select(models.Booking)))


async def test_deep_link_opens_this_slot_without_hidden_name(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID)
    await chat.send(_message(_BUYER_TG_ID, f"/start slot_{slot_id}"))
    [card] = chat.network.sent_to(_BUYER_TG_ID)
    [[button]] = card.reply_markup.inline_keyboard
    assert (_BUSINESS_NAME in card.text, _ADDRESS in card.text,
            button.callback_data) == (False, False, f"book:{slot_id}")


async def test_deep_link_records_slot_opened_event(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID)
    await chat.send(_message(_BUYER_TG_ID, f"/start slot_{slot_id}"))
    async with session_factory() as db_session:
        event = await db_session.scalar(
            sqlalchemy.select(models.Event).where(
                models.Event.type == models.EventType.SLOT_OPENED
            )
        )
    assert event.payload == {"slot_id": slot_id}


async def test_deep_link_new_user_gets_slot_after_consent(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await chat.send(_message(_BUYER_TG_ID, f"/start slot_{slot_id}"))
    await chat.send(_press(_BUYER_TG_ID, keyboards.CONSENT_CALLBACK))
    consent, card = chat.network.sent_to(_BUYER_TG_ID)
    assert ("согласие" in consent.text,
            card.reply_markup.inline_keyboard[0][0].callback_data) == (
        True, f"book:{slot_id}",
    )


async def test_deep_link_unknown_slot_says_unavailable(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    await _register(session_factory, _BUYER_TG_ID)
    await chat.send(_message(_BUYER_TG_ID, "/start slot_999"))
    assert chat.replies() == ["Слот уже недоступен. Другие слоты: /slots"]


async def test_book_without_phone_asks_contact_button(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID)
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    [request] = chat.network.sent_to(_BUYER_TG_ID)
    [[button]] = request.reply_markup.keyboard
    assert (button.request_contact, await _bookings(session_factory)) == (
        True, [],
    )


async def test_own_contact_saves_phone_and_books(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID)
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    await chat.send(_message(_BUYER_TG_ID, contact=_contact(_BUYER_TG_ID)))
    async with session_factory() as db_session:
        buyer = await users.find_user(db_session, _BUYER_TG_ID)
    [booking] = await _bookings(session_factory)
    assert (buyer.phone, booking.slot_id) == ("+7(900)555-44-33", slot_id)


async def test_foreign_contact_refused_without_booking(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID)
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    await chat.send(_message(_BUYER_TG_ID, contact=_contact(_OTHER_TG_ID)))
    assert (chat.replies()[-1], await _bookings(session_factory)) == (
        "Отправьте свой номер кнопкой «Отправить телефон».", [],
    )


async def test_booking_buyer_gets_name_address_and_phone(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    [confirmation] = chat.replies()
    assert all(
        part in confirmation
        for part in ("Слот забронирован.", _BUSINESS_NAME, _ADDRESS,
                     _BUSINESS_PHONE)
    )


async def test_booking_owner_gets_buyer_name_and_phone(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    [notice] = chat.replies(_OWNER_TG_ID)
    assert notice.startswith("Новая бронь.") and (
        "Покупатель, +7(900)555-44-33" in notice
    )


async def test_booking_marks_channel_post_booked_without_button(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    [edit] = [
        request for request in chat.network.requests
        if isinstance(request, methods.EditMessageText)
        and request.chat_id == _CHANNEL_ID
    ]
    assert (edit.message_id, edit.text.startswith("Забронировано"),
            edit.reply_markup) == (_POST_MESSAGE_ID, True, None)


async def test_booking_taken_slot_says_just_taken(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _OTHER_TG_ID, "79001110000")
    await chat.send(_press(_OTHER_TG_ID, f"book:{slot_id}"))
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    assert chat.replies() == ["Слот только что забрали. Другие слоты: /slots"]


async def test_booking_texts_have_no_emoji_or_symbols(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    await chat.send(_message(_BUYER_TG_ID, f"/start slot_{slot_id}"))
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    texts = chat.replies() + chat.replies(_OWNER_TG_ID)
    assert not any(
        0x1F000 <= ord(char) <= 0x1FAFF or char in "•️"
        for text in texts for char in text
    )


async def _book_via_bot(
    chat: _Chat,
    session_factory: _SessionFactory,
    starts_in: datetime.timedelta = datetime.timedelta(hours=5),
) -> models.Booking:
    slot_id = await _create_slot(session_factory, starts_in=starts_in)
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    [booking] = await _bookings(session_factory)
    chat.network.requests.clear()
    return booking


async def _cancel_via_bot(chat: _Chat, booking: models.Booking) -> None:
    await chat.send(_press(_BUYER_TG_ID, f"cancel:{booking.id}"))
    await chat.send(_press(_BUYER_TG_ID, f"cancelok:{booking.id}"))


def _callback_answers(chat: _Chat) -> list[str | None]:
    return [
        request.text for request in chat.network.requests
        if isinstance(request, methods.AnswerCallbackQuery)
    ]


def _edits(chat: _Chat, chat_id: int) -> list[methods.EditMessageText]:
    return [
        request for request in chat.network.requests
        if isinstance(request, methods.EditMessageText)
        and request.chat_id == chat_id
    ]


async def test_my_without_consent_says_no_bookings(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    await chat.send(_message(_BUYER_TG_ID, "/my"))
    async with session_factory() as db_session:
        user = await users.find_user(db_session, _BUYER_TG_ID)
    assert (chat.replies(), user) == (
        ["Броней пока нет. Свободные слоты: /slots"], None,
    )


async def test_my_lists_booking_with_address_and_cancel_button(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(_message(_BUYER_TG_ID, "/my"))
    [reply] = chat.network.sent_to(_BUYER_TG_ID)
    [[button]] = reply.reply_markup.inline_keyboard
    assert (_ADDRESS in reply.text, button.text, button.callback_data) == (
        True, "Отменить 1", f"cancel:{booking.id}",
    )


async def test_my_booking_within_hour_has_no_cancel_button(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    await _book_via_bot(
        chat, session_factory, starts_in=datetime.timedelta(minutes=50)
    )
    await chat.send(_message(_BUYER_TG_ID, "/my"))
    [reply] = chat.network.sent_to(_BUYER_TG_ID)
    assert (reply.reply_markup, "Отменить уже нельзя" in reply.text) == (
        None, True,
    )


async def test_cancel_returns_slot_to_feed(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _cancel_via_bot(chat, booking)
    async with session_factory() as db_session:
        slot = await db_session.get(models.Slot, booking.slot_id)
        stored = await db_session.get(models.Booking, booking.id)
    assert (slot.status, stored.status) == (
        models.SlotStatus.OPEN, models.BookingStatus.CANCELLED,
    )


async def test_cancel_notifies_owner(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _cancel_via_bot(chat, booking)
    [notice] = chat.replies(_OWNER_TG_ID)
    assert notice.startswith("Клиент отменил бронь.") and (
        "Слот снова в ленте." in notice
    )


async def test_cancel_post_gets_button_and_current_price(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _cancel_via_bot(chat, booking)
    [edit] = _edits(chat, _CHANNEL_ID)
    [[button]] = edit.reply_markup.inline_keyboard
    assert (edit.text.startswith("Стрижка"), "Цена сейчас:" in edit.text,
            button.url.endswith(f"start=slot_{booking.slot_id}")) == (
        True, True, True,
    )


async def test_cancel_updates_my_message(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _cancel_via_bot(chat, booking)
    assert _edits(chat, _BUYER_TG_ID)[-1].text == (
        "Броней пока нет. Свободные слоты: /slots"
    )


async def test_cancel_within_hour_refused_with_alert(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(
        chat, session_factory, starts_in=datetime.timedelta(minutes=50)
    )
    await chat.send(_press(_BUYER_TG_ID, f"cancel:{booking.id}"))
    assert _callback_answers(chat) == [
        "Отменить уже нельзя: до начала меньше часа."
    ]


async def test_cancel_foreign_booking_not_found(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _register(session_factory, _OTHER_TG_ID, "79001110000")
    await chat.send(_press(_OTHER_TG_ID, f"cancel:{booking.id}"))
    async with session_factory() as db_session:
        stored = await db_session.get(models.Booking, booking.id)
    assert (_callback_answers(chat), stored.status) == (
        ["Бронь не найдена или уже отменена."],
        models.BookingStatus.ACTIVE,
    )


async def test_my_and_cancel_texts_have_no_emoji_or_symbols(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(_message(_BUYER_TG_ID, "/my"))
    await _cancel_via_bot(chat, booking)
    texts = (
        chat.replies() + chat.replies(_OWNER_TG_ID)
        + [edit.text for edit in _edits(chat, _BUYER_TG_ID)]
    )
    assert not any(
        0x1F000 <= ord(char) <= 0x1FAFF or char in "\u2022\ufe0f"
        for text in texts for char in text
    )


async def test_cancel_asks_confirmation_and_keeps_booking(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(_press(_BUYER_TG_ID, f"cancel:{booking.id}"))
    [edit] = _edits(chat, _BUYER_TG_ID)
    buttons = [button.callback_data
               for button in edit.reply_markup.inline_keyboard[0]]
    async with session_factory() as db_session:
        stored = await db_session.get(models.Booking, booking.id)
    assert (edit.text.startswith("Точно отменить бронь?"), buttons,
            stored.status) == (
        True, [f"cancelok:{booking.id}", "cancelno"],
        models.BookingStatus.ACTIVE,
    )


async def test_cancel_answer_no_keeps_booking_and_restores_list(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(_press(_BUYER_TG_ID, f"cancel:{booking.id}"))
    await chat.send(_press(_BUYER_TG_ID, "cancelno"))
    async with session_factory() as db_session:
        stored = await db_session.get(models.Booking, booking.id)
    restored = _edits(chat, _BUYER_TG_ID)[-1]
    assert (restored.text.startswith("Ваши брони:"), stored.status,
            chat.replies(_OWNER_TG_ID)) == (
        True, models.BookingStatus.ACTIVE, [],
    )


async def test_cancel_confirm_by_other_buyer_not_found(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _register(session_factory, _OTHER_TG_ID, "79001110000")
    await chat.send(_press(_OTHER_TG_ID, f"cancelok:{booking.id}"))
    async with session_factory() as db_session:
        stored = await db_session.get(models.Booking, booking.id)
    assert (_callback_answers(chat), stored.status) == (
        ["Бронь не найдена или уже отменена."],
        models.BookingStatus.ACTIVE,
    )


async def _withdraw_via_bot(chat: _Chat, booking: models.Booking) -> None:
    await chat.send(
        _press(_OWNER_TG_ID, f"slot:withdrawbooked:{booking.slot_id}")
    )
    await chat.send(
        _press(_OWNER_TG_ID, f"slot:withdrawok:{booking.slot_id}")
    )


async def _load(
    session_factory: _SessionFactory, booking: models.Booking
) -> tuple[models.Slot, models.Booking]:
    async with session_factory() as db_session:
        return (
            await db_session.get(models.Slot, booking.slot_id),
            await db_session.get(models.Booking, booking.id),
        )


async def test_owner_slots_list_offers_withdraw_for_booked_slot(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(_press(_OWNER_TG_ID, keyboards.SLOTS_LIST_CALLBACK))
    [reply] = chat.network.sent_to(_OWNER_TG_ID)
    [[button]] = reply.reply_markup.inline_keyboard
    assert (button.text.endswith("(есть бронь)"), button.callback_data) == (
        True, f"slot:withdrawbooked:{booking.slot_id}",
    )


async def test_owner_withdraw_booked_asks_confirmation_first(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(
        _press(_OWNER_TG_ID, f"slot:withdrawbooked:{booking.slot_id}")
    )
    [edit] = _edits(chat, _OWNER_TG_ID)
    slot, _ = await _load(session_factory, booking)
    assert (edit.text.startswith("Точно снять слот?"), slot.status) == (
        True, models.SlotStatus.BOOKED,
    )


async def test_owner_withdraw_confirmed_slot_withdrawn_by_owner(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _withdraw_via_bot(chat, booking)
    slot, stored = await _load(session_factory, booking)
    assert (slot.status, stored.status, stored.cancelled_by) == (
        models.SlotStatus.WITHDRAWN, models.BookingStatus.CANCELLED,
        models.CancelledBy.OWNER,
    )


async def test_owner_withdraw_confirmed_notifies_buyer(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _withdraw_via_bot(chat, booking)
    [notice] = chat.replies(_BUYER_TG_ID)
    assert notice.startswith("Бизнес снял слот, ваша бронь отменена.")


async def test_owner_withdraw_confirmed_post_marked_withdrawn(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await _withdraw_via_bot(chat, booking)
    [edit] = _edits(chat, _CHANNEL_ID)
    assert (edit.text.startswith("Слот снят"), edit.reply_markup) == (
        True, None,
    )


async def test_owner_withdraw_answer_no_keeps_booking(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(
        _press(_OWNER_TG_ID, f"slot:withdrawbooked:{booking.slot_id}")
    )
    await chat.send(_press(_OWNER_TG_ID, "slot:withdrawno"))
    slot, _ = await _load(session_factory, booking)
    restored = _edits(chat, _OWNER_TG_ID)[-1]
    assert (slot.status, restored.text.startswith("Ближайшие слоты:"),
            chat.replies(_BUYER_TG_ID)) == (
        models.SlotStatus.BOOKED, True, [],
    )


async def test_owner_withdraw_after_buyer_cancelled_alerts_owner(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(
        _press(_OWNER_TG_ID, f"slot:withdrawbooked:{booking.slot_id}")
    )
    await _cancel_via_bot(chat, booking)
    chat.network.requests.clear()
    await chat.send(
        _press(_OWNER_TG_ID, f"slot:withdrawok:{booking.slot_id}")
    )
    slot, _ = await _load(session_factory, booking)
    assert (_callback_answers(chat), slot.status,
            chat.replies(_BUYER_TG_ID)) == (
        ["На слоте уже нет брони. Обновите список: «Слоты»."],
        models.SlotStatus.OPEN, [],
    )


async def test_owner_withdraw_by_stranger_keeps_booking(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(
        _press(_OTHER_TG_ID, f"slot:withdrawok:{booking.slot_id}")
    )
    slot, _ = await _load(session_factory, booking)
    assert slot.status == models.SlotStatus.BOOKED


async def test_owner_withdraw_texts_have_no_emoji_or_symbols(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _book_via_bot(chat, session_factory)
    await chat.send(_press(_OWNER_TG_ID, keyboards.SLOTS_LIST_CALLBACK))
    await _withdraw_via_bot(chat, booking)
    texts = (
        chat.replies(_OWNER_TG_ID) + chat.replies(_BUYER_TG_ID)
        + [edit.text for edit in _edits(chat, _OWNER_TG_ID)]
    )
    assert not any(
        0x1F000 <= ord(char) <= 0x1FAFF or char in "\u2022\ufe0f"
        for text in texts for char in text
    )


async def _book_slot_as(
    chat: _Chat,
    session_factory: _SessionFactory,
    buyer_tg_id: int,
    starts_in: datetime.timedelta,
) -> models.Booking:
    slot_id = await _create_slot(session_factory, starts_in=starts_in)
    await _register(
        session_factory, buyer_tg_id, f"7900{buyer_tg_id:07d}"
    )
    await chat.send(_press(buyer_tg_id, f"book:{slot_id}"))
    async with session_factory() as db_session:
        return await db_session.scalar(
            sqlalchemy.select(models.Booking).where(
                models.Booking.slot_id == slot_id
            )
        )


async def _started_booking(
    chat: _Chat, session_factory: _SessionFactory
) -> models.Booking:
    # Слот начался 2 часа назад: визит уже можно отметить.
    booking = await _book_slot_as(
        chat, session_factory, _BUYER_TG_ID, -datetime.timedelta(hours=2)
    )
    chat.network.requests.clear()
    return booking


async def _buyer(session_factory: _SessionFactory) -> models.User:
    async with session_factory() as db_session:
        return await users.find_user(db_session, _BUYER_TG_ID)


async def test_visit_came_marks_done_and_edits_message(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _started_booking(chat, session_factory)
    await chat.send(_press(_OWNER_TG_ID, f"visit:came:{booking.id}"))
    slot, stored = await _load(session_factory, booking)
    [edit] = _edits(chat, _OWNER_TG_ID)
    assert (stored.status, slot.status, edit.text.split("\n")[0],
            edit.reply_markup) == (
        models.BookingStatus.DONE, models.SlotStatus.DONE,
        "Отмечено: пришёл.", None,
    )


async def test_visit_first_no_show_marked_without_block(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _started_booking(chat, session_factory)
    await chat.send(_press(_OWNER_TG_ID, f"visit:noshow:{booking.id}"))
    [edit] = _edits(chat, _OWNER_TG_ID)
    buyer = await _buyer(session_factory)
    assert (edit.text.split("\n")[0], "заблокирован" in edit.text,
            "закрыто" in edit.text, buyer.no_show_count,
            buyer.blocked_until) == (
        "Отмечено: не пришёл.", False, False, 1, None,
    )


async def test_visit_second_no_show_shows_30_day_block(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    first = await _started_booking(chat, session_factory)
    second = await _started_booking(chat, session_factory)
    await chat.send(_press(_OWNER_TG_ID, f"visit:noshow:{first.id}"))
    await chat.send(_press(_OWNER_TG_ID, f"visit:noshow:{second.id}"))
    last_edit = _edits(chat, _OWNER_TG_ID)[-1]
    buyer = await _buyer(session_factory)
    blocked_date = buyer.blocked_until.astimezone(_CITY_TZ)
    assert (
        f"закрыто на 30 дней, до {blocked_date:%d.%m.%Y}." in last_edit.text,
        buyer.no_show_count,
    ) == (True, 0)


async def test_visit_repeated_press_changes_nothing(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _started_booking(chat, session_factory)
    await chat.send(_press(_OWNER_TG_ID, f"visit:came:{booking.id}"))
    chat.network.requests.clear()
    await chat.send(_press(_OWNER_TG_ID, f"visit:noshow:{booking.id}"))
    _, stored = await _load(session_factory, booking)
    buyer = await _buyer(session_factory)
    assert (_callback_answers(chat), _edits(chat, _OWNER_TG_ID),
            stored.status, buyer.no_show_count) == (
        ["Отметка уже стоит или бронь не найдена."], [],
        models.BookingStatus.DONE, 0,
    )


async def test_visit_press_by_stranger_changes_nothing(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _started_booking(chat, session_factory)
    await _register(session_factory, _OTHER_TG_ID, "79001110000")
    await chat.send(_press(_OTHER_TG_ID, f"visit:noshow:{booking.id}"))
    _, stored = await _load(session_factory, booking)
    assert (_callback_answers(chat), stored.status) == (
        ["Отметка уже стоит или бронь не найдена."],
        models.BookingStatus.ACTIVE,
    )


async def _withdraw_booked_slots(
    chat: _Chat, session_factory: _SessionFactory, count: int
) -> None:
    # У покупателя не больше 2 активных броней: чередуем двух.
    for index in range(count):
        buyer_tg_id = _BUYER_TG_ID if index % 2 == 0 else _OTHER_TG_ID
        booking = await _book_slot_as(
            chat, session_factory, buyer_tg_id,
            datetime.timedelta(hours=5 + index),
        )
        await _withdraw_via_bot(chat, booking)


async def test_owner_withdrawals_two_do_not_alert_admins(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    await _withdraw_booked_slots(chat, session_factory, 2)
    assert chat.replies(_ADMIN_TG_ID) == []


async def test_owner_withdrawals_third_alerts_admins(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    await _withdraw_booked_slots(chat, session_factory, 3)
    [alert] = chat.replies(_ADMIN_TG_ID)
    assert ("Всего таких снятий: 3." in alert, _BUSINESS_NAME in alert) == (
        True, True,
    )


async def test_owner_withdrawals_each_next_alerts_with_count(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    await _withdraw_booked_slots(chat, session_factory, 4)
    alerts = chat.replies(_ADMIN_TG_ID)
    assert [alert.split("\n")[1] for alert in alerts] == [
        "Всего таких снятий: 3.", "Всего таких снятий: 4.",
    ]


async def test_visit_and_alert_texts_have_no_emoji_or_symbols(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    booking = await _started_booking(chat, session_factory)
    await chat.send(_press(_OWNER_TG_ID, f"visit:came:{booking.id}"))
    await _withdraw_booked_slots(chat, session_factory, 3)
    texts = (
        [edit.text for edit in _edits(chat, _OWNER_TG_ID)]
        + chat.replies(_ADMIN_TG_ID)
    )
    assert texts and not any(
        0x1F000 <= ord(char) <= 0x1FAFF or char in "\u2022\ufe0f"
        for text in texts for char in text
    )


async def _block_owner_business(session_factory: _SessionFactory) -> None:
    async with session_factory() as db_session:
        owner = await users.find_user(db_session, _OWNER_TG_ID)
        business = await businesses.find_owner_business(db_session, owner.id)
        await businesses.block_business(db_session, business.id)


async def test_book_permanently_blocked_buyer_sees_no_date(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    async with session_factory() as db_session:
        await users.block_user(db_session, _BUYER_TG_ID)
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    assert chat.replies() == ["Бронирование недоступно."]


async def test_book_blocked_business_slot_says_unavailable(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _block_owner_business(session_factory)
    await _register(session_factory, _BUYER_TG_ID, "79005554433")
    await chat.send(_press(_BUYER_TG_ID, f"book:{slot_id}"))
    assert (chat.replies(), await _bookings(session_factory)) == (
        ["Слот уже недоступен. Другие слоты: /slots"], [],
    )


async def test_deep_link_blocked_business_slot_says_unavailable(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    slot_id = await _create_slot(session_factory)
    await _block_owner_business(session_factory)
    await _register(session_factory, _BUYER_TG_ID)
    await chat.send(_message(_BUYER_TG_ID, f"/start slot_{slot_id}"))
    assert chat.replies() == ["Слот уже недоступен. Другие слоты: /slots"]


async def test_slots_command_hides_blocked_business_slot(
    chat: _Chat, session_factory: _SessionFactory
) -> None:
    await _create_slot(session_factory, hide_name=False)
    await _block_owner_business(session_factory)
    await chat.send(_message(_BUYER_TG_ID, "/slots"))
    [reply] = chat.replies()
    assert _BUSINESS_NAME not in reply and "Стрижка" not in reply

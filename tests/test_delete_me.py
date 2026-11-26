"""Тесты /delete_me и текста согласия в боте (B5)."""

import collections.abc
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
from core import users
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
_TG_ID = 5151
_SYMBOLS = re.compile(
    "[•←-⇿⌀-➿■-◿⬀-⯿"
    "️\U0001f000-\U0001faff]"
)


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


def _tg_user() -> types.User:
    return types.User(id=_TG_ID, is_bot=False, first_name="Иван")


def _chat() -> types.Chat:
    return types.Chat(id=_TG_ID, type="private")


def _command_update(text: str) -> types.Update:
    command_length = len(text.split()[0])
    return types.Update(
        update_id=1,
        message=types.Message(
            message_id=1, date=datetime.datetime.now(datetime.UTC),
            chat=_chat(), from_user=_tg_user(), text=text,
            entities=[
                types.MessageEntity(
                    type="bot_command", offset=0, length=command_length
                )
            ],
        ),
    )


def _confirm_update() -> types.Update:
    return types.Update(
        update_id=2,
        callback_query=types.CallbackQuery(
            id="1", from_user=_tg_user(), chat_instance="1",
            data=keyboards.DELETE_ME_CALLBACK,
            message=types.Message(
                message_id=1, date=datetime.datetime.now(datetime.UTC),
                chat=_chat(), text="Удалить ваши данные?",
            ),
        ),
    )


async def _feed(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    update: types.Update,
) -> list[methods.SendMessage]:
    fake_session = conftest.FakeTelegramSession()
    bot = aiogram.Bot("1:test", session=fake_session)
    await dispatcher.feed_update(
        bot, update, session_factory=session_factory, city_tz=_CITY_TZ,
        admin_ids=frozenset(),
    )
    return fake_session.sent_to(_TG_ID)


async def _register(session_factory: _SessionFactory) -> None:
    async with session_factory() as db_session:
        await users.register_user(db_session, _TG_ID, "Иван")


async def test_consent_text_mentions_delete_me(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    [reply] = await _feed(
        dispatcher, session_factory, _command_update("/start")
    )
    assert "/delete_me" in reply.text


async def test_consent_text_says_only_telegram_id_stays(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    [reply] = await _feed(
        dispatcher, session_factory, _command_update("/start")
    )
    assert "останется только Telegram ID" in reply.text


async def test_delete_me_without_consent_says_no_data(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    [reply] = await _feed(
        dispatcher, session_factory, _command_update("/delete_me")
    )
    assert reply.text == "Ваших данных у нас нет."


async def test_delete_me_with_consent_asks_confirmation(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    await _register(session_factory)
    [reply] = await _feed(
        dispatcher, session_factory, _command_update("/delete_me")
    )
    [[button]] = reply.reply_markup.inline_keyboard
    assert button.callback_data == keyboards.DELETE_ME_CALLBACK


async def test_delete_me_confirm_removes_consent(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    await _register(session_factory)
    await _feed(dispatcher, session_factory, _confirm_update())
    async with session_factory() as db_session:
        found = await users.find_user(db_session, _TG_ID)
    assert found is None


async def test_delete_me_confirm_reports_done(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    await _register(session_factory)
    [reply] = await _feed(dispatcher, session_factory, _confirm_update())
    assert reply.text.startswith("Данные удалены.")


async def test_delete_me_texts_have_no_emoji_or_symbols(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    consent = await _feed(
        dispatcher, session_factory, _command_update("/start")
    )
    await _register(session_factory)
    confirm = await _feed(
        dispatcher, session_factory, _command_update("/delete_me")
    )
    done = await _feed(dispatcher, session_factory, _confirm_update())
    texts = [reply.text for reply in consent + confirm + done]
    assert not any(_SYMBOLS.search(text) for text in texts)

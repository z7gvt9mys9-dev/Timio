"""Тесты ленты: тексты постов и /slots, скрытое название, выборки (B4.6)."""

import collections.abc
import dataclasses
import datetime
import pathlib
import re
import zoneinfo

import pytest
from sqlalchemy.ext import asyncio as sa_asyncio

from core import businesses
from core import feed
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
# 12:00 по Москве, вторник 1 декабря.
_NOW = datetime.datetime(2026, 12, 1, 9, 0, tzinfo=datetime.UTC)
_BUSINESS_NAME = "Барбершоп «Борода»"
_ADDRESS = "ул. Ленина, 1"
# Эмодзи, стрелки, геометрические фигуры, маркеры списка (CLAUDE.md §3).
_SYMBOLS = re.compile(
    "[•←-⇿⌀-➿■-◿⬀-⯿"
    "️\U0001f000-\U0001faff]"
)
_CARD = feed.SlotCard(
    slot_id=12,
    status=models.SlotStatus.OPEN,
    # 18:00 по Москве.
    starts_at=datetime.datetime(2026, 12, 1, 15, 0, tzinfo=datetime.UTC),
    current_price=1250,
    regular_price=1500,
    hide_name=False,
    channel_msg_id=None,
    service_title="Мужская стрижка",
    category="барбершоп",
    business_name=_BUSINESS_NAME,
    district="Хамовники",
)
_HIDDEN_CARD = dataclasses.replace(_CARD, hide_name=True)


def _post(card: feed.SlotCard = _CARD) -> str:
    return feed.build_post_text(card, _CITY_TZ)


def _list_item(card: feed.SlotCard = _CARD) -> str:
    return feed.build_list_item_text(card, _NOW, _CITY_TZ)


def test_build_post_text_hidden_name_excludes_business_name() -> None:
    assert "Борода" not in _post(_HIDDEN_CARD)


def test_build_post_text_hidden_name_shows_category_and_district() -> None:
    assert "барбершоп, Хамовники" in _post(_HIDDEN_CARD)


def test_build_list_item_text_hidden_name_excludes_business_name() -> None:
    assert "Борода" not in _list_item(_HIDDEN_CARD)


def test_build_post_text_regular_shows_business_name() -> None:
    assert "Барбершоп «Борода», Хамовники" in _post()


def test_build_post_text_shows_current_and_struck_regular_price() -> None:
    assert "Цена сейчас: 1 250 ₽ (обычно <s>1 500 ₽</s>)" in _post()


def test_build_post_text_date_with_weekday_in_city_time() -> None:
    assert "вт, 01.12 в 18:00" in _post()


def test_build_post_text_open_slot_has_no_status_mark() -> None:
    assert _post().startswith("Мужская стрижка\n")


def test_build_post_text_open_slot_says_price_falls() -> None:
    assert "Цена снижается до начала. Слот один." in _post()


@pytest.mark.parametrize(
    ("status", "mark"),
    [
        (models.SlotStatus.BOOKED, "Забронировано"),
        (models.SlotStatus.DONE, "Забронировано"),
        (models.SlotStatus.NO_SHOW, "Забронировано"),
        (models.SlotStatus.EXPIRED, "Время вышло"),
        (models.SlotStatus.WITHDRAWN, "Слот снят"),
    ],
)
def test_build_post_text_closed_slot_starts_with_mark(
    status: models.SlotStatus, mark: str
) -> None:
    post = _post(dataclasses.replace(_CARD, status=status))
    assert post.startswith(f"{mark}\n\nМужская стрижка")


def test_build_post_text_closed_slot_drops_price_falls_line() -> None:
    post = _post(dataclasses.replace(_CARD, status=models.SlotStatus.BOOKED))
    assert "Цена снижается" not in post


def test_build_post_text_escapes_html_from_database() -> None:
    card = dataclasses.replace(_CARD, business_name="A<b>B</b>")
    assert "A&lt;b&gt;B&lt;/b&gt;" in _post(card)


@pytest.mark.parametrize("status", list(models.SlotStatus))
def test_build_post_text_has_no_emoji_or_symbols(
    status: models.SlotStatus,
) -> None:
    post = _post(dataclasses.replace(_CARD, status=status))
    assert not _SYMBOLS.search(post)


def test_build_list_item_text_has_no_emoji_or_symbols() -> None:
    assert not _SYMBOLS.search(_list_item())


def test_build_list_item_text_today_label() -> None:
    assert _list_item().startswith("Сегодня, 18:00 — Мужская стрижка")


def test_build_list_item_text_tomorrow_label() -> None:
    card = dataclasses.replace(
        _CARD, starts_at=_CARD.starts_at + datetime.timedelta(days=1)
    )
    assert _list_item(card).startswith("Завтра, 18:00")


def test_build_deep_link_payload_has_slot_prefix() -> None:
    assert feed.build_deep_link_payload(12) == "slot_12"


@pytest.mark.parametrize(
    ("status", "is_bookable"),
    [(models.SlotStatus.OPEN, True), (models.SlotStatus.BOOKED, False)],
)
def test_is_bookable_only_open(
    status: models.SlotStatus, is_bookable: bool
) -> None:
    card = dataclasses.replace(_CARD, status=status)
    assert feed.is_bookable(card) == is_bookable


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _create_service(
    session_factory: _SessionFactory, tg_id: int, category: str
) -> int:
    async with session_factory() as db_session:
        user = await users.register_user(db_session, tg_id, "Иван")
        business = await businesses.create_business(
            db_session, user.id,
            businesses.BusinessInput(
                _BUSINESS_NAME, category, "Хамовники", _ADDRESS,
                "+7(900)123-45-67",
            ),
        )
        # Лента и бронь только для одобренного бизнеса (B4.9).
        await businesses.approve_business(db_session, business.id)
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Мужская стрижка", 60, 1500),
        )
    return service.id


@pytest.fixture
async def service_id(session_factory: _SessionFactory) -> int:
    return await _create_service(session_factory, 100, "барбершоп")


async def _insert_slot(
    session_factory: _SessionFactory,
    service_id: int,
    starts_at: datetime.datetime,
    status: models.SlotStatus = models.SlotStatus.OPEN,
    hide_name: bool = False,
) -> int:
    async with session_factory() as db_session:
        slot = models.Slot(
            service_id=service_id, starts_at=starts_at, start_price=1250,
            min_price=750, current_price=1250, hide_name=hide_name,
            status=status, published_at=_NOW - datetime.timedelta(hours=1),
        )
        db_session.add(slot)
        await db_session.commit()
        return slot.id


def _local(day: int, hour: int) -> datetime.datetime:
    return datetime.datetime(2026, 12, day, hour, 0, tzinfo=_CITY_TZ)


async def test_hidden_slot_from_database_hides_name_and_address(
    session_factory: _SessionFactory, service_id: int
) -> None:
    slot_id = await _insert_slot(
        session_factory, service_id, _local(1, 18), hide_name=True
    )
    async with session_factory() as db_session:
        [card] = await feed.load_cards(db_session, [slot_id])
    texts = _post(card) + _list_item(card)
    assert "Борода" not in texts and _ADDRESS not in texts


async def test_list_open_cards_today_and_tomorrow_only(
    session_factory: _SessionFactory, service_id: int
) -> None:
    today_id = await _insert_slot(session_factory, service_id, _local(1, 18))
    tomorrow_id = await _insert_slot(
        session_factory, service_id, _local(2, 23)
    )
    await _insert_slot(session_factory, service_id, _local(3, 0))
    await _insert_slot(session_factory, service_id, _local(1, 11))
    async with session_factory() as db_session:
        cards = await feed.list_open_cards(db_session, _NOW, _CITY_TZ)
    assert [card.slot_id for card in cards] == [today_id, tomorrow_id]


async def test_list_open_cards_skips_not_open(
    session_factory: _SessionFactory, service_id: int
) -> None:
    for status in (models.SlotStatus.BOOKED, models.SlotStatus.WITHDRAWN):
        await _insert_slot(session_factory, service_id, _local(1, 18), status)
    async with session_factory() as db_session:
        cards = await feed.list_open_cards(db_session, _NOW, _CITY_TZ)
    assert cards == []


async def test_list_open_cards_filters_by_category(
    session_factory: _SessionFactory, service_id: int
) -> None:
    studio_service_id = await _create_service(
        session_factory, 200, "фотостудия"
    )
    await _insert_slot(session_factory, service_id, _local(1, 18))
    studio_id = await _insert_slot(
        session_factory, studio_service_id, _local(1, 19)
    )
    async with session_factory() as db_session:
        cards = await feed.list_open_cards(
            db_session, _NOW, _CITY_TZ, category="фотостудия"
        )
    assert [card.slot_id for card in cards] == [studio_id]


async def test_list_unposted_cards_open_without_message(
    session_factory: _SessionFactory, service_id: int
) -> None:
    unposted_id = await _insert_slot(
        session_factory, service_id, _local(1, 18)
    )
    posted_id = await _insert_slot(session_factory, service_id, _local(1, 19))
    await _insert_slot(
        session_factory, service_id, _local(1, 20), models.SlotStatus.EXPIRED
    )
    async with session_factory() as db_session:
        await feed.set_channel_msg_id(db_session, posted_id, 555)
        cards = await feed.list_unposted_cards(db_session)
    assert [card.slot_id for card in cards] == [unposted_id]


async def test_set_channel_msg_id_stored_in_card(
    session_factory: _SessionFactory, service_id: int
) -> None:
    slot_id = await _insert_slot(session_factory, service_id, _local(1, 18))
    async with session_factory() as db_session:
        await feed.set_channel_msg_id(db_session, slot_id, 555)
        [card] = await feed.load_cards(db_session, [slot_id])
    assert card.channel_msg_id == 555


async def test_load_cards_empty_ids_returns_empty(
    session_factory: _SessionFactory,
) -> None:
    async with session_factory() as db_session:
        assert await feed.load_cards(db_session, []) == []


def test_format_slot_time_weekday_date_and_city_time() -> None:
    assert feed.format_slot_time(_CARD.starts_at, _CITY_TZ) == (
        "вт, 01.12 в 18:00"
    )

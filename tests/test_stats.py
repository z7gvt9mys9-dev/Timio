"""Тесты статистики и выгрузки для команды: /stats и /export (B4.9)."""

import collections.abc
import csv
import dataclasses
import datetime
import io
import pathlib
import re
import zoneinfo

import aiogram
import pytest
import sqlalchemy
from aiogram import methods
from aiogram import types
from sqlalchemy.ext import asyncio as sa_asyncio

import conftest
from core import businesses
from core import reports
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
_NOW = datetime.datetime(2026, 11, 27, 12, 0, tzinfo=datetime.UTC)
_RECENT = _NOW - datetime.timedelta(days=1)
_OLD = _NOW - datetime.timedelta(days=30)
_HOUR = datetime.timedelta(hours=1)
_ADMIN_TG_ID = 900
_OWNER_TG_ID = 100
_BUYER_TG_ID = 200
_OLD_BUYER_TG_ID = 201
_BUYER_PHONE = "+7(911)222-33-44"
_UTF8_BOM = b"\xef\xbb\xbf"
_SYMBOLS = re.compile(
    "[•←-⇿⌀-➿■-◿⬀-⯿"
    "️\U0001f000-\U0001faff]"
)


@dataclasses.dataclass(frozen=True)
class _World:
    owner_id: int
    service_id: int
    buyer_id: int
    old_buyer_id: int


@pytest.fixture
async def session_factory(
    tmp_path: pathlib.Path,
) -> collections.abc.AsyncIterator[_SessionFactory]:
    engine = session.create_engine(tmp_path / "test.db")
    await session.create_tables(engine)
    yield session.create_session_factory(engine)
    await engine.dispose()


async def _add_slot(
    session_factory: _SessionFactory,
    world: _World,
    starts_at: datetime.datetime,
    status: models.SlotStatus,
    booking: tuple[int, models.BookingStatus, int] | None = None,
) -> None:
    async with session_factory() as db_session:
        slot = models.Slot(
            service_id=world.service_id, starts_at=starts_at,
            start_price=1250, min_price=750, current_price=900,
            status=status, published_at=starts_at - _HOUR,
        )
        db_session.add(slot)
        await db_session.flush()
        if booking is not None:
            buyer_id, booking_status, price = booking
            db_session.add(
                models.Booking(
                    slot_id=slot.id, buyer_id=buyer_id, price=price,
                    status=booking_status, created_at=starts_at - _HOUR,
                )
            )
        await db_session.commit()


async def _add_calc(
    session_factory: _SessionFactory,
    user_id: int | None,
    created_at: datetime.datetime,
) -> None:
    async with session_factory() as db_session:
        db_session.add(
            models.CalcReport(
                user_id=user_id, source=models.CalcSource.BOT, seats=1,
                hours_per_day=10, days_per_month=26, load_pct=60,
                revenue_per_hour=1500, variable_cost=600,
                lost_revenue=1000, recoverable=0, created_at=created_at,
            )
        )
        await db_session.commit()


async def _create_world(session_factory: _SessionFactory) -> _World:
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
        buyer = await users.register_user(db_session, _BUYER_TG_ID, "Анна")
        buyer.phone = _BUYER_PHONE
        old_buyer = await users.register_user(
            db_session, _OLD_BUYER_TG_ID, "Пётр"
        )
        await db_session.commit()
    return _World(owner.id, service.id, buyer.id, old_buyer.id)


@pytest.fixture
async def world(session_factory: _SessionFactory) -> _World:
    """Слоты за последние 7 дней и месяц назад, расчёты калькулятора."""
    created = await _create_world(session_factory)
    slot = models.SlotStatus
    booking = models.BookingStatus
    buyer_id = created.buyer_id
    await _add_slot(
        session_factory, created, _RECENT, slot.DONE,
        (buyer_id, booking.DONE, 900),
    )
    await _add_slot(
        session_factory, created, _RECENT, slot.NO_SHOW,
        (buyer_id, booking.NO_SHOW, 800),
    )
    await _add_slot(session_factory, created, _RECENT, slot.EXPIRED)
    await _add_slot(session_factory, created, _RECENT, slot.WITHDRAWN)
    await _add_slot(
        session_factory, created, _OLD, slot.DONE,
        (created.old_buyer_id, booking.DONE, 600),
    )
    await _add_slot(
        session_factory, created, _OLD, slot.OPEN,
        (buyer_id, booking.CANCELLED, 1100),
    )
    # Владелец посчитал потери до первого слота, покупатель слотов
    # не выставлял, расчёт с сайта ни к кому не привязан.
    await _add_calc(session_factory, created.owner_id, _OLD - _HOUR * 5)
    await _add_calc(session_factory, buyer_id, _RECENT)
    await _add_calc(session_factory, None, _RECENT)
    return created


async def _stats(session_factory: _SessionFactory) -> reports.TeamStats:
    async with session_factory() as db_session:
        return await reports.build_team_stats(db_session, _NOW)


async def test_stats_published_excludes_withdrawn(
    session_factory: _SessionFactory, world: _World
) -> None:
    stats = await _stats(session_factory)
    assert (stats.all_time.published, stats.last_days.published) == (5, 3)


async def test_stats_sold_share_of_published(
    session_factory: _SessionFactory, world: _World
) -> None:
    stats = await _stats(session_factory)
    assert (stats.all_time.sold_pct, stats.last_days.sold_pct) == (40, 33)


async def test_stats_no_show_share_of_marked_visits(
    session_factory: _SessionFactory, world: _World
) -> None:
    # Всё время: 1 неявка из 3 визитов, 7 дней: 1 из 2.
    stats = await _stats(session_factory)
    assert (stats.all_time.no_show_pct, stats.last_days.no_show_pct) == (
        33, 50,
    )


async def test_stats_returned_sums_done_bookings(
    session_factory: _SessionFactory, world: _World
) -> None:
    stats = await _stats(session_factory)
    assert (stats.all_time.returned, stats.last_days.returned) == (1500, 900)


async def test_stats_average_discount_at_purchase(
    session_factory: _SessionFactory, world: _World
) -> None:
    # 900 и 600 от 1500: скидки 40% и 60%.
    stats = await _stats(session_factory)
    assert (
        stats.all_time.average_discount_pct,
        stats.last_days.average_discount_pct,
    ) == (50, 40)


async def test_stats_calc_conversion_all_time_counts_owner_with_slot(
    session_factory: _SessionFactory, world: _World
) -> None:
    stats = await _stats(session_factory)
    assert (
        stats.all_time.calc_owners_with_slot, stats.all_time.calc_owners,
    ) == (1, 2)


async def test_stats_calc_conversion_last_days_only_recent_calcs(
    session_factory: _SessionFactory, world: _World
) -> None:
    stats = await _stats(session_factory)
    assert stats.last_days.calc_conversion_pct == 0


async def test_stats_calc_conversion_ignores_slots_before_calc(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.CalcReport)
            .where(models.CalcReport.user_id == world.owner_id)
            .values(created_at=_NOW - _HOUR)
        )
        await db_session.commit()
    stats = await _stats(session_factory)
    assert stats.all_time.calc_owners_with_slot == 0


async def test_stats_active_buyers_only_last_30_days(
    session_factory: _SessionFactory, world: _World
) -> None:
    assert (await _stats(session_factory)).active_buyers == 1


async def test_stats_active_businesses_only_approved(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        await businesses.create_business(
            db_session, world.buyer_id,
            businesses.BusinessInput(
                "Ножницы", "барбершоп", "Центр", "ул. Мира, 2",
                "+7(900)765-43-21",
            ),
        )
    assert (await _stats(session_factory)).active_businesses == 1


async def test_stats_empty_base_has_no_shares(
    session_factory: _SessionFactory,
) -> None:
    stats = await _stats(session_factory)
    assert (
        stats.all_time.sold_pct, stats.all_time.no_show_pct,
        stats.all_time.calc_conversion_pct,
    ) == (None, None, None)


async def test_format_team_stats_lists_both_periods(
    session_factory: _SessionFactory, world: _World
) -> None:
    text = reports.format_team_stats(await _stats(session_factory))
    assert text.split("\n")[2:10] == [
        "За всё время",
        "- Опубликовано слотов: 5",
        "- Продано: 2 из 5 (40%)",
        "- Неявки: 1 (33% от отмеченных визитов, их 3)",
        "- Возвращено денег: 1 500 ₽",
        "- Средняя скидка при покупке: 50%",
        "- Выставили слот после калькулятора: 1 из 2 (50%)",
        "",
    ]


async def test_format_team_stats_empty_base_says_no_data(
    session_factory: _SessionFactory,
) -> None:
    text = reports.format_team_stats(await _stats(session_factory))
    assert "- Продано: 0 из 0 (нет данных)" in text


async def test_format_team_stats_has_no_emoji_or_symbols(
    session_factory: _SessionFactory, world: _World
) -> None:
    text = reports.format_team_stats(await _stats(session_factory))
    assert not _SYMBOLS.search(text)


def _csv_rows(content: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))


async def test_export_slots_csv_starts_with_bom(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        content = await reports.export_slots_csv(db_session)
    assert content.startswith(_UTF8_BOM)


async def test_export_slots_csv_has_all_slots(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        rows = _csv_rows(await reports.export_slots_csv(db_session))
    assert (rows[0][:3], len(rows) - 1) == (
        ["id", "service_id", "starts_at"], 6,
    )


async def test_export_bookings_csv_columns_without_personal_data(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        rows = _csv_rows(await reports.export_bookings_csv(db_session))
    assert rows[0] == [
        "id", "slot_id", "buyer_id", "price", "status", "created_at",
        "cancelled_at", "cancelled_by",
    ]


async def test_export_bookings_csv_has_no_phone_name_or_tg_id(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        content = await reports.export_bookings_csv(db_session)
    text = content.decode("utf-8-sig")
    assert not any(
        value in text for value in (_BUYER_PHONE, "Анна", str(_BUYER_TG_ID))
    )


async def test_export_bookings_csv_times_in_utc_iso(
    session_factory: _SessionFactory, world: _World
) -> None:
    async with session_factory() as db_session:
        rows = _csv_rows(await reports.export_bookings_csv(db_session))
    assert rows[1][5] == (_RECENT - _HOUR).isoformat()


def _command(text: str, tg_id: int) -> types.Update:
    return types.Update(
        update_id=1,
        message=types.Message(
            message_id=1, date=datetime.datetime.now(datetime.UTC),
            chat=types.Chat(id=tg_id, type="private"),
            from_user=types.User(id=tg_id, is_bot=False, first_name="Админ"),
            text=text,
            entities=[
                types.MessageEntity(
                    type="bot_command", offset=0, length=len(text)
                )
            ],
        ),
    )


async def _send(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    text: str,
    tg_id: int = _ADMIN_TG_ID,
) -> conftest.FakeTelegramSession:
    fake_session = conftest.FakeTelegramSession()
    bot = aiogram.Bot("1:test", session=fake_session)
    await dispatcher.feed_update(
        bot, _command(text, tg_id), session_factory=session_factory,
        city_tz=_CITY_TZ, admin_ids=frozenset({_ADMIN_TG_ID}),
    )
    return fake_session


def _documents(
    fake_session: conftest.FakeTelegramSession,
) -> list[methods.SendDocument]:
    return [
        request for request in fake_session.requests
        if isinstance(request, methods.SendDocument)
    ]


async def test_stats_command_admin_gets_stats(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    fake_session = await _send(dispatcher, session_factory, "/stats")
    [reply] = fake_session.sent_to(_ADMIN_TG_ID)
    assert reply.text.startswith("Статистика SlotMarket")


async def test_stats_command_non_admin_gets_no_stats(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    fake_session = await _send(
        dispatcher, session_factory, "/stats", _OWNER_TG_ID
    )
    assert not any(
        request.text.startswith("Статистика")
        for request in fake_session.sent_to(_OWNER_TG_ID)
    )


async def test_export_command_admin_gets_two_csv_files(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    fake_session = await _send(dispatcher, session_factory, "/export")
    assert [
        document.document.filename for document in _documents(fake_session)
    ] == ["slots.csv", "bookings.csv"]


async def test_export_command_non_admin_gets_no_files(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    world: _World,
) -> None:
    fake_session = await _send(
        dispatcher, session_factory, "/export", _OWNER_TG_ID
    )
    assert _documents(fake_session) == []

"""Тесты отчёта владельцу и недельной рассылки (B4.8)."""

import collections.abc
import dataclasses
import datetime
import pathlib
import re
import zoneinfo

import aiogram
import pytest
import sqlalchemy
from aiogram import types
from sqlalchemy.ext import asyncio as sa_asyncio

import conftest
import jobs
from core import businesses
from core import reports
from core import users
from db import models
from db import session

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
_MakeBot = collections.abc.Callable[..., conftest.FakeBot]

_CITY_TZ = zoneinfo.ZoneInfo("Europe/Moscow")
_OWNER_TG_ID = 100
_OTHER_OWNER_TG_ID = 101
_BUYER_TG_ID = 200
# Понедельник 30.11.2026, 10:05 по Москве. Прошлая неделя — 23–29.11.
_MONDAY_SEND = datetime.datetime(2026, 11, 30, 7, 5, tzinfo=datetime.UTC)
_WEEK_START = datetime.datetime(2026, 11, 23, tzinfo=_CITY_TZ)
_WEEK_END = datetime.datetime(2026, 11, 30, tzinfo=_CITY_TZ)
_WEEK = reports.ReportPeriod(start=_WEEK_START, end=_WEEK_END)
_IN_WEEK = datetime.datetime(2026, 11, 25, 15, 0, tzinfo=_CITY_TZ)
_REGISTERED_AT = datetime.datetime(2026, 11, 1, tzinfo=datetime.UTC)
_SYMBOLS = re.compile(
    "[•←-⇿⌀-➿■-◿⬀-⯿"
    "️\U0001f000-\U0001faff]"
)


@dataclasses.dataclass(frozen=True)
class _Owner:
    owner_id: int
    business_id: int
    service_id: int
    buyer_id: int


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
        owner = await users.register_user(db_session, tg_id, "Иван")
        business = await businesses.create_business(
            db_session, owner.id,
            businesses.BusinessInput(
                f"Борода{tg_id}", "барбершоп", "Хамовники", "ул. Ленина, 1",
                "+7(900)123-45-67",
            ),
        )
        service = await businesses.add_service(
            db_session, business.id,
            businesses.ServiceInput("Стрижка", 60, 1500),
        )
        if is_approved:
            await businesses.approve_business(db_session, business.id)
        # Бизнес зарегистрирован до отчётной недели.
        await db_session.execute(
            sqlalchemy.update(models.Business)
            .where(models.Business.id == business.id)
            .values(created_at=_REGISTERED_AT)
        )
        buyer = await users.register_user(db_session, _BUYER_TG_ID, "Анна")
        await db_session.commit()
    return _Owner(owner.id, business.id, service.id, buyer.id)


async def _add_slot(
    session_factory: _SessionFactory,
    owner: _Owner,
    status: models.SlotStatus,
    booking: tuple[models.BookingStatus, int] | None = None,
    starts_at: datetime.datetime = _IN_WEEK,
) -> None:
    async with session_factory() as db_session:
        slot = models.Slot(
            service_id=owner.service_id, starts_at=starts_at,
            start_price=1250, min_price=750, current_price=900,
            status=status, published_at=starts_at - datetime.timedelta(1),
        )
        db_session.add(slot)
        await db_session.flush()
        if booking is not None:
            booking_status, price = booking
            db_session.add(
                models.Booking(
                    slot_id=slot.id, buyer_id=owner.buyer_id, price=price,
                    status=booking_status,
                )
            )
        await db_session.commit()


async def _add_calc(
    session_factory: _SessionFactory,
    owner: _Owner,
    lost_revenue: int,
    days_per_month: int,
) -> None:
    async with session_factory() as db_session:
        db_session.add(
            models.CalcReport(
                user_id=owner.owner_id, source=models.CalcSource.BOT,
                seats=1, hours_per_day=10, days_per_month=days_per_month,
                load_pct=60, revenue_per_hour=1500, variable_cost=600,
                lost_revenue=lost_revenue, recoverable=0,
            )
        )
        await db_session.commit()


@pytest.fixture
async def owner(session_factory: _SessionFactory) -> _Owner:
    """Неделя бизнеса: все исходы слотов и один слот вне недели."""
    created = await _create_owner(session_factory)
    slot = models.SlotStatus
    booking = models.BookingStatus
    await _add_slot(session_factory, created, slot.DONE, (booking.DONE, 900))
    await _add_slot(
        session_factory, created, slot.BOOKED, (booking.ACTIVE, 1000)
    )
    await _add_slot(
        session_factory, created, slot.NO_SHOW, (booking.NO_SHOW, 800)
    )
    await _add_slot(session_factory, created, slot.EXPIRED)
    await _add_slot(session_factory, created, slot.WITHDRAWN)
    await _add_slot(
        session_factory, created, slot.OPEN, (booking.CANCELLED, 1100)
    )
    await _add_slot(
        session_factory, created, slot.DONE, (booking.DONE, 700),
        starts_at=_WEEK_END,
    )
    return created


async def _report(
    session_factory: _SessionFactory, owner: _Owner
) -> reports.OwnerReport:
    async with session_factory() as db_session:
        return await reports.build_owner_report(
            db_session, owner.business_id, owner.owner_id, _WEEK
        )


def test_due_weekly_period_monday_before_10_returns_none() -> None:
    before = _MONDAY_SEND - datetime.timedelta(minutes=6)
    assert reports.due_weekly_period(before, _CITY_TZ) is None


def test_due_weekly_period_monday_at_10_returns_previous_week() -> None:
    at_ten = _MONDAY_SEND - datetime.timedelta(minutes=5)
    assert reports.due_weekly_period(at_ten, _CITY_TZ) == _WEEK


def test_due_weekly_period_later_in_week_still_previous_week() -> None:
    sunday = datetime.datetime(2026, 12, 6, 23, 0, tzinfo=_CITY_TZ)
    assert reports.due_weekly_period(sunday, _CITY_TZ) == _WEEK


def test_week_key_is_monday_date() -> None:
    assert reports.week_key(_WEEK, _CITY_TZ) == "2026-11-23"


def test_last_days_period_is_seven_days_to_now() -> None:
    period = reports.last_days_period(_MONDAY_SEND)
    assert (period.start, period.end) == (
        _MONDAY_SEND - datetime.timedelta(days=7), _MONDAY_SEND,
    )


async def test_report_published_excludes_withdrawn_and_other_weeks(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert (await _report(session_factory, owner)).published == 5


async def test_report_sold_counts_active_and_done(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert (await _report(session_factory, owner)).sold == 2


async def test_report_not_sold_counts_expired(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert (await _report(session_factory, owner)).not_sold == 1


async def test_report_returned_sums_only_done(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert (await _report(session_factory, owner)).returned == 900


async def test_report_no_shows_counted(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert (await _report(session_factory, owner)).no_shows == 1


async def test_report_average_discount_from_regular_price(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    # (1500 − 900) / 1500 = 40%, (1500 − 1000) / 1500 = 33,3%: в среднем 37%.
    report = await _report(session_factory, owner)
    assert report.average_discount_pct == 37


async def test_report_commission_is_15_percent_of_returned(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert (await _report(session_factory, owner)).commission == 135


async def test_report_without_calc_has_no_recovered_pct(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert (await _report(session_factory, owner)).recovered_pct is None


async def test_report_recovered_pct_uses_latest_calc_weekly_loss(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _add_calc(session_factory, owner, 100, 7)
    # Недельные потери 9 000 × 7 / 7 = 9 000 ₽, возвращено 900 ₽.
    await _add_calc(session_factory, owner, 9000, 7)
    report = await _report(session_factory, owner)
    assert report.recovered_pct == 10


async def test_report_recovered_pct_scales_month_to_week(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    # Пример B4.1: 468 000 × 7 / 26 = 126 000 ₽ в неделю.
    await _add_calc(session_factory, owner, 468_000, 26)
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.Booking)
            .where(models.Booking.price == 900)
            .values(price=12_600)
        )
        await db_session.commit()
    report = await _report(session_factory, owner)
    assert report.recovered_pct == 10


async def test_report_zero_lost_revenue_has_no_recovered_pct(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    await _add_calc(session_factory, owner, 0, 26)
    assert (await _report(session_factory, owner)).recovered_pct is None


async def test_report_ignores_other_business_slots(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    other = await _create_owner(session_factory, _OTHER_OWNER_TG_ID)
    async with session_factory() as db_session:
        report = await reports.build_owner_report(
            db_session, other.business_id, other.owner_id, _WEEK
        )
    assert (report.published, report.average_discount_pct) == (0, None)


_REPORT = reports.OwnerReport(
    published=5, sold=2, not_sold=1, returned=900, average_discount_pct=37,
    no_shows=1, recovered_pct=10, commission=135,
)


def test_format_owner_report_lists_numbers() -> None:
    text = reports.format_owner_report(_REPORT, _WEEK, _CITY_TZ)
    assert text.split("\n")[:8] == [
        "Отчёт с 23.11 по 29.11",
        "",
        "Опубликовано слотов: 5",
        "Продано: 2",
        "Не продано: 1",
        "Возвращено денег: 900 ₽",
        "Средняя скидка: 37%",
        "Неявки: 1",
    ]


def test_format_owner_report_shows_recovered_line() -> None:
    text = reports.format_owner_report(_REPORT, _WEEK, _CITY_TZ)
    assert "Возвращено 10% от потерь по калькулятору." in text


def test_format_owner_report_without_calc_skips_recovered_line() -> None:
    report = dataclasses.replace(_REPORT, recovered_pct=None)
    text = reports.format_owner_report(report, _WEEK, _CITY_TZ)
    assert "от потерь" not in text


def test_format_owner_report_commission_marked_not_charged() -> None:
    text = reports.format_owner_report(_REPORT, _WEEK, _CITY_TZ)
    assert text.endswith(
        "Комиссия сервиса составила бы 135 ₽. Сейчас она не взимается."
    )


def test_format_owner_report_no_sales_discount_text() -> None:
    report = dataclasses.replace(_REPORT, average_discount_pct=None)
    text = reports.format_owner_report(report, _WEEK, _CITY_TZ)
    assert "Средняя скидка: нет продаж" in text


def test_format_owner_report_has_no_emoji_or_symbols() -> None:
    text = reports.format_owner_report(_REPORT, _WEEK, _CITY_TZ)
    assert not _SYMBOLS.search(text)


async def _recipients(
    session_factory: _SessionFactory,
) -> list[reports.WeeklyRecipient]:
    async with session_factory() as db_session:
        return await reports.list_weekly_recipients(
            db_session, _WEEK, reports.week_key(_WEEK, _CITY_TZ)
        )


async def test_weekly_recipients_include_approved_business(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    assert await _recipients(session_factory) == [
        reports.WeeklyRecipient(
            owner.business_id, owner.owner_id, _OWNER_TG_ID
        ),
    ]


async def test_weekly_recipients_skip_pending_business(
    session_factory: _SessionFactory,
) -> None:
    await _create_owner(session_factory, is_approved=False)
    assert await _recipients(session_factory) == []


async def test_weekly_recipients_skip_business_registered_after_week(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    async with session_factory() as db_session:
        await db_session.execute(
            sqlalchemy.update(models.Business).values(created_at=_WEEK.end)
        )
        await db_session.commit()
    assert await _recipients(session_factory) == []


async def test_weekly_recipients_skip_already_sent(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    [recipient] = await _recipients(session_factory)
    async with session_factory() as db_session:
        await reports.mark_report_sent(db_session, recipient, "2026-11-23")
    assert await _recipients(session_factory) == []


async def test_weekly_recipients_other_week_mark_does_not_count(
    session_factory: _SessionFactory, owner: _Owner
) -> None:
    [recipient] = await _recipients(session_factory)
    async with session_factory() as db_session:
        await reports.mark_report_sent(db_session, recipient, "2026-11-16")
    assert len(await _recipients(session_factory)) == 1


@pytest.fixture
def monday_10_05(monkeypatch: pytest.MonkeyPatch) -> None:
    """Время планировщика — понедельник 10:05 по Москве."""
    monkeypatch.setattr(models, "utc_now", lambda: _MONDAY_SEND)


async def test_run_weekly_reports_sends_report_to_owner(
    session_factory: _SessionFactory,
    owner: _Owner,
    make_fake_bot: _MakeBot,
    monday_10_05: None,
) -> None:
    bot = make_fake_bot()
    await jobs.run_weekly_reports(bot, session_factory, _CITY_TZ)
    [sent] = bot.sent
    assert (sent.chat_id, sent.text.split("\n")[0]) == (
        _OWNER_TG_ID, "Отчёт с 23.11 по 29.11",
    )


async def test_run_weekly_reports_second_run_sends_nothing(
    session_factory: _SessionFactory,
    owner: _Owner,
    make_fake_bot: _MakeBot,
    monday_10_05: None,
) -> None:
    bot = make_fake_bot()
    await jobs.run_weekly_reports(bot, session_factory, _CITY_TZ)
    await jobs.run_weekly_reports(bot, session_factory, _CITY_TZ)
    assert len(bot.sent) == 1


async def test_run_weekly_reports_records_week_in_event(
    session_factory: _SessionFactory,
    owner: _Owner,
    make_fake_bot: _MakeBot,
    monday_10_05: None,
) -> None:
    await jobs.run_weekly_reports(make_fake_bot(), session_factory, _CITY_TZ)
    async with session_factory() as db_session:
        event = await db_session.scalar(
            sqlalchemy.select(models.Event).where(
                models.Event.type == models.EventType.REPORT_SENT
            )
        )
    assert event.payload == {
        "week": "2026-11-23", "business_id": owner.business_id,
    }


async def test_run_weekly_reports_network_error_retried_next_run(
    session_factory: _SessionFactory,
    owner: _Owner,
    make_fake_bot: _MakeBot,
    monday_10_05: None,
) -> None:
    failing = make_fake_bot(network_error_chat_ids=frozenset({_OWNER_TG_ID}))
    await jobs.run_weekly_reports(failing, session_factory, _CITY_TZ)
    bot = make_fake_bot()
    await jobs.run_weekly_reports(bot, session_factory, _CITY_TZ)
    assert len(bot.sent) == 1


async def test_run_weekly_reports_blocked_owner_not_retried(
    session_factory: _SessionFactory,
    owner: _Owner,
    make_fake_bot: _MakeBot,
    monday_10_05: None,
) -> None:
    blocked = make_fake_bot(blocked_chat_ids=frozenset({_OWNER_TG_ID}))
    await jobs.run_weekly_reports(blocked, session_factory, _CITY_TZ)
    assert await _recipients(session_factory) == []


async def test_run_weekly_reports_before_monday_10_sends_nothing(
    session_factory: _SessionFactory,
    owner: _Owner,
    make_fake_bot: _MakeBot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    early = _MONDAY_SEND - datetime.timedelta(minutes=10)
    monkeypatch.setattr(models, "utc_now", lambda: early)
    bot = make_fake_bot()
    await jobs.run_weekly_reports(bot, session_factory, _CITY_TZ)
    assert bot.sent == []


def _report_command(tg_id: int) -> types.Update:
    return types.Update(
        update_id=1,
        message=types.Message(
            message_id=1, date=datetime.datetime.now(datetime.UTC),
            chat=types.Chat(id=tg_id, type="private"),
            from_user=types.User(id=tg_id, is_bot=False, first_name="Иван"),
            text="/report",
            entities=[
                types.MessageEntity(type="bot_command", offset=0, length=7)
            ],
        ),
    )


async def _send_report_command(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> str:
    fake_session = conftest.FakeTelegramSession()
    bot = aiogram.Bot("1:test", session=fake_session)
    await dispatcher.feed_update(
        bot, _report_command(_OWNER_TG_ID), session_factory=session_factory,
        city_tz=_CITY_TZ, admin_ids=frozenset(),
    )
    [reply] = fake_session.sent_to(_OWNER_TG_ID)
    return reply.text


async def test_report_command_without_business_points_to_business(
    dispatcher: aiogram.Dispatcher, session_factory: _SessionFactory
) -> None:
    text = await _send_report_command(dispatcher, session_factory)
    assert text == (
        "Отчёт появится, когда вы зарегистрируете бизнес: /business"
    )


async def test_report_command_with_business_sends_report(
    dispatcher: aiogram.Dispatcher,
    session_factory: _SessionFactory,
    owner: _Owner,
) -> None:
    text = await _send_report_command(dispatcher, session_factory)
    assert "Опубликовано слотов:" in text

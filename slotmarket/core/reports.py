"""Отчёт владельцу (B4.8), статистика и выгрузка для команды (B4.9)."""

import csv
import dataclasses
import datetime
import decimal
import io

import sqlalchemy
from sqlalchemy import orm
from sqlalchemy.ext import asyncio as sa_asyncio

from core import feed
from core import pricing
from db import models

REPORT_PERIOD = datetime.timedelta(days=7)
# Недельный отчёт уходит в понедельник с 10:00 по времени города.
WEEKLY_REPORT_HOUR = 10
_DAYS_PER_WEEK = 7
_PERCENT = 100
_WEEK_KEY = "week"
_BUSINESS_ID_KEY = "business_id"
_DATE_FORMAT = "%d.%m"
_SOLD_STATUSES = (models.BookingStatus.ACTIVE, models.BookingStatus.DONE)
# Доля неявок считается среди броней, по которым визит уже отмечен.
_VISITED_STATUSES = (models.BookingStatus.DONE, models.BookingStatus.NO_SHOW)
ACTIVE_BUYER_PERIOD = datetime.timedelta(days=30)
# UTF-8 с BOM, чтобы Excel открыл кириллицу без выбора кодировки.
CSV_ENCODING = "utf-8-sig"
# Только колонки без имени, телефона и Telegram ID (B4.9): покупатель
# указан внутренним buyer_id.
_SLOT_EXPORT_COLUMNS = (
    models.Slot.id, models.Slot.service_id, models.Slot.starts_at,
    models.Slot.start_price, models.Slot.min_price,
    models.Slot.current_price, models.Slot.hide_name, models.Slot.status,
    models.Slot.channel_msg_id, models.Slot.published_at,
)
_BOOKING_EXPORT_COLUMNS = (
    models.Booking.id, models.Booking.slot_id, models.Booking.buyer_id,
    models.Booking.price, models.Booking.status, models.Booking.created_at,
    models.Booking.cancelled_at, models.Booking.cancelled_by,
)
_NO_DATA_TEXT = "нет данных"

_BookingRow = tuple[models.BookingStatus, int, int]


@dataclasses.dataclass(frozen=True)
class ReportPeriod:
    """Полуоткрытый интервал [start, end) по времени начала слотов."""

    start: datetime.datetime
    end: datetime.datetime


@dataclasses.dataclass(frozen=True)
class OwnerReport:
    """Цифры отчёта владельцу. Деньги — целые рубли.

    Attributes:
        published: Слоты периода, кроме снятых владельцем.
        sold: Брони в статусах active и done.
        not_sold: Слоты в статусе expired.
        returned: Сумма цен броней в статусе done.
        average_discount_pct: Средняя скидка проданных броней от
            текущей обычной цены услуги или None, если продаж нет.
        no_shows: Брони в статусе no_show.
        recovered_pct: Возвращено в процентах от недельных потерь по
            калькулятору или None, если расчёта нет.
        commission: Расчётная комиссия сервиса, не взимается.
    """

    published: int
    sold: int
    not_sold: int
    returned: int
    average_discount_pct: int | None
    no_shows: int
    recovered_pct: int | None
    commission: int


@dataclasses.dataclass(frozen=True)
class WeeklyRecipient:
    """Кому отправить недельный отчёт."""

    business_id: int
    owner_id: int
    owner_tg_id: int


def last_days_period(now: datetime.datetime) -> ReportPeriod:
    """Возвращает последние 7 дней до текущего момента для /report.

    Args:
        now: Текущее время.

    Returns:
        Период [now − 7 дней, now).
    """
    return ReportPeriod(start=now - REPORT_PERIOD, end=now)


def due_weekly_period(
    now: datetime.datetime, city_tz: datetime.tzinfo
) -> ReportPeriod | None:
    """Возвращает прошлую неделю, если пора слать недельный отчёт.

    Пора с понедельника 10:00 по времени города и до следующего
    понедельника: если бот был выключен в 10:00, отчёт уйдёт после
    запуска, а отметка report_sent не даст отправить его дважды.

    Args:
        now: Текущее время.
        city_tz: Часовой пояс города.

    Returns:
        Прошлая неделя с понедельника по воскресенье или None, если
        в понедельник ещё нет 10:00.
    """
    local_now = now.astimezone(city_tz)
    monday = local_now.date() - datetime.timedelta(days=local_now.weekday())
    week_start = datetime.datetime.combine(
        monday, datetime.time(), tzinfo=city_tz
    )
    send_from = week_start + datetime.timedelta(hours=WEEKLY_REPORT_HOUR)
    if local_now < send_from:
        return None
    previous_start = datetime.datetime.combine(
        monday - datetime.timedelta(days=_DAYS_PER_WEEK),
        datetime.time(), tzinfo=city_tz,
    )
    return ReportPeriod(start=previous_start, end=week_start)


def week_key(period: ReportPeriod, city_tz: datetime.tzinfo) -> str:
    """Возвращает ключ недели для отметки report_sent.

    Args:
        period: Недельный период.
        city_tz: Часовой пояс города.

    Returns:
        Дата понедельника недели, например 2026-11-23.
    """
    return period.start.astimezone(city_tz).date().isoformat()


async def build_owner_report(
    db_session: sa_asyncio.AsyncSession,
    business_id: int,
    owner_id: int,
    period: ReportPeriod,
) -> OwnerReport:
    """Считает отчёт бизнеса по слотам, начавшимся в периоде (B4.8).

    Args:
        db_session: Сессия базы.
        business_id: ID бизнеса.
        owner_id: ID владельца: по нему ищется последний расчёт.
        period: Период по времени начала слотов.

    Returns:
        Цифры отчёта.
    """
    slot_statuses = await _slot_statuses(db_session, business_id, period)
    booking_rows = await _booking_rows(db_session, business_id, period)
    sold_rows = [row for row in booking_rows if row[0] in _SOLD_STATUSES]
    returned = sum(
        price for status, price, _ in booking_rows
        if status == models.BookingStatus.DONE
    )
    return OwnerReport(
        published=sum(
            status != models.SlotStatus.WITHDRAWN for status in slot_statuses
        ),
        sold=len(sold_rows),
        not_sold=slot_statuses.count(models.SlotStatus.EXPIRED),
        returned=returned,
        average_discount_pct=_average_discount(sold_rows),
        no_shows=sum(
            status == models.BookingStatus.NO_SHOW
            for status, _, _ in booking_rows
        ),
        recovered_pct=await _recovered_pct(db_session, owner_id, returned),
        commission=_round(returned * pricing.COMMISSION_SHARE),
    )


def format_owner_report(
    report: OwnerReport, period: ReportPeriod, city_tz: datetime.tzinfo
) -> str:
    """Собирает текст отчёта для владельца.

    Args:
        report: Цифры отчёта.
        period: Период отчёта.
        city_tz: Часовой пояс города.

    Returns:
        Текст без эмодзи и значков.
    """
    first_day = period.start.astimezone(city_tz)
    # end не входит в период, поэтому последний день — на секунду раньше.
    last_day = (period.end - datetime.timedelta(seconds=1)).astimezone(
        city_tz
    )
    discount = (
        "нет продаж" if report.average_discount_pct is None
        else f"{report.average_discount_pct}%"
    )
    lines = [
        f"Отчёт с {first_day.strftime(_DATE_FORMAT)} "
        f"по {last_day.strftime(_DATE_FORMAT)}",
        "",
        f"Опубликовано слотов: {report.published}",
        f"Продано: {report.sold}",
        f"Не продано: {report.not_sold}",
        f"Возвращено денег: {feed.format_rubles(report.returned)}",
        f"Средняя скидка: {discount}",
        f"Неявки: {report.no_shows}",
    ]
    if report.recovered_pct is not None:
        lines.append(
            f"Возвращено {report.recovered_pct}% от потерь по калькулятору."
        )
    lines.extend([
        "",
        "Комиссия сервиса составила бы "
        f"{feed.format_rubles(report.commission)}. Сейчас она не "
        "взимается.",
    ])
    return "\n".join(lines)


async def list_weekly_recipients(
    db_session: sa_asyncio.AsyncSession, period: ReportPeriod, week: str
) -> list[WeeklyRecipient]:
    """Возвращает одобренные бизнесы, которым отчёт за неделю не ушёл.

    Бизнесы, зарегистрированные после недели, отчёт не получают.

    Args:
        db_session: Сессия базы.
        period: Недельный период.
        week: Ключ недели из week_key.

    Returns:
        Получатели по возрастанию ID бизнеса.
    """
    sent_business_ids = sqlalchemy.select(
        models.Event.payload[_BUSINESS_ID_KEY].as_integer()
    ).where(
        models.Event.type == models.EventType.REPORT_SENT,
        models.Event.payload[_WEEK_KEY].as_string() == week,
    )
    rows = await db_session.execute(
        sqlalchemy.select(
            models.Business.id, models.Business.owner_id, models.User.tg_id
        )
        .join(models.User, models.User.id == models.Business.owner_id)
        .where(
            models.Business.status == models.BusinessStatus.APPROVED,
            models.Business.created_at < period.end,
            models.Business.id.not_in(sent_business_ids),
        )
        .order_by(models.Business.id)
    )
    return [WeeklyRecipient(*row) for row in rows]


async def mark_report_sent(
    db_session: sa_asyncio.AsyncSession,
    recipient: WeeklyRecipient,
    week: str,
) -> None:
    """Запоминает, что недельный отчёт отправлен (B4.8).

    Args:
        db_session: Сессия базы.
        recipient: Кому отправлен отчёт.
        week: Ключ недели из week_key.
    """
    db_session.add(
        models.Event(
            user_id=recipient.owner_id,
            type=models.EventType.REPORT_SENT,
            payload={
                _WEEK_KEY: week, _BUSINESS_ID_KEY: recipient.business_id,
            },
        )
    )
    await db_session.commit()


@dataclasses.dataclass(frozen=True)
class PeriodStats:
    """Цифры команды за период и метрики теста из B10.

    Доли в процентах равны None, если делить не на что.

    Attributes:
        published: Слоты, кроме снятых владельцем.
        sold: Брони в статусах active и done.
        sold_pct: Доля проданных от опубликованных.
        no_shows: Брони в статусе no_show.
        visits: Брони с отмеченным визитом: done и no_show.
        no_show_pct: Доля неявок среди отмеченных визитов.
        returned: Сумма цен броней в статусе done, целые рубли.
        average_discount_pct: Средняя скидка проданных броней.
        calc_owners: Пользователи, прошедшие калькулятор в периоде.
        calc_owners_with_slot: Из них выставили слот после расчёта.
        calc_conversion_pct: Доля выставивших слот после калькулятора.
    """

    published: int
    sold: int
    sold_pct: int | None
    no_shows: int
    visits: int
    no_show_pct: int | None
    returned: int
    average_discount_pct: int | None
    calc_owners: int
    calc_owners_with_slot: int
    calc_conversion_pct: int | None


@dataclasses.dataclass(frozen=True)
class TeamStats:
    """Статистика для /stats (B4.9).

    Attributes:
        all_time: Цифры за всё время.
        last_days: Цифры за последние 7 дней.
        active_businesses: Бизнесы в статусе approved.
        active_buyers: Покупатели с бронью за последние 30 дней.
    """

    all_time: PeriodStats
    last_days: PeriodStats
    active_businesses: int
    active_buyers: int


async def build_team_stats(
    db_session: sa_asyncio.AsyncSession, now: datetime.datetime
) -> TeamStats:
    """Считает статистику команды за всё время и за 7 дней (B4.9, B10).

    Слоты и брони отбираются по времени начала слота, как в отчёте
    владельцу, расчёты калькулятора — по времени расчёта.

    Args:
        db_session: Сессия базы.
        now: Текущее время.

    Returns:
        Статистика по всем бизнесам.
    """
    active_businesses = await db_session.scalar(
        sqlalchemy.select(sqlalchemy.func.count()).where(
            models.Business.status == models.BusinessStatus.APPROVED
        )
    )
    active_buyers = await db_session.scalar(
        sqlalchemy.select(
            sqlalchemy.func.count(
                sqlalchemy.distinct(models.Booking.buyer_id)
            )
        ).where(models.Booking.created_at >= now - ACTIVE_BUYER_PERIOD)
    )
    return TeamStats(
        all_time=await _build_period_stats(db_session, None),
        last_days=await _build_period_stats(
            db_session, last_days_period(now)
        ),
        active_businesses=active_businesses,
        active_buyers=active_buyers,
    )


def format_team_stats(stats: TeamStats) -> str:
    """Собирает текст /stats для администратора.

    Args:
        stats: Статистика команды.

    Returns:
        Текст без эмодзи и значков.
    """
    return "\n".join([
        "Статистика SlotMarket",
        "",
        "За всё время",
        *_format_period_stats(stats.all_time),
        "",
        "За последние 7 дней",
        *_format_period_stats(stats.last_days),
        "",
        f"Активных бизнесов: {stats.active_businesses}",
        f"Активных покупателей за 30 дней: {stats.active_buyers}",
    ])


async def export_slots_csv(db_session: sa_asyncio.AsyncSession) -> bytes:
    """Выгружает все слоты в CSV для /export (B4.9).

    Args:
        db_session: Сессия базы.

    Returns:
        CSV в UTF-8 с BOM.
    """
    return await _export_csv(db_session, _SLOT_EXPORT_COLUMNS)


async def export_bookings_csv(db_session: sa_asyncio.AsyncSession) -> bytes:
    """Выгружает все брони в CSV для /export без данных покупателя.

    Args:
        db_session: Сессия базы.

    Returns:
        CSV в UTF-8 с BOM.
    """
    return await _export_csv(db_session, _BOOKING_EXPORT_COLUMNS)


async def _build_period_stats(
    db_session: sa_asyncio.AsyncSession, period: ReportPeriod | None
) -> PeriodStats:
    slot_statuses = await _slot_statuses(db_session, None, period)
    booking_rows = await _booking_rows(db_session, None, period)
    booking_statuses = [status for status, _, _ in booking_rows]
    sold_rows = [row for row in booking_rows if row[0] in _SOLD_STATUSES]
    published = sum(
        status != models.SlotStatus.WITHDRAWN for status in slot_statuses
    )
    no_shows = booking_statuses.count(models.BookingStatus.NO_SHOW)
    visits = sum(status in _VISITED_STATUSES for status in booking_statuses)
    calc_owners, calc_owners_with_slot = await _calc_conversion(
        db_session, period
    )
    return PeriodStats(
        published=published,
        sold=len(sold_rows),
        sold_pct=_share_pct(len(sold_rows), published),
        no_shows=no_shows,
        visits=visits,
        no_show_pct=_share_pct(no_shows, visits),
        returned=sum(
            price for status, price, _ in booking_rows
            if status == models.BookingStatus.DONE
        ),
        average_discount_pct=_average_discount(sold_rows),
        calc_owners=calc_owners,
        calc_owners_with_slot=calc_owners_with_slot,
        calc_conversion_pct=_share_pct(calc_owners_with_slot, calc_owners),
    )


async def _calc_conversion(
    db_session: sa_asyncio.AsyncSession, period: ReportPeriod | None
) -> tuple[int, int]:
    # Расчёты с сайта без привязки к боту (user_id пуст) не считаются:
    # неизвестно, выставил ли их автор слот.
    calc_query = (
        sqlalchemy.select(
            models.CalcReport.user_id,
            sqlalchemy.func.min(models.CalcReport.created_at),
        )
        .where(models.CalcReport.user_id.is_not(None))
        .group_by(models.CalcReport.user_id)
    )
    if period is not None:
        calc_query = calc_query.where(
            models.CalcReport.created_at >= period.start,
            models.CalcReport.created_at < period.end,
        )
    first_calc_at = dict((await db_session.execute(calc_query)).all())
    last_slot_rows = await db_session.execute(
        sqlalchemy.select(
            models.Business.owner_id,
            sqlalchemy.func.max(models.Slot.published_at),
        )
        .join(models.Service, models.Service.id == models.Slot.service_id)
        .join(
            models.Business,
            models.Business.id == models.Service.business_id,
        )
        .where(models.Business.owner_id.in_(first_calc_at))
        .group_by(models.Business.owner_id)
    )
    with_slot = sum(
        last_published >= first_calc_at[owner_id]
        for owner_id, last_published in last_slot_rows
    )
    return len(first_calc_at), with_slot


def _format_period_stats(stats: PeriodStats) -> list[str]:
    discount = (
        _NO_DATA_TEXT if stats.average_discount_pct is None
        else f"{stats.average_discount_pct}%"
    )
    return [
        f"- Опубликовано слотов: {stats.published}",
        f"- Продано: {stats.sold} из {stats.published} "
        f"({_format_pct(stats.sold_pct)})",
        f"- Неявки: {stats.no_shows} ({_format_pct(stats.no_show_pct)} "
        f"от отмеченных визитов, их {stats.visits})",
        f"- Возвращено денег: {feed.format_rubles(stats.returned)}",
        f"- Средняя скидка при покупке: {discount}",
        "- Выставили слот после калькулятора: "
        f"{stats.calc_owners_with_slot} из {stats.calc_owners} "
        f"({_format_pct(stats.calc_conversion_pct)})",
    ]


def _format_pct(value: int | None) -> str:
    return _NO_DATA_TEXT if value is None else f"{value}%"


def _share_pct(part: int, whole: int) -> int | None:
    if whole == 0:
        return None
    return _round(decimal.Decimal(part) / whole * _PERCENT)


async def _export_csv(
    db_session: sa_asyncio.AsyncSession,
    columns: tuple[orm.InstrumentedAttribute, ...],
) -> bytes:
    rows = await db_session.execute(
        sqlalchemy.select(*columns).order_by(columns[0])
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([column.key for column in columns])
    for row in rows:
        writer.writerow([_csv_value(value) for value in row])
    return buffer.getvalue().encode(CSV_ENCODING)


def _csv_value(value: object) -> object:
    # Время в UTC в ISO 8601, булевы как 1 и 0 — так их читают таблицы.
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, bool):
        return int(value)
    return value


def _period_slots(
    business_id: int | None, period: ReportPeriod | None
) -> list[sqlalchemy.ColumnElement[bool]]:
    # Без бизнеса — все бизнесы, без периода — всё время (/stats).
    conditions = []
    if business_id is not None:
        conditions.append(models.Service.business_id == business_id)
    if period is not None:
        conditions.extend([
            models.Slot.starts_at >= period.start,
            models.Slot.starts_at < period.end,
        ])
    return conditions


async def _slot_statuses(
    db_session: sa_asyncio.AsyncSession,
    business_id: int | None,
    period: ReportPeriod | None,
) -> list[models.SlotStatus]:
    result = await db_session.scalars(
        sqlalchemy.select(models.Slot.status)
        .join(models.Service, models.Service.id == models.Slot.service_id)
        .where(*_period_slots(business_id, period))
    )
    return list(result)


async def _booking_rows(
    db_session: sa_asyncio.AsyncSession,
    business_id: int | None,
    period: ReportPeriod | None,
) -> list[_BookingRow]:
    result = await db_session.execute(
        sqlalchemy.select(
            models.Booking.status, models.Booking.price,
            models.Service.regular_price,
        )
        .join(models.Slot, models.Slot.id == models.Booking.slot_id)
        .join(models.Service, models.Service.id == models.Slot.service_id)
        .where(*_period_slots(business_id, period))
    )
    return [tuple(row) for row in result]


def _average_discount(sold_rows: list[_BookingRow]) -> int | None:
    # Обычная цена в слоте не хранится, берём текущую цену услуги:
    # после её смены скидка по старым слотам приблизительная (B4.8).
    if not sold_rows:
        return None
    shares = [
        decimal.Decimal(regular_price - price) / regular_price
        for _, price, regular_price in sold_rows
    ]
    return _round(sum(shares) / len(shares) * _PERCENT)


async def _recovered_pct(
    db_session: sa_asyncio.AsyncSession, owner_id: int, returned: int
) -> int | None:
    calc_report = await db_session.scalar(
        sqlalchemy.select(models.CalcReport)
        .where(models.CalcReport.user_id == owner_id)
        .order_by(
            models.CalcReport.created_at.desc(), models.CalcReport.id.desc()
        )
        .limit(1)
    )
    if calc_report is None or calc_report.lost_revenue == 0:
        return None
    weekly_loss = (
        decimal.Decimal(calc_report.lost_revenue) * _DAYS_PER_WEEK
        / calc_report.days_per_month
    )
    return _round(decimal.Decimal(returned) / weekly_loss * _PERCENT)


def _round(value: decimal.Decimal) -> int:
    return int(value.quantize(decimal.Decimal(1), decimal.ROUND_HALF_UP))

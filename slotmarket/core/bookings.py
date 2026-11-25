"""Бронирование: бронь, отмена, визиты, напоминания, /my (B4.5, B4.7).

Все смены статуса слота проходят через таблицу переходов B7
(slots.check_transition) и делаются одним UPDATE с условием на
текущий статус.
"""

import dataclasses
import datetime
import enum

import sqlalchemy
from sqlalchemy import orm
from sqlalchemy.ext import asyncio as sa_asyncio

from core import pricing
from core import slots
from db import models

MAX_ACTIVE_BOOKINGS = 2
CANCEL_DEADLINE = datetime.timedelta(hours=1)
EVENT_SLOT_OPENED = "slot_opened"
EVENT_SLOT_BOOKED = "slot_booked"
EVENT_BOOKING_CANCELLED = "booking_cancelled"
REMINDER_2H_LEAD = datetime.timedelta(hours=2)
REMINDER_30M_LEAD = datetime.timedelta(minutes=30)
VISIT_QUESTION_DELAY = datetime.timedelta(hours=1)
# 24 часа на ответ считаются от вопроса «пришёл или нет» (B4.5).
AUTO_CONFIRM_DELAY = VISIT_QUESTION_DELAY + datetime.timedelta(hours=24)
NO_SHOW_LIMIT = 2
BLOCK_PERIOD = datetime.timedelta(days=30)
OWNER_WITHDRAWALS_ALERT = 3


class BookOutcome(enum.StrEnum):
    """Чем закончилась попытка брони."""

    BOOKED = "booked"
    TAKEN = "taken"
    LIMIT_REACHED = "limit_reached"
    BLOCKED = "blocked"
    NO_PHONE = "no_phone"
    OWN_SLOT = "own_slot"


class NoticeKind(enum.StrEnum):
    """Напоминание или вопрос по брони. Значение — тип события в events."""

    REMINDER_2H = "reminder_2h"
    REMINDER_30M = "reminder_30m"
    VISIT_ASKED = "visit_asked"


class VisitOutcome(enum.StrEnum):
    """Чем закончилась отметка визита владельцем."""

    RECORDED = "recorded"
    NOT_FOUND = "not_found"


class CancelOutcome(enum.StrEnum):
    """Чем закончилась отмена брони покупателем."""

    CANCELLED = "cancelled"
    TOO_LATE = "too_late"
    NOT_FOUND = "not_found"


@dataclasses.dataclass(frozen=True)
class BookingDetails:
    """Всё о брони для уведомлений и /my (B4.7).

    Телефон покупателя попадает только к владельцу этого слота.
    """

    booking_id: int
    slot_id: int
    price: int
    starts_at: datetime.datetime
    service_title: str
    business_name: str
    business_address: str
    business_phone: str
    owner_tg_id: int
    buyer_tg_id: int
    buyer_name: str
    buyer_phone: str | None
    booked_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class BookResult:
    """Итог попытки брони.

    Attributes:
        outcome: Чем закончилась попытка.
        details: Данные брони, если она создана.
        blocked_until: До какого времени покупатель заблокирован.
    """

    outcome: BookOutcome
    details: BookingDetails | None = None
    blocked_until: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class DueNotice:
    """Напоминание или вопрос, который пора отправить."""

    kind: NoticeKind
    details: BookingDetails


@dataclasses.dataclass(frozen=True)
class VisitResult:
    """Итог отметки визита.

    Attributes:
        outcome: Отмечено или бронь не найдена (чужая, уже отмечена,
            слот ещё не начался).
        details: Данные брони, если отмечено.
        buyer_blocked_until: До какого времени заблокирован покупатель,
            если это была вторая неявка.
    """

    outcome: VisitOutcome
    details: BookingDetails | None = None
    buyer_blocked_until: datetime.datetime | None = None


@dataclasses.dataclass(frozen=True)
class CancelResult:
    """Итог отмены брони покупателем."""

    outcome: CancelOutcome
    details: BookingDetails | None = None


def can_cancel(starts_at: datetime.datetime, now: datetime.datetime) -> bool:
    """Проверяет, можно ли ещё отменить бронь: не позднее чем за час.

    Args:
        starts_at: Начало слота.
        now: Текущее время.

    Returns:
        True, если до начала не меньше часа (B4.5).
    """
    return starts_at - now >= CANCEL_DEADLINE


async def quote_price(
    db_session: sa_asyncio.AsyncSession,
    slot_id: int,
    now: datetime.datetime,
) -> int | None:
    """Считает цену открытого слота на этот момент (B4.4).

    Args:
        db_session: Сессия базы.
        slot_id: ID слота.
        now: Текущее время.

    Returns:
        Цена, ₽, или None, если слот не открыт.
    """
    slot = await db_session.get(models.Slot, slot_id)
    if slot is None or slot.status != models.SlotStatus.OPEN:
        return None
    return pricing.calc_current_price(
        slot.start_price, slot.min_price, slot.starts_at,
        slot.published_at, now,
    )


async def record_slot_opened(
    db_session: sa_asyncio.AsyncSession, user_id: int, slot_id: int
) -> None:
    """Пишет событие «открыл слот» для метрик (B7, B10).

    Args:
        db_session: Сессия базы.
        user_id: Кто открыл слот по deep link.
        slot_id: ID слота.
    """
    db_session.add(
        models.Event(
            user_id=user_id, type=EVENT_SLOT_OPENED,
            payload={"slot_id": slot_id},
        )
    )
    await db_session.commit()


async def book_slot(
    db_session: sa_asyncio.AsyncSession,
    buyer_id: int,
    slot_id: int,
    now: datetime.datetime,
) -> BookResult:
    """Бронирует слот атомарным UPDATE (B4.5).

    Args:
        db_session: Сессия базы.
        buyer_id: ID покупателя.
        slot_id: ID слота.
        now: Время брони: по нему считается цена.

    Returns:
        Итог. При гонке двух покупателей бронь получает тот, чей UPDATE
        изменил строку, второй получает TAKEN.
    """
    rejection = await _check_buyer(db_session, buyer_id, slot_id, now)
    if rejection is not None:
        return rejection
    slots.check_transition(models.SlotStatus.OPEN, models.SlotStatus.BOOKED)
    # Проверки выше только читают. Бронь решает этот UPDATE: условие
    # status='open' пропустит только первый из одновременных запросов.
    row = (
        await db_session.execute(
            sqlalchemy.update(models.Slot)
            .where(
                models.Slot.id == slot_id,
                models.Slot.status == models.SlotStatus.OPEN,
            )
            .values(status=models.SlotStatus.BOOKED)
            .returning(
                models.Slot.start_price, models.Slot.min_price,
                models.Slot.starts_at, models.Slot.published_at,
            )
        )
    ).one_or_none()
    if row is None:
        await db_session.rollback()
        return BookResult(BookOutcome.TAKEN)
    price = pricing.calc_current_price(*row, now)
    booking_id = await _insert_booking(
        db_session, buyer_id, slot_id, price, now
    )
    await db_session.commit()
    details = await _load_details(db_session, booking_id)
    return BookResult(BookOutcome.BOOKED, details=details)


async def cancel_booking(
    db_session: sa_asyncio.AsyncSession,
    buyer_id: int,
    booking_id: int,
    now: datetime.datetime,
) -> CancelResult:
    """Отменяет бронь покупателем и возвращает слот в ленту (B4.5).

    Args:
        db_session: Сессия базы.
        buyer_id: ID покупателя, который отменяет.
        booking_id: ID брони.
        now: Время отмены.

    Returns:
        CANCELLED, TOO_LATE (до начала меньше часа) или NOT_FOUND
        (чужая, уже отменённая или несуществующая бронь).
    """
    slot_id = (
        await db_session.execute(
            sqlalchemy.update(models.Booking)
            .where(
                models.Booking.id == booking_id,
                models.Booking.buyer_id == buyer_id,
                models.Booking.status == models.BookingStatus.ACTIVE,
                models.Booking.slot_id.in_(
                    sqlalchemy.select(models.Slot.id).where(
                        models.Slot.starts_at >= now + CANCEL_DEADLINE
                    )
                ),
            )
            .values(
                status=models.BookingStatus.CANCELLED,
                cancelled_at=now,
                cancelled_by=models.CancelledBy.BUYER,
            )
            .returning(models.Booking.slot_id)
        )
    ).scalar_one_or_none()
    if slot_id is None:
        await db_session.rollback()
        outcome = await _cancel_rejection(db_session, buyer_id, booking_id)
        return CancelResult(outcome)
    # Отмена возможна не позднее чем за час, поэтому до начала всегда
    # больше 30 минут и слот возвращается в ленту (B4.5).
    await _reopen_slot(db_session, slot_id, now)
    db_session.add(
        models.Event(
            user_id=buyer_id, type=EVENT_BOOKING_CANCELLED,
            payload={"booking_id": booking_id, "by": "buyer"},
        )
    )
    await db_session.commit()
    details = await _load_details(db_session, booking_id)
    return CancelResult(CancelOutcome.CANCELLED, details=details)


async def withdraw_booked_slot(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    slot_id: int,
    now: datetime.datetime,
) -> BookingDetails | None:
    """Снимает забронированный слот владельцем: booked → withdrawn (B9).

    Бронь отменяется с пометкой «отменил бизнес».

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца, который снимает слот.
        slot_id: ID слота.
        now: Время снятия.

    Returns:
        Данные отменённой брони для уведомления покупателя или None,
        если слот не забронирован или чужой.
    """
    slots.check_transition(
        models.SlotStatus.BOOKED, models.SlotStatus.WITHDRAWN
    )
    withdrawn_id = (
        await db_session.execute(
            sqlalchemy.update(models.Slot)
            .where(
                models.Slot.id == slot_id,
                models.Slot.status == models.SlotStatus.BOOKED,
                models.Slot.service_id.in_(slots.owner_service_ids(owner_id)),
            )
            .values(status=models.SlotStatus.WITHDRAWN)
            .returning(models.Slot.id)
        )
    ).scalar_one_or_none()
    if withdrawn_id is None:
        await db_session.rollback()
        return None
    booking_id = (
        await db_session.execute(
            sqlalchemy.update(models.Booking)
            .where(
                models.Booking.slot_id == slot_id,
                models.Booking.status == models.BookingStatus.ACTIVE,
            )
            .values(
                status=models.BookingStatus.CANCELLED,
                cancelled_at=now,
                cancelled_by=models.CancelledBy.OWNER,
            )
            .returning(models.Booking.id)
        )
    ).scalar_one()
    db_session.add(
        models.Event(
            user_id=owner_id, type=EVENT_BOOKING_CANCELLED,
            payload={"booking_id": booking_id, "by": "owner"},
        )
    )
    await db_session.commit()
    return await _load_details(db_session, booking_id)


async def list_buyer_bookings(
    db_session: sa_asyncio.AsyncSession,
    buyer_id: int,
    now: datetime.datetime,
) -> list[BookingDetails]:
    """Возвращает активные брони покупателя на будущие слоты для /my.

    Args:
        db_session: Сессия базы.
        buyer_id: ID покупателя.
        now: Текущее время.

    Returns:
        Брони по времени начала.
    """
    return await _select_details(
        db_session,
        models.Booking.buyer_id == buyer_id,
        models.Booking.status == models.BookingStatus.ACTIVE,
        models.Slot.starts_at > now,
    )


async def find_buyer_booking(
    db_session: sa_asyncio.AsyncSession, buyer_id: int, booking_id: int
) -> BookingDetails | None:
    """Ищет активную бронь, если она принадлежит этому покупателю.

    Args:
        db_session: Сессия базы.
        buyer_id: ID покупателя.
        booking_id: ID брони, например из кнопки.

    Returns:
        Данные брони или None, если она чужая, отменена или её нет.
    """
    found = await _select_details(
        db_session,
        models.Booking.id == booking_id,
        models.Booking.buyer_id == buyer_id,
        models.Booking.status == models.BookingStatus.ACTIVE,
    )
    return found[0] if found else None


async def due_notices(
    db_session: sa_asyncio.AsyncSession, now: datetime.datetime
) -> list[DueNotice]:
    """Находит напоминания и вопросы «пришёл или нет», которые пора слать.

    Напоминание «за 2 часа» не приходит, если бронь сделана позже,
    и не приходит вместе с «за 30 минут» (B3.2, пункт 5).

    Args:
        db_session: Сессия базы.
        now: Текущее время.

    Returns:
        Ещё не отправленные напоминания покупателям и вопросы владельцам.
    """
    candidates = await _select_details(
        db_session,
        models.Booking.status == models.BookingStatus.ACTIVE,
        models.Slot.status == models.SlotStatus.BOOKED,
        models.Slot.starts_at <= now + REMINDER_2H_LEAD,
    )
    sent = await _sent_notices(
        db_session, [details.booking_id for details in candidates]
    )
    due = []
    for details in candidates:
        for kind in _due_kinds(details, now):
            if (kind, details.booking_id) not in sent:
                due.append(DueNotice(kind=kind, details=details))
    return due


async def mark_notified(
    db_session: sa_asyncio.AsyncSession, kind: NoticeKind, booking_id: int
) -> None:
    """Запоминает, что напоминание или вопрос по брони отправлен (B4.5).

    Args:
        db_session: Сессия базы.
        kind: Что отправлено.
        booking_id: ID брони.
    """
    db_session.add(
        models.Event(type=kind.value, payload={"booking_id": booking_id})
    )
    await db_session.commit()


async def record_visit(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    booking_id: int,
    has_come: bool,
    now: datetime.datetime,
) -> VisitResult:
    """Отмечает визит: пришёл → done, не пришёл → no_show (B4.5, B7).

    После второй неявки покупатель блокируется на 30 дней, а счётчик
    неявок обнуляется.

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца, который отвечает.
        booking_id: ID брони.
        has_come: True — клиент пришёл, False — не пришёл.
        now: Время ответа.

    Returns:
        Итог отметки. Повторное нажатие получает NOT_FOUND.
    """
    booking_status, slot_status = _visit_statuses(has_come)
    slots.check_transition(models.SlotStatus.BOOKED, slot_status)
    row = (
        await db_session.execute(
            sqlalchemy.update(models.Booking)
            .where(
                models.Booking.id == booking_id,
                models.Booking.status == models.BookingStatus.ACTIVE,
                models.Booking.slot_id.in_(
                    sqlalchemy.select(models.Slot.id).where(
                        models.Slot.service_id.in_(
                            slots.owner_service_ids(owner_id)
                        ),
                        models.Slot.starts_at <= now,
                    )
                ),
            )
            .values(status=booking_status)
            .returning(models.Booking.slot_id, models.Booking.buyer_id)
        )
    ).one_or_none()
    if row is None:
        await db_session.rollback()
        return VisitResult(VisitOutcome.NOT_FOUND)
    slot_id, buyer_id = row
    await _set_slot_status(db_session, [slot_id], slot_status)
    blocked_until = None
    if not has_come:
        blocked_until = await _count_no_show(db_session, buyer_id, now)
    await db_session.commit()
    return VisitResult(
        VisitOutcome.RECORDED,
        details=await _load_details(db_session, booking_id),
        buyer_blocked_until=blocked_until,
    )


async def auto_confirm_due(
    db_session: sa_asyncio.AsyncSession, now: datetime.datetime
) -> list[int]:
    """Считает визит состоявшимся, если владелец молчит 24 часа (B4.5).

    Args:
        db_session: Сессия базы.
        now: Текущее время.

    Returns:
        ID подтверждённых броней.
    """
    slots.check_transition(models.SlotStatus.BOOKED, models.SlotStatus.DONE)
    rows = (
        await db_session.execute(
            sqlalchemy.update(models.Booking)
            .where(
                models.Booking.status == models.BookingStatus.ACTIVE,
                models.Booking.slot_id.in_(
                    sqlalchemy.select(models.Slot.id).where(
                        models.Slot.starts_at <= now - AUTO_CONFIRM_DELAY
                    )
                ),
            )
            .values(status=models.BookingStatus.DONE)
            .returning(models.Booking.id, models.Booking.slot_id)
        )
    ).all()
    await _set_slot_status(
        db_session, [slot_id for _, slot_id in rows], models.SlotStatus.DONE
    )
    await db_session.commit()
    return [booking_id for booking_id, _ in rows]


async def count_owner_withdrawals(
    db_session: sa_asyncio.AsyncSession, owner_id: int
) -> int:
    """Считает снятия слотов владельцем после брони за всё время (B9).

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца.

    Returns:
        Число броней, отменённых бизнесом.
    """
    return await db_session.scalar(
        sqlalchemy.select(sqlalchemy.func.count(models.Booking.id))
        .join(models.Slot, models.Slot.id == models.Booking.slot_id)
        .where(
            models.Booking.cancelled_by == models.CancelledBy.OWNER,
            models.Slot.service_id.in_(slots.owner_service_ids(owner_id)),
        )
    )


def should_alert_admins(withdrawals: int) -> bool:
    """Проверяет, пора ли сообщить админам о снятиях после брони (B9).

    Args:
        withdrawals: Сколько раз бизнес снял слоты после брони.

    Returns:
        True на третьем и каждом следующем снятии.
    """
    return withdrawals >= OWNER_WITHDRAWALS_ALERT


def _due_kinds(
    details: BookingDetails, now: datetime.datetime
) -> list[NoticeKind]:
    lead = details.starts_at - now
    if now >= details.starts_at + VISIT_QUESTION_DELAY:
        return [NoticeKind.VISIT_ASKED]
    if datetime.timedelta(0) < lead <= REMINDER_30M_LEAD:
        return [NoticeKind.REMINDER_30M]
    is_booked_early = (
        details.booked_at <= details.starts_at - REMINDER_2H_LEAD
    )
    if REMINDER_30M_LEAD < lead <= REMINDER_2H_LEAD and is_booked_early:
        return [NoticeKind.REMINDER_2H]
    return []


async def _sent_notices(
    db_session: sa_asyncio.AsyncSession, booking_ids: list[int]
) -> set[tuple[NoticeKind, int]]:
    if not booking_ids:
        return set()
    booking_id = models.Event.payload["booking_id"].as_integer()
    rows = await db_session.execute(
        sqlalchemy.select(models.Event.type, booking_id).where(
            models.Event.type.in_([kind.value for kind in NoticeKind]),
            booking_id.in_(booking_ids),
        )
    )
    return {(NoticeKind(kind), event_booking_id)
            for kind, event_booking_id in rows}


def _visit_statuses(
    has_come: bool,
) -> tuple[models.BookingStatus, models.SlotStatus]:
    if has_come:
        return models.BookingStatus.DONE, models.SlotStatus.DONE
    return models.BookingStatus.NO_SHOW, models.SlotStatus.NO_SHOW


async def _set_slot_status(
    db_session: sa_asyncio.AsyncSession,
    slot_ids: list[int],
    new_status: models.SlotStatus,
) -> None:
    if not slot_ids:
        return
    await db_session.execute(
        sqlalchemy.update(models.Slot)
        .where(
            models.Slot.id.in_(slot_ids),
            models.Slot.status == models.SlotStatus.BOOKED,
        )
        .values(status=new_status)
    )


async def _count_no_show(
    db_session: sa_asyncio.AsyncSession,
    buyer_id: int,
    now: datetime.datetime,
) -> datetime.datetime | None:
    # Увеличиваем счётчик в самой базе, чтобы два ответа подряд
    # не потеряли неявку.
    no_show_count = (
        await db_session.execute(
            sqlalchemy.update(models.User)
            .where(models.User.id == buyer_id)
            .values(no_show_count=models.User.no_show_count + 1)
            .returning(models.User.no_show_count)
        )
    ).scalar_one()
    if no_show_count < NO_SHOW_LIMIT:
        return None
    blocked_until = now + BLOCK_PERIOD
    await db_session.execute(
        sqlalchemy.update(models.User)
        .where(models.User.id == buyer_id)
        .values(blocked_until=blocked_until, no_show_count=0)
    )
    return blocked_until


async def _check_buyer(
    db_session: sa_asyncio.AsyncSession,
    buyer_id: int,
    slot_id: int,
    now: datetime.datetime,
) -> BookResult | None:
    buyer = await db_session.get(models.User, buyer_id)
    if buyer.blocked_until is not None and buyer.blocked_until > now:
        return BookResult(
            BookOutcome.BLOCKED, blocked_until=buyer.blocked_until
        )
    if buyer.phone is None:
        return BookResult(BookOutcome.NO_PHONE)
    owner_id = await db_session.scalar(
        sqlalchemy.select(models.Business.owner_id)
        .join(models.Service, models.Service.business_id == models.Business.id)
        .join(models.Slot, models.Slot.service_id == models.Service.id)
        .where(models.Slot.id == slot_id)
    )
    if owner_id is None:
        return BookResult(BookOutcome.TAKEN)
    if owner_id == buyer_id:
        return BookResult(BookOutcome.OWN_SLOT)
    # Лимит проверяется чтением до брони. Если один покупатель жмёт
    # из двух окон сразу, он может получить третью бронь; для MVP это
    # допустимо, гонку между разными покупателями решает UPDATE.
    active_count = await db_session.scalar(
        sqlalchemy.select(sqlalchemy.func.count(models.Booking.id))
        .join(models.Slot, models.Slot.id == models.Booking.slot_id)
        .where(
            models.Booking.buyer_id == buyer_id,
            models.Booking.status == models.BookingStatus.ACTIVE,
            models.Slot.starts_at > now,
        )
    )
    if active_count >= MAX_ACTIVE_BOOKINGS:
        return BookResult(BookOutcome.LIMIT_REACHED)
    return None


async def _insert_booking(
    db_session: sa_asyncio.AsyncSession,
    buyer_id: int,
    slot_id: int,
    price: int,
    now: datetime.datetime,
) -> int:
    # Цена в брони фиксируется и дальше не пересчитывается (B4.4).
    booking = models.Booking(
        slot_id=slot_id, buyer_id=buyer_id, price=price,
        status=models.BookingStatus.ACTIVE, created_at=now,
    )
    db_session.add(booking)
    await db_session.execute(
        sqlalchemy.update(models.Slot)
        .where(models.Slot.id == slot_id)
        .values(current_price=price)
    )
    await db_session.flush()
    db_session.add(
        models.Event(
            user_id=buyer_id, type=EVENT_SLOT_BOOKED,
            payload={"slot_id": slot_id, "booking_id": booking.id},
        )
    )
    return booking.id


async def _reopen_slot(
    db_session: sa_asyncio.AsyncSession,
    slot_id: int,
    now: datetime.datetime,
) -> None:
    slots.check_transition(models.SlotStatus.BOOKED, models.SlotStatus.OPEN)
    slot = await db_session.get(models.Slot, slot_id)
    # Цена в ленте сразу актуальная, не дожидаясь планировщика.
    price = pricing.calc_current_price(
        slot.start_price, slot.min_price, slot.starts_at,
        slot.published_at, now,
    )
    await db_session.execute(
        sqlalchemy.update(models.Slot)
        .where(
            models.Slot.id == slot_id,
            models.Slot.status == models.SlotStatus.BOOKED,
        )
        .values(status=models.SlotStatus.OPEN, current_price=price)
    )


async def _cancel_rejection(
    db_session: sa_asyncio.AsyncSession, buyer_id: int, booking_id: int
) -> CancelOutcome:
    is_active = await db_session.scalar(
        sqlalchemy.select(
            sqlalchemy.exists().where(
                models.Booking.id == booking_id,
                models.Booking.buyer_id == buyer_id,
                models.Booking.status == models.BookingStatus.ACTIVE,
            )
        )
    )
    return CancelOutcome.TOO_LATE if is_active else CancelOutcome.NOT_FOUND


async def _load_details(
    db_session: sa_asyncio.AsyncSession, booking_id: int
) -> BookingDetails:
    [details] = await _select_details(
        db_session, models.Booking.id == booking_id
    )
    return details


async def _select_details(
    db_session: sa_asyncio.AsyncSession,
    *conditions: sqlalchemy.ColumnElement[bool],
) -> list[BookingDetails]:
    owner = orm.aliased(models.User)
    buyer = orm.aliased(models.User)
    rows = await db_session.execute(
        sqlalchemy.select(
            models.Booking.id, models.Booking.slot_id, models.Booking.price,
            models.Slot.starts_at, models.Service.title,
            models.Business.name, models.Business.address,
            models.Business.phone, owner.tg_id, buyer.tg_id, buyer.name,
            buyer.phone, models.Booking.created_at,
        )
        .join(models.Slot, models.Slot.id == models.Booking.slot_id)
        .join(models.Service, models.Service.id == models.Slot.service_id)
        .join(
            models.Business, models.Business.id == models.Service.business_id
        )
        .join(owner, owner.id == models.Business.owner_id)
        .join(buyer, buyer.id == models.Booking.buyer_id)
        .where(*conditions)
        .order_by(models.Slot.starts_at, models.Booking.id)
    )
    return [BookingDetails(*row) for row in rows]

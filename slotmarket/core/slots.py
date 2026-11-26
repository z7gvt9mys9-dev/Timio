"""Слоты: создание, переходы статусов, закрытие и цена (B4.3, B4.4, B7)."""

import dataclasses
import datetime

import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from core import pricing
from db import models

MIN_LEAD = datetime.timedelta(hours=2)
MAX_LEAD = datetime.timedelta(hours=48)
EXPIRE_LEAD = datetime.timedelta(minutes=30)
_TIME_FORMAT = "%H:%M"
_ONE_DAY = datetime.timedelta(days=1)
_MIDNIGHT = datetime.time(0, 0)
_TIME_ERROR = "Время в формате ЧЧ:ММ. Пример: 15:00"
_LEAD_ERROR = (
    "Слот можно выставить не раньше чем за 48 часов и не позже чем "
    "за 2 часа до начала."
)
_NOT_APPROVED_ERROR = "Слоты можно выставлять после одобрения заявки."
_SERVICE_ERROR = "Услуга не найдена или скрыта."

# Таблица переходов B7. Других переходов нет.
ALLOWED_TRANSITIONS = frozenset({
    (models.SlotStatus.OPEN, models.SlotStatus.BOOKED),
    (models.SlotStatus.OPEN, models.SlotStatus.EXPIRED),
    (models.SlotStatus.OPEN, models.SlotStatus.WITHDRAWN),
    (models.SlotStatus.BOOKED, models.SlotStatus.OPEN),
    (models.SlotStatus.BOOKED, models.SlotStatus.WITHDRAWN),
    (models.SlotStatus.BOOKED, models.SlotStatus.DONE),
    (models.SlotStatus.BOOKED, models.SlotStatus.NO_SHOW),
})


class InvalidTransitionError(Exception):
    """Переход статуса слота не из таблицы B7."""


class SlotRejectedError(ValueError):
    """Слот нельзя создать. Текст ошибки готов для пользователя."""


@dataclasses.dataclass(frozen=True)
class SlotInput:
    """Данные нового слота. Время tz-aware, цена — целые рубли."""

    service_id: int
    starts_at: datetime.datetime
    min_price: int
    hide_name: bool


@dataclasses.dataclass(frozen=True)
class ExpiredSlot:
    """Закрытый непроданный слот для уведомления владельца (B4.7)."""

    slot_id: int
    owner_tg_id: int
    service_title: str
    starts_at: datetime.datetime


@dataclasses.dataclass(frozen=True)
class OwnerSlot:
    """Слот владельца вместе с названием услуги."""

    slot: models.Slot
    service_title: str


@dataclasses.dataclass(frozen=True)
class RepeatResult:
    """Итог «Повторить вчерашние слоты».

    Attributes:
        created_ids: ID созданных слотов: их нужно опубликовать.
        skipped: Сколько слотов пропущено.
    """

    created_ids: tuple[int, ...]
    skipped: int


def check_transition(
    from_status: models.SlotStatus, to_status: models.SlotStatus
) -> None:
    """Проверяет переход статуса слота по таблице B7.

    Args:
        from_status: Текущий статус.
        to_status: Новый статус.

    Raises:
        InvalidTransitionError: Такого перехода в B7 нет.
    """
    if (from_status, to_status) not in ALLOWED_TRANSITIONS:
        raise InvalidTransitionError(f"{from_status} → {to_status}")


def build_starts_at(
    day: datetime.date, raw_time: str, city_tz: datetime.tzinfo
) -> datetime.datetime:
    """Собирает время начала из дня и времени, введённого владельцем.

    Args:
        day: День по календарю города.
        raw_time: Время в виде ЧЧ:ММ.
        city_tz: Часовой пояс города.

    Returns:
        Начало слота, tz-aware.

    Raises:
        ValueError: Время не в формате ЧЧ:ММ. Текст содержит пример.
    """
    try:
        parsed = datetime.datetime.strptime(raw_time.strip(), _TIME_FORMAT)
    except ValueError as error:
        raise ValueError(_TIME_ERROR) from error
    return datetime.datetime.combine(day, parsed.time(), tzinfo=city_tz)


def validate_starts_at(
    starts_at: datetime.datetime, now: datetime.datetime
) -> None:
    """Проверяет окно публикации: от 2 до 48 часов до начала (B4.3).

    Args:
        starts_at: Начало слота.
        now: Текущее время.

    Raises:
        SlotRejectedError: Начало вне окна.
    """
    if not MIN_LEAD <= starts_at - now <= MAX_LEAD:
        raise SlotRejectedError(_LEAD_ERROR)


def parse_min_price(raw_text: str, regular_price: int) -> int:
    """Проверяет минимальную цену по диапазону B4.3.

    Args:
        raw_text: Текст ответа.
        regular_price: Обычная цена услуги, ₽.

    Returns:
        Минимальная цена, ₽.

    Raises:
        ValueError: Не число или вне диапазона. Текст содержит диапазон
            и пример.
    """
    price_range = pricing.calc_min_price_range(regular_price)
    error_text = (
        f"Минимальная цена — целое число рублей от {price_range.lower} "
        f"до {price_range.upper}. Пример: {price_range.upper}"
    )
    try:
        value = int(raw_text.strip())
    except ValueError as error:
        raise ValueError(error_text) from error
    if not price_range.lower <= value <= price_range.upper:
        raise ValueError(error_text)
    return value


async def create_slot(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    slot_input: SlotInput,
    now: datetime.datetime,
) -> models.Slot:
    """Публикует слот со статусом open и пишет событие для метрик.

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца.
        slot_input: Данные слота.
        now: Время публикации.

    Returns:
        Новый слот.

    Raises:
        SlotRejectedError: Бизнес не одобрен, услуга чужая или скрыта,
            время вне окна или цена вне диапазона.
    """
    service = await _find_publishable_service(
        db_session, owner_id, slot_input.service_id
    )
    validate_starts_at(slot_input.starts_at, now)
    price_range = pricing.calc_min_price_range(service.regular_price)
    if not price_range.lower <= slot_input.min_price <= price_range.upper:
        raise SlotRejectedError(
            f"Минимальная цена должна быть от {price_range.lower} "
            f"до {price_range.upper} ₽."
        )
    start_price = pricing.calc_start_price(service.regular_price)
    slot = models.Slot(
        service_id=service.id,
        starts_at=slot_input.starts_at,
        start_price=start_price,
        min_price=slot_input.min_price,
        current_price=pricing.calc_current_price(
            start_price, slot_input.min_price, slot_input.starts_at, now, now
        ),
        hide_name=slot_input.hide_name,
        status=models.SlotStatus.OPEN,
        published_at=now,
    )
    db_session.add(slot)
    await db_session.flush()
    db_session.add(
        models.Event(
            user_id=owner_id,
            type=models.EventType.SLOT_CREATED,
            payload={"slot_id": slot.id},
        )
    )
    await db_session.commit()
    return slot


async def withdraw_slot(
    db_session: sa_asyncio.AsyncSession, owner_id: int, slot_id: int
) -> bool:
    """Снимает открытый слот владельца: open → withdrawn (B4.3).

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца, который нажал кнопку.
        slot_id: ID слота.

    Returns:
        True, если слот был открыт и принадлежит владельцу.
    """
    changed_ids = await _transition(
        db_session,
        models.SlotStatus.OPEN,
        models.SlotStatus.WITHDRAWN,
        models.Slot.id == slot_id,
        models.Slot.service_id.in_(owner_service_ids(owner_id)),
    )
    return bool(changed_ids)


async def expire_due_slots(
    db_session: sa_asyncio.AsyncSession, now: datetime.datetime
) -> list[ExpiredSlot]:
    """Закрывает непроданные слоты за 30 минут до начала (B4.3).

    Args:
        db_session: Сессия базы.
        now: Текущее время.

    Returns:
        Закрытые слоты с данными для уведомления владельцев.
    """
    expired_ids = await _transition(
        db_session,
        models.SlotStatus.OPEN,
        models.SlotStatus.EXPIRED,
        models.Slot.starts_at <= now + EXPIRE_LEAD,
    )
    if not expired_ids:
        return []
    rows = await db_session.execute(
        sqlalchemy.select(
            models.Slot.id,
            models.User.tg_id,
            models.Service.title,
            models.Slot.starts_at,
        )
        .join(models.Service, models.Service.id == models.Slot.service_id)
        .join(
            models.Business, models.Business.id == models.Service.business_id
        )
        .join(models.User, models.User.id == models.Business.owner_id)
        .where(models.Slot.id.in_(expired_ids))
        .order_by(models.Slot.starts_at)
    )
    return [
        ExpiredSlot(
            slot_id=slot_id, owner_tg_id=tg_id,
            service_title=title, starts_at=starts_at,
        )
        for slot_id, tg_id, title, starts_at in rows
    ]


async def recalc_prices(
    db_session: sa_asyncio.AsyncSession, now: datetime.datetime
) -> list[int]:
    """Пересчитывает текущую цену открытых слотов (B4.4).

    Args:
        db_session: Сессия базы.
        now: Текущее время.

    Returns:
        ID слотов, у которых цена изменилась: их посты нужно обновить.
    """
    open_slots = await db_session.scalars(
        sqlalchemy.select(models.Slot).where(
            models.Slot.status == models.SlotStatus.OPEN
        )
    )
    changed_ids = []
    for slot in list(open_slots):
        price = pricing.calc_current_price(
            slot.start_price, slot.min_price, slot.starts_at,
            slot.published_at, now,
        )
        if price == slot.current_price:
            continue
        # Условие на open: слот могли забронировать во время пересчёта.
        result = await db_session.execute(
            sqlalchemy.update(models.Slot)
            .where(
                models.Slot.id == slot.id,
                models.Slot.status == models.SlotStatus.OPEN,
            )
            .values(current_price=price)
        )
        if result.rowcount > 0:
            changed_ids.append(slot.id)
    await db_session.commit()
    return changed_ids


async def list_owner_slots(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    now: datetime.datetime,
) -> list[OwnerSlot]:
    """Возвращает ближайшие открытые и забронированные слоты владельца.

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца.
        now: Текущее время.

    Returns:
        Слоты, которые ещё не начались, по времени начала.
    """
    rows = await db_session.execute(
        sqlalchemy.select(models.Slot, models.Service.title)
        .join(models.Service, models.Service.id == models.Slot.service_id)
        .where(
            models.Slot.service_id.in_(owner_service_ids(owner_id)),
            models.Slot.status.in_(
                (models.SlotStatus.OPEN, models.SlotStatus.BOOKED)
            ),
            models.Slot.starts_at > now,
        )
        .order_by(models.Slot.starts_at, models.Slot.id)
    )
    return [OwnerSlot(slot=slot, service_title=title) for slot, title in rows]


async def repeat_yesterday(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    now: datetime.datetime,
    city_tz: datetime.tzinfo,
) -> RepeatResult:
    """Копирует вчерашние слоты владельца на сегодня (B4.3).

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца.
        now: Текущее время.
        city_tz: Часовой пояс города: «вчера» считается по нему.

    Returns:
        Сколько слотов создано и сколько пропущено.
    """
    today = now.astimezone(city_tz).date()
    copies = await _yesterday_copies(db_session, owner_id, today, city_tz)
    created_ids = []
    for copy in copies:
        if await _has_same_slot(db_session, copy):
            continue
        try:
            slot = await create_slot(db_session, owner_id, copy, now)
        except SlotRejectedError:
            continue
        created_ids.append(slot.id)
    return RepeatResult(
        created_ids=tuple(created_ids),
        skipped=len(copies) - len(created_ids),
    )


async def _yesterday_copies(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    today: datetime.date,
    city_tz: datetime.tzinfo,
) -> list[SlotInput]:
    day_start = datetime.datetime.combine(
        today - _ONE_DAY, _MIDNIGHT, tzinfo=city_tz
    )
    day_end = datetime.datetime.combine(today, _MIDNIGHT, tzinfo=city_tz)
    source_slots = await db_session.scalars(
        sqlalchemy.select(models.Slot)
        .where(
            models.Slot.service_id.in_(owner_service_ids(owner_id)),
            models.Slot.status != models.SlotStatus.WITHDRAWN,
            models.Slot.starts_at >= day_start,
            models.Slot.starts_at < day_end,
        )
        .order_by(models.Slot.starts_at, models.Slot.id)
    )
    # Время копии собираем по часам города, а не прибавляем 24 часа,
    # чтобы 18:00 оставалось 18:00 и при смене часового пояса.
    return [
        SlotInput(
            service_id=slot.service_id,
            starts_at=datetime.datetime.combine(
                today, slot.starts_at.astimezone(city_tz).time(),
                tzinfo=city_tz,
            ),
            min_price=slot.min_price,
            hide_name=slot.hide_name,
        )
        for slot in source_slots
    ]


async def _has_same_slot(
    db_session: sa_asyncio.AsyncSession, slot_input: SlotInput
) -> bool:
    return bool(
        await db_session.scalar(
            sqlalchemy.select(
                sqlalchemy.exists().where(
                    models.Slot.service_id == slot_input.service_id,
                    models.Slot.starts_at == slot_input.starts_at,
                    models.Slot.status != models.SlotStatus.WITHDRAWN,
                )
            )
        )
    )


async def _find_publishable_service(
    db_session: sa_asyncio.AsyncSession, owner_id: int, service_id: int
) -> models.Service:
    row = (
        await db_session.execute(
            sqlalchemy.select(models.Service, models.Business.status)
            .join(
                models.Business,
                models.Business.id == models.Service.business_id,
            )
            .where(
                models.Service.id == service_id,
                models.Business.owner_id == owner_id,
            )
        )
    ).one_or_none()
    if row is None or not row[0].is_active:
        raise SlotRejectedError(_SERVICE_ERROR)
    if row[1] != models.BusinessStatus.APPROVED:
        raise SlotRejectedError(_NOT_APPROVED_ERROR)
    return row[0]


def owner_service_ids(owner_id: int) -> sqlalchemy.Select[tuple[int]]:
    """Подзапрос ID услуг владельца для условий «слот этого владельца».

    Args:
        owner_id: ID владельца.

    Returns:
        SELECT по services.id для использования в .in_().
    """
    return (
        sqlalchemy.select(models.Service.id)
        .join(
            models.Business, models.Business.id == models.Service.business_id
        )
        .where(models.Business.owner_id == owner_id)
    )


async def _transition(
    db_session: sa_asyncio.AsyncSession,
    from_status: models.SlotStatus,
    to_status: models.SlotStatus,
    *conditions: sqlalchemy.ColumnElement[bool],
) -> list[int]:
    # Условие на текущий статус стоит в самом UPDATE: из двух
    # одновременных действий строку изменит только одно (B4.5).
    check_transition(from_status, to_status)
    result = await db_session.execute(
        sqlalchemy.update(models.Slot)
        .where(models.Slot.status == from_status, *conditions)
        .values(status=to_status)
        .returning(models.Slot.id)
    )
    changed_ids = list(result.scalars())
    await db_session.commit()
    return changed_ids

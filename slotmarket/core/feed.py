"""Лента слотов: данные и тексты постов канала и /slots (B4.6).

Тексты в разметке HTML (зачёркнутая цена), все значения из базы
экранируются. Эмодзи и значков нет (CLAUDE.md §3).
"""

import dataclasses
import datetime
import html

import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from db import models

SLOT_DEEP_LINK_PREFIX = "slot_"
_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_POST_DATE_FORMAT = "%d.%m"
_TIME_FORMAT = "%H:%M"
_ONE_DAY = datetime.timedelta(days=1)
_MIDNIGHT = datetime.time(0, 0)
_DAY_LABELS = {0: "Сегодня", 1: "Завтра"}
# Сколько дней показывает /slots: сегодня и завтра (B8).
_LIST_DAYS = 2
_STATUS_MARKS = {
    models.SlotStatus.BOOKED: "Забронировано",
    models.SlotStatus.DONE: "Забронировано",
    models.SlotStatus.NO_SHOW: "Забронировано",
    models.SlotStatus.EXPIRED: "Время вышло",
    models.SlotStatus.WITHDRAWN: "Слот снят",
}
_PRICE_FALLS_TEXT = "Цена снижается до начала. Слот один."


@dataclasses.dataclass(frozen=True)
class SlotCard:
    """Всё, что нужно для поста и строки /slots. Адреса здесь нет.

    Название бизнеса нужно для обычного поста. В режиме скрытого
    названия оно в текст не попадает (B4.6, CLAUDE.md §5).
    """

    slot_id: int
    status: models.SlotStatus
    starts_at: datetime.datetime
    current_price: int
    regular_price: int
    hide_name: bool
    channel_msg_id: int | None
    service_title: str
    category: str
    business_name: str
    district: str


def build_deep_link_payload(slot_id: int) -> str:
    """Собирает параметр deep link для кнопки «Забронировать» (B8).

    Args:
        slot_id: ID слота.

    Returns:
        Строка вида slot_12 для t.me/<бот>?start=...
    """
    return f"{SLOT_DEEP_LINK_PREFIX}{slot_id}"


def is_bookable(card: SlotCard) -> bool:
    """Проверяет, нужна ли посту кнопка «Забронировать».

    Args:
        card: Карточка слота.

    Returns:
        True, пока слот открыт.
    """
    return card.status == models.SlotStatus.OPEN


def format_rubles(amount: int) -> str:
    """Форматирует сумму: 1500 → «1 500 ₽».

    Args:
        amount: Сумма в целых рублях.

    Returns:
        Сумма с пробелами между разрядами и знаком рубля.
    """
    return f"{amount:,}".replace(",", " ") + " ₽"


def format_slot_time(
    starts_at: datetime.datetime, city_tz: datetime.tzinfo
) -> str:
    """Показывает начало слота с днём недели: «вт, 01.12 в 18:00».

    Args:
        starts_at: Начало слота, tz-aware.
        city_tz: Часовой пояс города.

    Returns:
        День недели, дата и время по часам города.
    """
    local = starts_at.astimezone(city_tz)
    return (
        f"{_WEEKDAYS[local.weekday()]}, {local.strftime(_POST_DATE_FORMAT)}"
        f" в {local.strftime(_TIME_FORMAT)}"
    )


def build_post_text(card: SlotCard, city_tz: datetime.tzinfo) -> str:
    """Собирает текст поста канала (B4.6).

    Args:
        card: Карточка слота.
        city_tz: Часовой пояс города.

    Returns:
        Текст в HTML. Для закрытых слотов первой строкой идёт пометка.
    """
    lines = []
    if card.status in _STATUS_MARKS:
        lines.extend([_STATUS_MARKS[card.status], ""])
    lines.extend([
        html.escape(card.service_title),
        _format_place(card),
        format_slot_time(card.starts_at, city_tz),
        "",
        _format_price(card),
    ])
    if is_bookable(card):
        lines.append(_PRICE_FALLS_TEXT)
    return "\n".join(lines)


def build_list_item_text(
    card: SlotCard, now: datetime.datetime, city_tz: datetime.tzinfo
) -> str:
    """Собирает строку слота для /slots: «Сегодня, 18:00 — ...» (B4.6).

    Args:
        card: Карточка открытого слота.
        now: Текущее время: от него считаются «Сегодня» и «Завтра».
        city_tz: Часовой пояс города.

    Returns:
        Текст в HTML.
    """
    local = card.starts_at.astimezone(city_tz)
    day_offset = (local.date() - now.astimezone(city_tz).date()).days
    day_label = _DAY_LABELS.get(day_offset, local.strftime(_POST_DATE_FORMAT))
    return (
        f"{day_label}, {local.strftime(_TIME_FORMAT)} — "
        f"{html.escape(card.service_title)}, {_format_place(card)}\n"
        f"{_format_price(card)}"
    )


async def load_cards(
    db_session: sa_asyncio.AsyncSession, slot_ids: list[int]
) -> list[SlotCard]:
    """Загружает карточки слотов по ID.

    Args:
        db_session: Сессия базы.
        slot_ids: ID слотов.

    Returns:
        Карточки по времени начала. Несуществующие ID пропускаются.
    """
    if not slot_ids:
        return []
    return await _select_cards(db_session, models.Slot.id.in_(slot_ids))


async def list_open_cards(
    db_session: sa_asyncio.AsyncSession,
    now: datetime.datetime,
    city_tz: datetime.tzinfo,
    category: str | None = None,
) -> list[SlotCard]:
    """Возвращает открытые слоты на сегодня и завтра для /slots (B8).

    Args:
        db_session: Сессия базы.
        now: Текущее время.
        city_tz: Часовой пояс города: «сегодня» считается по нему.
        category: Категория для фильтра или None — все категории.

    Returns:
        Карточки по времени начала.
    """
    today = now.astimezone(city_tz).date()
    window_end = datetime.datetime.combine(
        today + _LIST_DAYS * _ONE_DAY, _MIDNIGHT, tzinfo=city_tz
    )
    conditions = [
        models.Slot.status == models.SlotStatus.OPEN,
        models.Slot.starts_at > now,
        models.Slot.starts_at < window_end,
    ]
    if category is not None:
        conditions.append(models.Business.category == category)
    return await _select_cards(db_session, *conditions)


async def list_unposted_cards(
    db_session: sa_asyncio.AsyncSession,
) -> list[SlotCard]:
    """Возвращает открытые слоты, пост которых не отправился (B9).

    Args:
        db_session: Сессия базы.

    Returns:
        Карточки открытых слотов без channel_msg_id.
    """
    return await _select_cards(
        db_session,
        models.Slot.status == models.SlotStatus.OPEN,
        models.Slot.channel_msg_id.is_(None),
    )


async def set_channel_msg_id(
    db_session: sa_asyncio.AsyncSession, slot_id: int, channel_msg_id: int
) -> None:
    """Запоминает ID поста в канале.

    Вызывать сразу после успешной отправки. Если запись упадёт, слот
    останется без channel_msg_id и следующий прогон планировщика
    отправит пост ещё раз: в канале будет дубль (B4.6).

    Args:
        db_session: Сессия базы.
        slot_id: ID слота.
        channel_msg_id: ID сообщения в канале.
    """
    await db_session.execute(
        sqlalchemy.update(models.Slot)
        .where(models.Slot.id == slot_id)
        .values(channel_msg_id=channel_msg_id)
    )
    await db_session.commit()


async def _select_cards(
    db_session: sa_asyncio.AsyncSession,
    *conditions: sqlalchemy.ColumnElement[bool],
) -> list[SlotCard]:
    rows = await db_session.execute(
        sqlalchemy.select(
            models.Slot.id,
            models.Slot.status,
            models.Slot.starts_at,
            models.Slot.current_price,
            models.Service.regular_price,
            models.Slot.hide_name,
            models.Slot.channel_msg_id,
            models.Service.title,
            models.Business.category,
            models.Business.name,
            models.Business.district,
        )
        .join(models.Service, models.Service.id == models.Slot.service_id)
        .join(
            models.Business, models.Business.id == models.Service.business_id
        )
        .where(*conditions)
        .order_by(models.Slot.starts_at, models.Slot.id)
    )
    return [SlotCard(*row) for row in rows]


def _format_place(card: SlotCard) -> str:
    # В режиме скрытого названия вместо названия — категория (B4.6).
    shown_name = card.category if card.hide_name else card.business_name
    return f"{html.escape(shown_name)}, {html.escape(card.district)}"


def _format_price(card: SlotCard) -> str:
    return (
        f"Цена сейчас: {format_rubles(card.current_price)} "
        f"(обычно <s>{format_rubles(card.regular_price)}</s>)"
    )

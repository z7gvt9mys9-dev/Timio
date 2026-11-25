"""Модели базы данных из раздела B7."""

import datetime
import enum
from typing import Any

import sqlalchemy
from sqlalchemy import orm


class BusinessStatus(enum.StrEnum):
    """Статус бизнеса."""

    PENDING = "pending"
    APPROVED = "approved"
    BLOCKED = "blocked"


class SlotStatus(enum.StrEnum):
    """Статус слота. Допустимые переходы — в таблице B7."""

    OPEN = "open"
    BOOKED = "booked"
    DONE = "done"
    NO_SHOW = "no_show"
    EXPIRED = "expired"
    WITHDRAWN = "withdrawn"


class BookingStatus(enum.StrEnum):
    """Статус брони."""

    ACTIVE = "active"
    CANCELLED = "cancelled"
    DONE = "done"
    NO_SHOW = "no_show"


class CancelledBy(enum.StrEnum):
    """Кто отменил бронь."""

    BUYER = "buyer"
    OWNER = "owner"


class CalcSource(enum.StrEnum):
    """Откуда пришёл расчёт калькулятора."""

    BOT = "bot"
    WEB = "web"


class UtcDateTime(sqlalchemy.types.TypeDecorator):
    """Время в UTC: в базу пишется UTC, из базы читается aware-datetime.

    SQLite не хранит часовой пояс, поэтому без этого типа из базы
    возвращались бы наивные datetime.
    """

    impl = sqlalchemy.DateTime
    cache_ok = True

    def process_bind_param(
        self, value: datetime.datetime | None, dialect: sqlalchemy.Dialect
    ) -> datetime.datetime | None:
        """Переводит время в UTC перед записью.

        Raises:
            ValueError: Если передан наивный datetime.
        """
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Наивный datetime запрещён, нужен tz-aware")
        return value.astimezone(datetime.UTC).replace(tzinfo=None)

    def process_result_value(
        self, value: datetime.datetime | None, dialect: sqlalchemy.Dialect
    ) -> datetime.datetime | None:
        """Помечает прочитанное время как UTC."""
        if value is None:
            return None
        return value.replace(tzinfo=datetime.UTC)


def utc_now() -> datetime.datetime:
    """Возвращает текущее время в UTC."""
    return datetime.datetime.now(datetime.UTC)


def _enum_column(enum_class: type[enum.StrEnum]) -> sqlalchemy.Enum:
    # В базе храним значения ("open"), а не имена членов ("OPEN"),
    # чтобы SQL из B4.5 совпадал с Enum буквально.
    return sqlalchemy.Enum(
        enum_class,
        native_enum=False,
        create_constraint=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(orm.DeclarativeBase):
    """Базовый класс моделей."""

    type_annotation_map = {datetime.datetime: UtcDateTime}


class User(Base):
    """Пользователь Telegram: владелец, покупатель или оба сразу."""

    __tablename__ = "users"

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    tg_id: orm.Mapped[int] = orm.mapped_column(
        sqlalchemy.BigInteger, unique=True
    )
    name: orm.Mapped[str]
    phone: orm.Mapped[str | None]
    is_owner: orm.Mapped[bool] = orm.mapped_column(default=False)
    no_show_count: orm.Mapped[int] = orm.mapped_column(default=0)
    blocked_until: orm.Mapped[datetime.datetime | None]
    consent_at: orm.Mapped[datetime.datetime | None]
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        default=utc_now
    )


class Business(Base):
    """Бизнес владельца."""

    __tablename__ = "businesses"

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    owner_id: orm.Mapped[int] = orm.mapped_column(
        sqlalchemy.ForeignKey("users.id")
    )
    name: orm.Mapped[str]
    category: orm.Mapped[str]
    district: orm.Mapped[str]
    address: orm.Mapped[str]
    phone: orm.Mapped[str]
    status: orm.Mapped[BusinessStatus] = orm.mapped_column(
        _enum_column(BusinessStatus), default=BusinessStatus.PENDING
    )
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        default=utc_now
    )


class Service(Base):
    """Услуга бизнеса. Деньги — целые рубли."""

    __tablename__ = "services"

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    business_id: orm.Mapped[int] = orm.mapped_column(
        sqlalchemy.ForeignKey("businesses.id")
    )
    title: orm.Mapped[str]
    duration_min: orm.Mapped[int]
    regular_price: orm.Mapped[int]
    is_active: orm.Mapped[bool] = orm.mapped_column(default=True)


class Slot(Base):
    """Свободное время, выставленное на продажу. Цены — целые рубли."""

    __tablename__ = "slots"
    __table_args__ = (
        sqlalchemy.Index("ix_slots_status_starts_at", "status", "starts_at"),
    )

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    service_id: orm.Mapped[int] = orm.mapped_column(
        sqlalchemy.ForeignKey("services.id")
    )
    starts_at: orm.Mapped[datetime.datetime]
    start_price: orm.Mapped[int]
    min_price: orm.Mapped[int]
    current_price: orm.Mapped[int]
    hide_name: orm.Mapped[bool] = orm.mapped_column(default=False)
    status: orm.Mapped[SlotStatus] = orm.mapped_column(
        _enum_column(SlotStatus), default=SlotStatus.OPEN
    )
    channel_msg_id: orm.Mapped[int | None]
    published_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        default=utc_now
    )


class Booking(Base):
    """Бронь слота. Цена фиксируется в момент брони и не меняется."""

    __tablename__ = "bookings"
    __table_args__ = (
        sqlalchemy.Index("ix_bookings_buyer_id_status", "buyer_id", "status"),
    )

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    slot_id: orm.Mapped[int] = orm.mapped_column(
        sqlalchemy.ForeignKey("slots.id")
    )
    buyer_id: orm.Mapped[int] = orm.mapped_column(
        sqlalchemy.ForeignKey("users.id")
    )
    price: orm.Mapped[int]
    status: orm.Mapped[BookingStatus] = orm.mapped_column(
        _enum_column(BookingStatus), default=BookingStatus.ACTIVE
    )
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        default=utc_now
    )
    cancelled_at: orm.Mapped[datetime.datetime | None]
    cancelled_by: orm.Mapped[CancelledBy | None] = orm.mapped_column(
        _enum_column(CancelledBy)
    )


class CalcReport(Base):
    """Результат калькулятора потерь (B4.1). Деньги — целые рубли."""

    __tablename__ = "calc_reports"

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    user_id: orm.Mapped[int | None] = orm.mapped_column(
        sqlalchemy.ForeignKey("users.id")
    )
    token: orm.Mapped[str | None] = orm.mapped_column(unique=True)
    source: orm.Mapped[CalcSource] = orm.mapped_column(
        _enum_column(CalcSource)
    )
    seats: orm.Mapped[int]
    hours_per_day: orm.Mapped[int]
    days_per_month: orm.Mapped[int]
    load_pct: orm.Mapped[int]
    revenue_per_hour: orm.Mapped[int]
    variable_cost: orm.Mapped[int]
    weak_slots: orm.Mapped[str | None]
    lost_revenue: orm.Mapped[int]
    recoverable: orm.Mapped[int]
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        default=utc_now
    )


class Event(Base):
    """Значимое действие пользователя для метрик (B10)."""

    __tablename__ = "events"

    id: orm.Mapped[int] = orm.mapped_column(primary_key=True)
    user_id: orm.Mapped[int | None] = orm.mapped_column(
        sqlalchemy.ForeignKey("users.id")
    )
    type: orm.Mapped[str]
    payload: orm.Mapped[dict[str, Any]] = orm.mapped_column(
        sqlalchemy.JSON, default=dict
    )
    created_at: orm.Mapped[datetime.datetime] = orm.mapped_column(
        default=utc_now
    )

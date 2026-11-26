"""Бизнесы, услуги и одобрение заявок (B4.2, B4.9)."""

import dataclasses
import enum
import functools
import re

import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from db import models

# Временный список до выбора ниши (B15, вопрос 2).
CATEGORIES = (
    "барбершоп",
    "салон красоты",
    "фотостудия",
    "репетитор",
    "переговорная",
)
MAX_TEXT_LENGTH = 100
MIN_DURATION_MIN = 10
MAX_DURATION_MIN = 480
MIN_REGULAR_PRICE = 100
MAX_REGULAR_PRICE = 100_000
_PHONE_ALLOWED = re.compile(r"[\d\s()+-]+")
_NON_DIGITS = re.compile(r"\D")
_PHONE_DIGITS = 10
_PHONE_TRUNK_PREFIXES = ("7", "8")
# Иностранный номер: от 11 цифр с кодом страны до предела E.164.
_FOREIGN_PHONE_MIN_DIGITS = 11
_FOREIGN_PHONE_MAX_DIGITS = 15
_PHONE_ERROR = "Телефон — номер с кодом страны. Пример: +79001234567"
_CATEGORY_ERROR = "Выберите категорию кнопкой ниже."


class TextField(enum.StrEnum):
    """Текстовые поля бизнеса и услуги."""

    BUSINESS_NAME = "business_name"
    DISTRICT = "district"
    ADDRESS = "address"
    SERVICE_TITLE = "service_title"


_TEXT_EXAMPLES = {
    TextField.BUSINESS_NAME: "Барбершоп «Борода»",
    TextField.DISTRICT: "Хамовники",
    TextField.ADDRESS: "ул. Ленина, 1",
    TextField.SERVICE_TITLE: "Мужская стрижка",
}


class ServiceField(enum.StrEnum):
    """Поле услуги, которое можно изменить (B4.2)."""

    TITLE = "title"
    DURATION_MIN = "duration_min"
    REGULAR_PRICE = "regular_price"


class ServiceUpdateResult(enum.StrEnum):
    """Чем закончилась правка услуги."""

    UPDATED = "updated"
    NOT_FOUND = "not_found"
    DURATION_LOCKED = "duration_locked"


# Слоты, для которых длительность услуги ещё важна (B4.2).
_ACTIVE_SLOT_STATUSES = (models.SlotStatus.OPEN, models.SlotStatus.BOOKED)


class BusinessAlreadyExistsError(Exception):
    """У владельца уже есть бизнес: в MVP он один (B4.2)."""


@dataclasses.dataclass(frozen=True)
class BusinessInput:
    """Поля нового бизнеса, уже проверенные."""

    name: str
    category: str
    district: str
    address: str
    phone: str


@dataclasses.dataclass(frozen=True)
class ServiceInput:
    """Поля новой услуги, уже проверенные. Цена — целые рубли."""

    title: str
    duration_min: int
    regular_price: int


@dataclasses.dataclass(frozen=True)
class BusinessDecision:
    """Результат решения по заявке.

    Attributes:
        business: Бизнес с новым статусом.
        owner_tg_id: Кому отправить уведомление (B4.7).
    """

    business: models.Business
    owner_tg_id: int


@dataclasses.dataclass(frozen=True)
class BlockedBusiness:
    """Результат блокировки бизнеса админом (B4.9).

    Attributes:
        business: Бизнес в статусе blocked.
        channel_msg_ids: Посты открытых слотов, которые нужно удалить
            из канала. У слотов channel_msg_id уже очищен, поэтому после
            разблокировки планировщик опубликует их заново.
    """

    business: models.Business
    channel_msg_ids: list[int]


def parse_text_field(field: TextField, raw_text: str) -> str:
    """Проверяет текстовое поле: не пустое и не длиннее лимита.

    Args:
        field: Какое поле вводится.
        raw_text: Текст ответа.

    Returns:
        Текст без пробелов по краям.

    Raises:
        ValueError: Поле пустое или слишком длинное. Текст ошибки
            содержит пример.
    """
    text = raw_text.strip()
    if not text or len(text) > MAX_TEXT_LENGTH:
        raise ValueError(
            f"Нужен текст от 1 до {MAX_TEXT_LENGTH} символов. "
            f"Пример: {_TEXT_EXAMPLES[field]}"
        )
    return text


def parse_category(raw_text: str) -> str:
    """Проверяет, что категория есть в списке CATEGORIES.

    Args:
        raw_text: Выбранная категория.

    Returns:
        Категория из списка.

    Raises:
        ValueError: Категории нет в списке.
    """
    if raw_text not in CATEGORIES:
        raise ValueError(_CATEGORY_ERROR)
    return raw_text


def parse_phone(raw_text: str) -> str:
    """Приводит телефон к одному виду для бизнеса и покупателя (B3.2).

    Args:
        raw_text: Номер в любом привычном виде: +7 900 123-45-67,
            8(900)1234567, 9001234567, +380 50 123 45 67.

    Returns:
        Российский номер в виде +7(900)123-45-67, остальные — +<цифры>.

    Raises:
        ValueError: Это не номер телефона. Текст ошибки содержит пример.
    """
    text = raw_text.strip()
    if not _PHONE_ALLOWED.fullmatch(text):
        raise ValueError(_PHONE_ERROR)
    digits = _NON_DIGITS.sub("", text)
    has_trunk = len(digits) == _PHONE_DIGITS + 1
    if has_trunk and digits[0] in _PHONE_TRUNK_PREFIXES:
        digits = digits[1:]
    if len(digits) == _PHONE_DIGITS:
        return (
            f"+7({digits[0:3]}){digits[3:6]}-{digits[6:8]}-{digits[8:10]}"
        )
    if _FOREIGN_PHONE_MIN_DIGITS <= len(digits) <= _FOREIGN_PHONE_MAX_DIGITS:
        return f"+{digits}"
    raise ValueError(_PHONE_ERROR)


def parse_duration(raw_text: str) -> int:
    """Проверяет длительность услуги в минутах.

    Args:
        raw_text: Текст ответа.

    Returns:
        Длительность в минутах.

    Raises:
        ValueError: Не число или вне диапазона.
    """
    return _parse_int_in_range(
        raw_text,
        MIN_DURATION_MIN,
        MAX_DURATION_MIN,
        f"Длительность — целое число минут от {MIN_DURATION_MIN} "
        f"до {MAX_DURATION_MIN}. Пример: 60",
    )


def parse_price(raw_text: str) -> int:
    """Проверяет обычную цену услуги в рублях.

    Args:
        raw_text: Текст ответа.

    Returns:
        Цена в целых рублях.

    Raises:
        ValueError: Не число или вне диапазона.
    """
    return _parse_int_in_range(
        raw_text,
        MIN_REGULAR_PRICE,
        MAX_REGULAR_PRICE,
        f"Цена — целое число рублей от {MIN_REGULAR_PRICE} "
        f"до {MAX_REGULAR_PRICE}. Пример: 1500",
    )


_SERVICE_PARSERS = {
    ServiceField.TITLE: functools.partial(
        parse_text_field, TextField.SERVICE_TITLE
    ),
    ServiceField.DURATION_MIN: parse_duration,
    ServiceField.REGULAR_PRICE: parse_price,
}


async def find_owner_business(
    db_session: sa_asyncio.AsyncSession, owner_id: int
) -> models.Business | None:
    """Ищет бизнес владельца.

    Args:
        db_session: Сессия базы.
        owner_id: ID пользователя-владельца.

    Returns:
        Бизнес или None, если владелец его ещё не зарегистрировал.
    """
    return await db_session.scalar(
        sqlalchemy.select(models.Business).where(
            models.Business.owner_id == owner_id
        )
    )


async def create_business(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    business_input: BusinessInput,
) -> models.Business:
    """Создаёт заявку бизнеса со статусом pending.

    Args:
        db_session: Сессия базы.
        owner_id: ID пользователя-владельца.
        business_input: Проверенные поля бизнеса.

    Returns:
        Новый бизнес.

    Raises:
        BusinessAlreadyExistsError: У владельца уже есть бизнес.
    """
    if await find_owner_business(db_session, owner_id) is not None:
        raise BusinessAlreadyExistsError(f"owner_id={owner_id}")
    business = models.Business(
        owner_id=owner_id, **dataclasses.asdict(business_input)
    )
    db_session.add(business)
    await db_session.commit()
    return business


async def list_pending(
    db_session: sa_asyncio.AsyncSession,
) -> list[models.Business]:
    """Возвращает заявки на проверке, старые первыми.

    Args:
        db_session: Сессия базы.

    Returns:
        Бизнесы в статусе pending.
    """
    result = await db_session.scalars(
        sqlalchemy.select(models.Business)
        .where(models.Business.status == models.BusinessStatus.PENDING)
        .order_by(models.Business.created_at, models.Business.id)
    )
    return list(result)


async def approve_business(
    db_session: sa_asyncio.AsyncSession, business_id: int
) -> BusinessDecision | None:
    """Одобряет заявку, если её ещё никто не рассмотрел.

    Args:
        db_session: Сессия базы.
        business_id: ID бизнеса.

    Returns:
        Решение или None, если заявка уже рассмотрена.
    """
    return await _decide(
        db_session, business_id, models.BusinessStatus.APPROVED
    )


async def reject_business(
    db_session: sa_asyncio.AsyncSession, business_id: int
) -> BusinessDecision | None:
    """Отклоняет заявку: бизнес получает статус blocked (B4.2).

    Args:
        db_session: Сессия базы.
        business_id: ID бизнеса.

    Returns:
        Решение или None, если заявка уже рассмотрена.
    """
    return await _decide(
        db_session, business_id, models.BusinessStatus.BLOCKED
    )


async def block_business(
    db_session: sa_asyncio.AsyncSession, business_id: int
) -> BlockedBusiness | None:
    """Блокирует бизнес и убирает его открытые слоты из ленты (B4.9).

    Статусы слотов не меняются: открытые закроются по времени, брони
    доживают, а новые брони не пройдут.

    Args:
        db_session: Сессия базы.
        business_id: ID бизнеса.

    Returns:
        Результат или None, если бизнеса нет или он уже заблокирован.
    """
    result = await db_session.execute(
        sqlalchemy.update(models.Business)
        .where(
            models.Business.id == business_id,
            models.Business.status != models.BusinessStatus.BLOCKED,
        )
        .values(status=models.BusinessStatus.BLOCKED)
    )
    if result.rowcount == 0:
        await db_session.rollback()
        return None
    posted = _posted_open_slots(business_id)
    channel_msg_ids = list(
        await db_session.scalars(
            sqlalchemy.select(models.Slot.channel_msg_id).where(posted)
        )
    )
    await db_session.execute(
        sqlalchemy.update(models.Slot).where(posted)
        .values(channel_msg_id=None)
    )
    _add_business_event(
        db_session, models.EventType.BUSINESS_BLOCKED, business_id
    )
    await db_session.commit()
    business = await db_session.get(
        models.Business, business_id, populate_existing=True
    )
    return BlockedBusiness(business=business, channel_msg_ids=channel_msg_ids)


async def unblock_business(
    db_session: sa_asyncio.AsyncSession, business_id: int
) -> models.Business | None:
    """Снимает блокировку: бизнес получает статус approved (B4.9).

    Args:
        db_session: Сессия базы.
        business_id: ID бизнеса.

    Returns:
        Бизнес или None, если его нет или он не заблокирован.
    """
    result = await db_session.execute(
        sqlalchemy.update(models.Business)
        .where(
            models.Business.id == business_id,
            models.Business.status == models.BusinessStatus.BLOCKED,
        )
        .values(status=models.BusinessStatus.APPROVED)
    )
    if result.rowcount == 0:
        await db_session.rollback()
        return None
    _add_business_event(
        db_session, models.EventType.BUSINESS_UNBLOCKED, business_id
    )
    await db_session.commit()
    return await db_session.get(
        models.Business, business_id, populate_existing=True
    )


async def add_service(
    db_session: sa_asyncio.AsyncSession,
    business_id: int,
    service_input: ServiceInput,
) -> models.Service:
    """Добавляет активную услугу бизнесу.

    Args:
        db_session: Сессия базы.
        business_id: ID бизнеса.
        service_input: Проверенные поля услуги.

    Returns:
        Новая услуга.
    """
    service = models.Service(
        business_id=business_id, **dataclasses.asdict(service_input)
    )
    db_session.add(service)
    await db_session.commit()
    return service


async def list_services(
    db_session: sa_asyncio.AsyncSession, business_id: int
) -> list[models.Service]:
    """Возвращает все услуги бизнеса, включая скрытые.

    Args:
        db_session: Сессия базы.
        business_id: ID бизнеса.

    Returns:
        Услуги в порядке добавления.
    """
    result = await db_session.scalars(
        sqlalchemy.select(models.Service)
        .where(models.Service.business_id == business_id)
        .order_by(models.Service.id)
    )
    return list(result)


async def set_service_active(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    service_id: int,
    is_active: bool,
) -> bool:
    """Скрывает или показывает услугу владельца.

    Услуги не удаляются, только скрываются, чтобы не терять слоты
    по ним (B4.2).

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца, который нажал кнопку.
        service_id: ID услуги.
        is_active: True — показать, False — скрыть.

    Returns:
        True, если услуга найдена у этого владельца.
    """
    result = await db_session.execute(
        sqlalchemy.update(models.Service)
        .where(
            models.Service.id == service_id,
            models.Service.business_id.in_(_owner_business_ids(owner_id)),
        )
        .values(is_active=is_active)
    )
    await db_session.commit()
    return result.rowcount > 0


async def find_owner_service(
    db_session: sa_asyncio.AsyncSession, owner_id: int, service_id: int
) -> models.Service | None:
    """Ищет услугу, если она принадлежит бизнесу этого владельца.

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца.
        service_id: ID услуги.

    Returns:
        Услуга или None, если её нет или она чужая.
    """
    return await db_session.scalar(
        sqlalchemy.select(models.Service).where(
            models.Service.id == service_id,
            models.Service.business_id.in_(_owner_business_ids(owner_id)),
        )
    )


async def has_active_slots(
    db_session: sa_asyncio.AsyncSession, service_id: int
) -> bool:
    """Проверяет, есть ли у услуги слоты в статусах open или booked.

    Пока такие слоты есть, длительность услуги менять нельзя: в слоте
    она не хранится (B4.2).

    Args:
        db_session: Сессия базы.
        service_id: ID услуги.

    Returns:
        True, если есть открытый или забронированный слот.
    """
    return bool(
        await db_session.scalar(
            sqlalchemy.select(_active_slots_exist(service_id))
        )
    )


async def update_service(
    db_session: sa_asyncio.AsyncSession,
    owner_id: int,
    service_id: int,
    field: ServiceField,
    raw_text: str,
) -> ServiceUpdateResult:
    """Меняет одно поле услуги владельца (B4.2).

    Значение проверяется теми же правилами, что при добавлении.
    Запрет на смену длительности при активных слотах стоит в самом
    UPDATE, поэтому слот, созданный в ту же секунду, его не обойдёт.

    Args:
        db_session: Сессия базы.
        owner_id: ID владельца, который меняет услугу.
        service_id: ID услуги.
        field: Какое поле меняется.
        raw_text: Новое значение в виде текста.

    Returns:
        UPDATED, NOT_FOUND (услуги нет или она чужая) или
        DURATION_LOCKED (есть слоты open или booked).

    Raises:
        ValueError: Значение не прошло проверку. Текст ошибки содержит
            пример.
    """
    value = _SERVICE_PARSERS[field](raw_text)
    conditions = [
        models.Service.id == service_id,
        models.Service.business_id.in_(_owner_business_ids(owner_id)),
    ]
    if field == ServiceField.DURATION_MIN:
        conditions.append(~_active_slots_exist(models.Service.id))
    result = await db_session.execute(
        sqlalchemy.update(models.Service)
        .where(*conditions)
        .values({field.value: value})
    )
    await db_session.commit()
    if result.rowcount > 0:
        return ServiceUpdateResult.UPDATED
    service = await find_owner_service(db_session, owner_id, service_id)
    if service is not None and field == ServiceField.DURATION_MIN:
        return ServiceUpdateResult.DURATION_LOCKED
    return ServiceUpdateResult.NOT_FOUND


def _owner_business_ids(owner_id: int) -> sqlalchemy.Select[tuple[int]]:
    return sqlalchemy.select(models.Business.id).where(
        models.Business.owner_id == owner_id
    )


def _active_slots_exist(
    service_id: int | sqlalchemy.ColumnElement[int],
) -> sqlalchemy.Exists:
    return sqlalchemy.exists().where(
        models.Slot.service_id == service_id,
        models.Slot.status.in_(_ACTIVE_SLOT_STATUSES),
    )


def _posted_open_slots(business_id: int) -> sqlalchemy.ColumnElement[bool]:
    return sqlalchemy.and_(
        models.Slot.service_id.in_(
            sqlalchemy.select(models.Service.id)
            .where(models.Service.business_id == business_id)
        ),
        models.Slot.status == models.SlotStatus.OPEN,
        models.Slot.channel_msg_id.is_not(None),
    )


def _add_business_event(
    db_session: sa_asyncio.AsyncSession,
    event_type: models.EventType,
    business_id: int,
) -> None:
    # Админ может не быть пользователем бота, поэтому user_id пустой.
    db_session.add(
        models.Event(type=event_type, payload={"business_id": business_id})
    )


async def _decide(
    db_session: sa_asyncio.AsyncSession,
    business_id: int,
    new_status: models.BusinessStatus,
) -> BusinessDecision | None:
    # Условие на pending в самом UPDATE: если два админа нажали
    # одновременно, строку изменит только первый.
    result = await db_session.execute(
        sqlalchemy.update(models.Business)
        .where(
            models.Business.id == business_id,
            models.Business.status == models.BusinessStatus.PENDING,
        )
        .values(status=new_status)
    )
    await db_session.commit()
    if result.rowcount == 0:
        return None
    row = (
        await db_session.execute(
            sqlalchemy.select(models.Business, models.User.tg_id)
            .join(models.User, models.User.id == models.Business.owner_id)
            .where(models.Business.id == business_id)
            .execution_options(populate_existing=True)
        )
    ).one()
    return BusinessDecision(business=row[0], owner_tg_id=row[1])


def _parse_int_in_range(
    raw_text: str, min_value: int, max_value: int, error_text: str
) -> int:
    try:
        value = int(raw_text.strip())
    except ValueError as error:
        raise ValueError(error_text) from error
    if not min_value <= value <= max_value:
        raise ValueError(error_text)
    return value

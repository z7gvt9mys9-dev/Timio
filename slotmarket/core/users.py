"""Пользователи Telegram (B5, B7).

Запись в users создаётся после согласия на обработку данных. После
/delete_me запись остаётся без имени, телефона и consent_at: на неё
ссылаются брони, а Telegram ID нужен для учёта блокировок. Поэтому
согласие означает consent_at, а не наличие записи.
"""

import datetime
import enum

import sqlalchemy
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext import asyncio as sa_asyncio

from core import bookings
from core import businesses
from db import models

# Колонка name обязательная, поэтому после удаления в ней эта заглушка.
DELETED_NAME = "Удалён"


class DeleteOutcome(enum.StrEnum):
    """Чем закончилась попытка удалить данные."""

    DELETED = "deleted"
    HAS_UPCOMING_BOOKINGS = "has_upcoming_bookings"


async def find_user(
    db_session: sa_asyncio.AsyncSession, tg_id: int
) -> models.User | None:
    """Ищет пользователя с согласием по Telegram ID.

    Args:
        db_session: Сессия базы.
        tg_id: Telegram ID.

    Returns:
        Пользователь или None, если согласия нет: он его не давал или
        удалил свои данные.
    """
    return await db_session.scalar(
        sqlalchemy.select(models.User).where(
            models.User.tg_id == tg_id,
            models.User.consent_at.is_not(None),
        )
    )


async def register_user(
    db_session: sa_asyncio.AsyncSession, tg_id: int, name: str
) -> models.User:
    """Создаёт пользователя после согласия или возвращает существующего.

    Вставка идёт одним запросом с ON CONFLICT, поэтому двойное нажатие
    «Принимаю» не создаст дубль и не упадёт на уникальном tg_id.
    После /delete_me запрос заново записывает имя и consent_at, но не
    трогает неявки и блокировку.

    Args:
        db_session: Сессия базы.
        tg_id: Telegram ID.
        name: Имя из Telegram.

    Returns:
        Пользователь. У существующего consent_at не меняется.

    Raises:
        LookupError: Пользователь не найден сразу после вставки.
    """
    insert = sqlite.insert(models.User).values(
        tg_id=tg_id, name=name, consent_at=models.utc_now()
    )
    statement = insert.on_conflict_do_update(
        index_elements=[models.User.tg_id],
        set_={
            "name": insert.excluded.name,
            "consent_at": insert.excluded.consent_at,
        },
        where=models.User.consent_at.is_(None),
    )
    await db_session.execute(statement)
    await db_session.commit()
    user = await find_user(db_session, tg_id)
    if user is None:
        raise LookupError(f"Пользователь не создан, tg_id={tg_id}")
    return user


async def mark_owner(db_session: sa_asyncio.AsyncSession, user_id: int) -> None:
    """Отмечает пользователя как владельца бизнеса.

    Публиковать слоты он всё равно не сможет, пока бизнес в статусе
    pending (B4.2).

    Args:
        db_session: Сессия базы.
        user_id: ID пользователя.
    """
    await db_session.execute(
        sqlalchemy.update(models.User)
        .where(models.User.id == user_id)
        .values(is_owner=True)
    )
    await db_session.commit()


async def set_phone(
    db_session: sa_asyncio.AsyncSession, user_id: int, raw_phone: str
) -> str:
    """Сохраняет телефон покупателя в том же виде, что у бизнеса (B3.2).

    Args:
        db_session: Сессия базы.
        user_id: ID пользователя.
        raw_phone: Телефон из контакта Telegram, например 79001234567.

    Returns:
        Российский телефон в виде +7(XXX)XXX-XX-XX, остальные — +<цифры>.

    Raises:
        ValueError: Это не номер телефона. Текст ошибки содержит пример.
    """
    phone = businesses.parse_phone(raw_phone)
    await db_session.execute(
        sqlalchemy.update(models.User)
        .where(models.User.id == user_id)
        .values(phone=phone)
    )
    await db_session.commit()
    return phone


async def delete_personal_data(
    db_session: sa_asyncio.AsyncSession,
    user_id: int,
    now: datetime.datetime,
) -> DeleteOutcome:
    """Стирает имя, телефон и согласие пользователя по /delete_me (B5).

    Строка в users остаётся: брони и их цены не меняются, а Telegram ID,
    неявки и блокировка нужны, чтобы блокировку нельзя было обойти
    удалением данных.

    Args:
        db_session: Сессия базы.
        user_id: ID пользователя.
        now: Текущее время.

    Returns:
        DELETED или HAS_UPCOMING_BOOKINGS, если есть активная бронь на
        слот, который не начался: бизнесу нужен телефон до визита.
    """
    upcoming = await bookings.count_upcoming_bookings(
        db_session, user_id, now
    )
    if upcoming:
        return DeleteOutcome.HAS_UPCOMING_BOOKINGS
    await db_session.execute(
        sqlalchemy.update(models.User)
        .where(models.User.id == user_id)
        .values(name=DELETED_NAME, phone=None, consent_at=None)
    )
    db_session.add(
        models.Event(user_id=user_id, type=models.EventType.DATA_DELETED)
    )
    await db_session.commit()
    return DeleteOutcome.DELETED


async def block_user(db_session: sa_asyncio.AsyncSession, tg_id: int) -> bool:
    """Блокирует бронирование покупателю бессрочно по команде админа.

    Ищет по Telegram ID без учёта согласия: после /delete_me запись
    остаётся именно для учёта блокировок (B5, B4.9).

    Args:
        db_session: Сессия базы.
        tg_id: Telegram ID покупателя.

    Returns:
        False, если такого пользователя нет.
    """
    return await _set_blocked_until(
        db_session, tg_id, bookings.PERMANENT_BLOCK_UNTIL,
        models.EventType.USER_BLOCKED,
    )


async def unblock_user(
    db_session: sa_asyncio.AsyncSession, tg_id: int
) -> bool:
    """Снимает любую блокировку покупателя: и админа, и за неявки.

    Args:
        db_session: Сессия базы.
        tg_id: Telegram ID покупателя.

    Returns:
        False, если такого пользователя нет.
    """
    return await _set_blocked_until(
        db_session, tg_id, None, models.EventType.USER_UNBLOCKED
    )


async def _set_blocked_until(
    db_session: sa_asyncio.AsyncSession,
    tg_id: int,
    blocked_until: datetime.datetime | None,
    event_type: models.EventType,
) -> bool:
    user_id = await db_session.scalar(
        sqlalchemy.update(models.User)
        .where(models.User.tg_id == tg_id)
        .values(blocked_until=blocked_until)
        .returning(models.User.id)
    )
    if user_id is None:
        await db_session.rollback()
        return False
    db_session.add(models.Event(user_id=user_id, type=event_type))
    await db_session.commit()
    return True

"""Пользователи Telegram (B5, B7).

Запись в users создаётся только после согласия на обработку данных,
поэтому само наличие записи означает, что согласие дано.
"""

import sqlalchemy
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext import asyncio as sa_asyncio

from db import models


async def find_user(
    db_session: sa_asyncio.AsyncSession, tg_id: int
) -> models.User | None:
    """Ищет пользователя по Telegram ID.

    Args:
        db_session: Сессия базы.
        tg_id: Telegram ID.

    Returns:
        Пользователь или None, если он ещё не давал согласия.
    """
    return await db_session.scalar(
        sqlalchemy.select(models.User).where(models.User.tg_id == tg_id)
    )


async def register_user(
    db_session: sa_asyncio.AsyncSession, tg_id: int, name: str
) -> models.User:
    """Создаёт пользователя после согласия или возвращает существующего.

    Вставка идёт одним запросом с ON CONFLICT, поэтому двойное нажатие
    «Принимаю» не создаст дубль и не упадёт на уникальном tg_id.

    Args:
        db_session: Сессия базы.
        tg_id: Telegram ID.
        name: Имя из Telegram.

    Returns:
        Пользователь. У существующего consent_at не меняется.

    Raises:
        LookupError: Пользователь не найден сразу после вставки.
    """
    statement = (
        sqlite.insert(models.User)
        .values(tg_id=tg_id, name=name, consent_at=models.utc_now())
        .on_conflict_do_nothing(index_elements=[models.User.tg_id])
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

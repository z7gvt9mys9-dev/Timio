"""Подключение к SQLite и фабрика сессий."""

import pathlib
import sqlite3

import sqlalchemy
from sqlalchemy.ext import asyncio as sa_asyncio

from db import models

# Сколько ждать снятия блокировки записи, прежде чем отдать ошибку.
_BUSY_TIMEOUT_MS = 5000


def create_engine(db_path: pathlib.Path) -> sa_asyncio.AsyncEngine:
    """Создаёт движок SQLite с WAL и проверкой внешних ключей.

    Args:
        db_path: Путь к файлу базы.

    Returns:
        Асинхронный движок SQLAlchemy.
    """
    engine = sa_asyncio.create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    sqlalchemy.event.listen(engine.sync_engine, "connect", _set_pragmas)
    return engine


def _set_pragmas(
    dbapi_connection: sqlite3.Connection, connection_record: object
) -> None:
    # PRAGMA действуют на одно подключение, поэтому ставим их на каждом.
    # WAL нужен, чтобы бот и сайт работали с базой одновременно (B6).
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    cursor.close()


def create_session_factory(
    engine: sa_asyncio.AsyncEngine,
) -> sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]:
    """Создаёт фабрику асинхронных сессий.

    Args:
        engine: Движок базы.

    Returns:
        Фабрика сессий. Объекты не сбрасываются после commit.
    """
    return sa_asyncio.async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: sa_asyncio.AsyncEngine) -> None:
    """Создаёт недостающие таблицы и индексы.

    Args:
        engine: Движок базы.
    """
    async with engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)

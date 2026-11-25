"""Точка входа: запуск Telegram-бота."""

import asyncio
import logging

import aiogram

import config
from bot.handlers import common
from bot.handlers import owner
from db import session

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


async def run_bot() -> None:
    """Загружает настройки, готовит базу и запускает опрос Telegram."""
    settings = config.load_config()
    engine = session.create_engine(settings.db_path)
    await session.create_tables(engine)

    bot = aiogram.Bot(token=settings.bot_token)
    # Фабрика сессий попадает в обработчики аргументом session_factory.
    dispatcher = aiogram.Dispatcher(
        session_factory=session.create_session_factory(engine)
    )
    dispatcher.include_router(common.router)
    dispatcher.include_router(owner.router)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    """Настраивает логирование и запускает бота."""
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()

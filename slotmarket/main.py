"""Точка входа: запуск Telegram-бота."""

import asyncio
import logging

import aiogram

import config
import jobs
from bot import channel
from bot.handlers import admin
from bot.handlers import buyer
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
    session_factory = session.create_session_factory(engine)
    channel_config = channel.ChannelConfig(
        channel_id=settings.channel_id, city_tz=settings.city_tz
    )
    # Эти значения попадают в обработчики аргументами с теми же именами.
    dispatcher = aiogram.Dispatcher(
        session_factory=session_factory,
        admin_ids=settings.admin_ids,
        city_tz=settings.city_tz,
        channel_config=channel_config,
    )
    dispatcher.include_router(common.router)
    dispatcher.include_router(owner.router)
    dispatcher.include_router(admin.router)
    dispatcher.include_router(buyer.router)
    dispatcher.include_router(common.fallback_router)
    scheduler = jobs.create_scheduler(bot, session_factory, channel_config)
    scheduler.start()
    try:
        await dispatcher.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    """Настраивает логирование и запускает бота."""
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()

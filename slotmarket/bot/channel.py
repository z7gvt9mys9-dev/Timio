"""Посты слотов в канале: отправка и правка (B4.6, B9).

Ошибки канала только пишутся в лог: слот остаётся доступным через
/slots, а бот и планировщик продолжают работу.
"""

import collections.abc
import dataclasses
import datetime
import logging

import aiogram
from aiogram import exceptions
from aiogram import types
from aiogram.enums import ParseMode
from sqlalchemy import exc as sa_exc
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import keyboards
from core import feed

_LOG = logging.getLogger(__name__)

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
_DEEP_LINK_URL = "https://t.me/{username}?start={payload}"
# Telegram отвечает так, если текст и кнопки поста не изменились.
_NOT_MODIFIED_ERROR = "message is not modified"


@dataclasses.dataclass(frozen=True)
class ChannelConfig:
    """Куда публиковать и в каком часовом поясе показывать время."""

    channel_id: int
    city_tz: datetime.tzinfo


async def publish_slots(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    channel: ChannelConfig,
    slot_ids: collections.abc.Sequence[int],
) -> None:
    """Публикует посты новых открытых слотов, у которых поста ещё нет.

    Args:
        bot: Бот, который пишет в канал.
        session_factory: Фабрика сессий базы.
        channel: Канал и часовой пояс.
        slot_ids: ID слотов.
    """
    async with session_factory() as db_session:
        cards = await feed.load_cards(db_session, list(slot_ids))
    for card in cards:
        if feed.is_bookable(card) and card.channel_msg_id is None:
            await _publish(bot, session_factory, channel, card)


async def publish_unposted(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    channel: ChannelConfig,
) -> None:
    """Повторяет публикацию открытых слотов, пост которых не ушёл (B9).

    Args:
        bot: Бот, который пишет в канал.
        session_factory: Фабрика сессий базы.
        channel: Канал и часовой пояс.
    """
    async with session_factory() as db_session:
        cards = await feed.list_unposted_cards(db_session)
    for card in cards:
        await _publish(bot, session_factory, channel, card)


async def refresh_posts(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    channel: ChannelConfig,
    slot_ids: collections.abc.Sequence[int],
) -> None:
    """Обновляет посты: новая цена или пометка статуса (B4.6).

    У закрытых слотов кнопка «Забронировать» убирается.

    Args:
        bot: Бот, который пишет в канал.
        session_factory: Фабрика сессий базы.
        channel: Канал и часовой пояс.
        slot_ids: ID слотов, посты которых нужно обновить.
    """
    async with session_factory() as db_session:
        cards = await feed.load_cards(db_session, list(slot_ids))
    for card in cards:
        if card.channel_msg_id is not None:
            await _edit(bot, channel, card)


async def build_booking_url(bot: aiogram.Bot, slot_id: int) -> str:
    """Собирает deep link на бронь слота: t.me/<бот>?start=slot_<id> (B8).

    Args:
        bot: Бот: его username берётся из кэша aiogram.
        slot_id: ID слота.

    Returns:
        Ссылка для кнопки «Забронировать».

    Raises:
        TelegramAPIError: Не удалось узнать username бота.
    """
    bot_user = await bot.me()
    return _DEEP_LINK_URL.format(
        username=bot_user.username,
        payload=feed.build_deep_link_payload(slot_id),
    )


async def _publish(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    channel: ChannelConfig,
    card: feed.SlotCard,
) -> None:
    try:
        message = await bot.send_message(
            channel.channel_id,
            feed.build_post_text(card, channel.city_tz),
            parse_mode=ParseMode.HTML,
            reply_markup=await _build_markup(bot, card),
        )
    except exceptions.TelegramAPIError as error:
        _LOG.warning("Пост слота %s не отправлен: %s", card.slot_id, error)
        return
    # Возможен дубль: если запись ниже упадёт, слот останется без
    # channel_msg_id и следующий прогон планировщика отправит пост снова.
    try:
        async with session_factory() as db_session:
            await feed.set_channel_msg_id(
                db_session, card.slot_id, message.message_id
            )
    except sa_exc.SQLAlchemyError:
        _LOG.exception(
            "Пост слота %s отправлен, но ID не сохранён", card.slot_id
        )


async def _edit(
    bot: aiogram.Bot, channel: ChannelConfig, card: feed.SlotCard
) -> None:
    try:
        await bot.edit_message_text(
            feed.build_post_text(card, channel.city_tz),
            chat_id=channel.channel_id,
            message_id=card.channel_msg_id,
            parse_mode=ParseMode.HTML,
            reply_markup=await _build_markup(bot, card),
        )
    except exceptions.TelegramBadRequest as error:
        if _NOT_MODIFIED_ERROR in str(error):
            return
        _LOG.warning("Пост слота %s не обновлён: %s", card.slot_id, error)
    except exceptions.TelegramAPIError as error:
        _LOG.warning("Пост слота %s не обновлён: %s", card.slot_id, error)


async def _build_markup(
    bot: aiogram.Bot, card: feed.SlotCard
) -> types.InlineKeyboardMarkup | None:
    # Без клавиатуры Telegram убирает кнопку из поста.
    if not feed.is_bookable(card):
        return None
    return keyboards.build_slot_post_keyboard(
        await build_booking_url(bot, card.slot_id)
    )

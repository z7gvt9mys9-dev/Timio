"""Планировщик раз в 5 минут: слоты, цены, посты, напоминания (B6, B9)."""

import dataclasses
import datetime
import logging

import aiogram
from aiogram import exceptions
from aiogram import types
from apscheduler.schedulers import asyncio as aps_asyncio
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import channel
from bot import keyboards
from core import bookings
from core import feed
from core import slots
from db import models

JOB_INTERVAL_MINUTES = 5
SLOT_JOBS_ID = "slot_jobs"
_SLOT_TIME_FORMAT = "%d.%m %H:%M"
_TIME_FORMAT = "%H:%M"
_DAY_LABELS = {0: "сегодня", 1: "завтра"}
_DATE_FORMAT = "%d.%m"
# Ошибки, которые не пройдут при повторе: бот заблокирован, чата нет.
# Такое уведомление считаем отправленным, чтобы не слать его снова (B9).
_PERMANENT_ERRORS = (
    exceptions.TelegramForbiddenError,
    exceptions.TelegramBadRequest,
)

_LOG = logging.getLogger(__name__)

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]


async def run_slot_jobs(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    channel_config: channel.ChannelConfig,
) -> None:
    """Закрывает слоты, пересчитывает цены, обновляет посты, уведомляет.

    Всё считается по данным из базы, поэтому после перезапуска бота
    прогон даёт тот же результат (B9). Ошибки канала только в лог.

    Args:
        bot: Бот для канала и уведомлений владельцам.
        session_factory: Фабрика сессий базы.
        channel_config: Канал и часовой пояс города.
    """
    now = models.utc_now()
    async with session_factory() as db_session:
        # Сначала закрываем: у закрытых слотов цену считать незачем.
        expired = await slots.expire_due_slots(db_session, now)
        changed_ids = await slots.recalc_prices(db_session, now)
    _LOG.info(
        "Закрыто слотов: %s, изменилась цена: %s",
        len(expired), len(changed_ids),
    )
    expired_ids = [expired_slot.slot_id for expired_slot in expired]
    await channel.refresh_posts(
        bot, session_factory, channel_config, expired_ids + changed_ids
    )
    await channel.publish_unposted(bot, session_factory, channel_config)
    for expired_slot in expired:
        await _notify_owner(bot, expired_slot, channel_config.city_tz)
    await run_booking_jobs(bot, session_factory, channel_config.city_tz)


async def run_booking_jobs(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Подтверждает молчаливые визиты, шлёт напоминания и вопросы (B4.5).

    Ошибка отправки одному пользователю не останавливает остальных.

    Args:
        bot: Бот для сообщений покупателям и владельцам.
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    now = models.utc_now()
    async with session_factory() as db_session:
        # Сначала подтверждаем: по подтверждённой брони вопрос не нужен.
        confirmed = await bookings.auto_confirm_due(db_session, now)
        due = await bookings.due_notices(db_session, now)
    _LOG.info(
        "Автоподтверждено броней: %s, к отправке уведомлений: %s",
        len(confirmed), len(due),
    )
    for notice in due:
        await _send_notice(bot, session_factory, notice, city_tz, now)


def create_scheduler(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    channel_config: channel.ChannelConfig,
) -> aps_asyncio.AsyncIOScheduler:
    """Создаёт планировщик с задачей по слотам.

    Первый прогон сразу при старте (B9). Если прогон затянулся,
    следующий не запускается параллельно, а пропущенные не копятся.

    Args:
        bot: Бот для канала и уведомлений владельцам.
        session_factory: Фабрика сессий базы.
        channel_config: Канал и часовой пояс города.

    Returns:
        Планировщик, который нужно запустить методом start().
    """
    scheduler = aps_asyncio.AsyncIOScheduler(timezone=datetime.UTC)
    scheduler.add_job(
        run_slot_jobs,
        "interval",
        minutes=JOB_INTERVAL_MINUTES,
        kwargs={
            "bot": bot,
            "session_factory": session_factory,
            "channel_config": channel_config,
        },
        id=SLOT_JOBS_ID,
        next_run_time=datetime.datetime.now(datetime.UTC),
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def _notify_owner(
    bot: aiogram.Bot,
    expired_slot: slots.ExpiredSlot,
    city_tz: datetime.tzinfo,
) -> None:
    local_time = expired_slot.starts_at.astimezone(city_tz).strftime(
        _SLOT_TIME_FORMAT
    )
    text = (
        f"Слот «{expired_slot.service_title}» на {local_time} не продан "
        "и закрыт."
    )
    try:
        await bot.send_message(expired_slot.owner_tg_id, text)
    except exceptions.TelegramAPIError as error:
        # Владелец мог заблокировать бота: слот уже закрыт в базе,
        # остальные уведомления должны уйти.
        _LOG.warning(
            "Уведомление о слоте %s не доставлено: %s",
            expired_slot.slot_id, error,
        )


@dataclasses.dataclass(frozen=True)
class _Outgoing:
    chat_id: int
    text: str
    markup: types.InlineKeyboardMarkup | None = None


async def _send_notice(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    notice: bookings.DueNotice,
    city_tz: datetime.tzinfo,
    now: datetime.datetime,
) -> None:
    outgoing = _build_notice(notice, city_tz, now)
    booking_id = notice.details.booking_id
    try:
        await bot.send_message(
            outgoing.chat_id, outgoing.text, reply_markup=outgoing.markup
        )
    except _PERMANENT_ERRORS as error:
        _LOG.warning(
            "%s по брони %s не доставлено, больше не шлём: %s",
            notice.kind, booking_id, error,
        )
    except exceptions.TelegramAPIError as error:
        # Сеть или лимит Telegram: не отмечаем, повторим в следующий прогон.
        _LOG.warning(
            "%s по брони %s не отправлено, повторим: %s",
            notice.kind, booking_id, error,
        )
        return
    async with session_factory() as db_session:
        await bookings.mark_notified(db_session, notice.kind, booking_id)


def _build_notice(
    notice: bookings.DueNotice,
    city_tz: datetime.tzinfo,
    now: datetime.datetime,
) -> _Outgoing:
    details = notice.details
    if notice.kind == bookings.NoticeKind.VISIT_ASKED:
        return _Outgoing(
            chat_id=details.owner_tg_id,
            text="\n".join([
                "Клиент пришёл?",
                "",
                f"{details.service_title}, "
                f"{feed.format_slot_time(details.starts_at, city_tz)}",
                f"Клиент: {details.buyer_name}",
            ]),
            markup=keyboards.build_visit_keyboard(details.booking_id),
        )
    lines = [
        "Напоминание о брони",
        "",
        f"{details.service_title}, "
        f"{_format_day_time(details.starts_at, city_tz, now)}",
        details.business_name,
        f"Адрес: {details.business_address}",
        f"Телефон: {details.business_phone}",
        f"Цена: {feed.format_rubles(details.price)}, оплата на месте.",
    ]
    if notice.kind == bookings.NoticeKind.REMINDER_2H:
        deadline = details.starts_at - bookings.CANCEL_DEADLINE
        lines.extend([
            "",
            "Отменить можно до "
            f"{deadline.astimezone(city_tz).strftime(_TIME_FORMAT)}: /my",
        ])
    return _Outgoing(chat_id=details.buyer_tg_id, text="\n".join(lines))


def _format_day_time(
    moment: datetime.datetime,
    city_tz: datetime.tzinfo,
    now: datetime.datetime,
) -> str:
    # Точный час, а не «через 2 часа»: планировщик может опоздать
    # до 5 минут (B3.2, пункт 5).
    local = moment.astimezone(city_tz)
    day_offset = (local.date() - now.astimezone(city_tz).date()).days
    day_label = _DAY_LABELS.get(day_offset, local.strftime(_DATE_FORMAT))
    return f"{day_label} в {local.strftime(_TIME_FORMAT)}"

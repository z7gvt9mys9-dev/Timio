"""Ответ владельца на «Клиент пришёл?»: визит или неявка (B3.1, B4.5)."""

import datetime

import aiogram
from aiogram import types

from bot import keyboards
from bot.handlers.owner import shared
from core import bookings
from core import feed
from db import models

router = aiogram.Router()

_NOT_FOUND_TEXT = "Отметка уже стоит или бронь не найдена."
_CAME_TEXT = "Отмечено: пришёл."
_NO_SHOW_TEXT = "Отмечено: не пришёл."
_BLOCKED_TEXT = (
    "Это вторая неявка клиента: бронирование для него закрыто "
    "на 30 дней, до {date}."
)
_BLOCKED_DATE_FORMAT = "%d.%m.%Y"


@router.callback_query(aiogram.F.data.startswith(keyboards.VISIT_CAME_PREFIX))
async def handle_came(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Отмечает, что клиент пришёл: бронь и слот становятся done.

    Args:
        callback: Нажатие «Пришёл».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    await _record(
        callback, session_factory, city_tz, keyboards.VISIT_CAME_PREFIX,
        has_come=True,
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.VISIT_NO_SHOW_PREFIX)
)
async def handle_no_show(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Отмечает неявку: бронь и слот становятся no_show (B4.5).

    Args:
        callback: Нажатие «Не пришёл».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    await _record(
        callback, session_factory, city_tz, keyboards.VISIT_NO_SHOW_PREFIX,
        has_come=False,
    )


async def _record(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
    prefix: str,
    has_come: bool,
) -> None:
    # Чужое и повторное нажатие отсекает record_visit: условие на
    # владельца и на status='active' стоит в самом UPDATE.
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    booking_id = callback.data.removeprefix(prefix)
    result = bookings.VisitResult(bookings.VisitOutcome.NOT_FOUND)
    if business is not None and booking_id.isdigit():
        async with session_factory() as db_session:
            result = await bookings.record_visit(
                db_session, business.owner_id, int(booking_id), has_come,
                models.utc_now(),
            )
    if result.outcome == bookings.VisitOutcome.NOT_FOUND:
        await callback.answer(_NOT_FOUND_TEXT, show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(_format_marked(result, has_come, city_tz))


def _format_marked(
    result: bookings.VisitResult, has_come: bool, city_tz: datetime.tzinfo
) -> str:
    details = result.details
    lines = [
        _CAME_TEXT if has_come else _NO_SHOW_TEXT,
        "",
        f"{details.service_title}, "
        f"{feed.format_slot_time(details.starts_at, city_tz)}",
        f"Клиент: {details.buyer_name}",
    ]
    if result.buyer_blocked_until is not None:
        blocked_date = result.buyer_blocked_until.astimezone(city_tz)
        lines.extend([
            "",
            _BLOCKED_TEXT.format(
                date=blocked_date.strftime(_BLOCKED_DATE_FORMAT)
            ),
        ])
    return "\n".join(lines)

"""Администрирование: заявки бизнесов (B3.3, B4.7, B4.9)."""

import collections.abc
import logging

import aiogram
from aiogram import exceptions
from aiogram import filters
from aiogram import types
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import keyboards
from core import businesses
from db import models

_LOG = logging.getLogger(__name__)

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
_DecideFunction = collections.abc.Callable[
    [sa_asyncio.AsyncSession, int],
    collections.abc.Awaitable[businesses.BusinessDecision | None],
]

_NO_PENDING_TEXT = "Заявок нет."
_ALREADY_DECIDED_TEXT = "Заявка уже рассмотрена."
_APPROVED_OWNER_TEXT = (
    "Заявка одобрена: бизнес «{name}» прошёл проверку. "
    "Скоро вы сможете выставлять пустые слоты на продажу."
)
_REJECTED_OWNER_TEXT = (
    "Заявка бизнеса «{name}» отклонена. Если это ошибка, напишите "
    "команде SlotMarket."
)


def _is_admin(
    event: types.Message | types.CallbackQuery, admin_ids: frozenset[int]
) -> bool:
    return event.from_user is not None and event.from_user.id in admin_ids


# Все команды и кнопки этого роутера только для ID из ADMIN_IDS (B4.9).
# Остальным бот молча не отвечает.
router = aiogram.Router()
router.message.filter(_is_admin)
router.callback_query.filter(_is_admin)


@router.message(filters.Command("pending"))
async def handle_pending(
    message: types.Message, session_factory: _SessionFactory
) -> None:
    """Показывает заявки на проверке с кнопками решения (B4.9).

    Args:
        message: Входящее сообщение.
        session_factory: Фабрика сессий базы.
    """
    async with session_factory() as db_session:
        pending = await businesses.list_pending(db_session)
    if not pending:
        await message.answer(_NO_PENDING_TEXT)
        return
    for business in pending:
        await message.answer(
            format_business_details(business),
            reply_markup=keyboards.build_application_keyboard(business.id),
        )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.BUSINESS_APPROVE_PREFIX)
)
async def handle_approve(
    callback: types.CallbackQuery,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
) -> None:
    """Одобряет заявку и уведомляет владельца (B4.7).

    Args:
        callback: Нажатие «Одобрить».
        session_factory: Фабрика сессий базы.
        bot: Бот, чтобы уведомить владельца.
    """
    await _decide(
        callback, session_factory, bot,
        keyboards.BUSINESS_APPROVE_PREFIX, businesses.approve_business,
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.BUSINESS_REJECT_PREFIX)
)
async def handle_reject(
    callback: types.CallbackQuery,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
) -> None:
    """Отклоняет заявку и уведомляет владельца (B4.7).

    Args:
        callback: Нажатие «Отклонить».
        session_factory: Фабрика сессий базы.
        bot: Бот, чтобы уведомить владельца.
    """
    await _decide(
        callback, session_factory, bot,
        keyboards.BUSINESS_REJECT_PREFIX, businesses.reject_business,
    )


async def _decide(
    callback: types.CallbackQuery,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    prefix: str,
    decide: _DecideFunction,
) -> None:
    business_id = callback.data.removeprefix(prefix)
    decision = None
    if business_id.isdigit():
        async with session_factory() as db_session:
            decision = await decide(db_session, int(business_id))
    if decision is None:
        # Другой админ успел раньше: строку изменил только его UPDATE.
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer(_ALREADY_DECIDED_TEXT, show_alert=True)
        return
    await callback.answer()
    business = decision.business
    is_approved = business.status == models.BusinessStatus.APPROVED
    verdict = "Одобрено" if is_approved else "Отклонено"
    admin_name = callback.from_user.full_name
    await callback.message.edit_text(
        f"{callback.message.text}\n\n{verdict}: {admin_name}"
    )
    owner_text = _APPROVED_OWNER_TEXT if is_approved else _REJECTED_OWNER_TEXT
    await _notify_owner(
        bot, decision.owner_tg_id, owner_text.format(name=business.name)
    )


async def _notify_owner(
    bot: aiogram.Bot, owner_tg_id: int, text: str
) -> None:
    try:
        await bot.send_message(owner_tg_id, text)
    except exceptions.TelegramAPIError as error:
        # Владелец мог заблокировать бота: решение уже сохранено,
        # статус он увидит в /business.
        _LOG.warning("Решение не доставлено владельцу: %s", error)


async def notify_owner_withdrawals(
    bot: aiogram.Bot,
    admin_ids: collections.abc.Iterable[int],
    business: models.Business,
    withdrawals: int,
) -> None:
    """Сообщает админам, что бизнес снова снял слот после брони (B9).

    Args:
        bot: Бот.
        admin_ids: Telegram ID администраторов из .env.
        business: Бизнес, который снял слот.
        withdrawals: Сколько раз он снял слоты после брони за всё время.
    """
    text = "\n".join([
        "Бизнес снял слот после брони.",
        f"Всего таких снятий: {withdrawals}.",
        "",
        format_business_details(business),
        "",
        "Решите, нужно ли блокировать бизнес.",
    ])
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text)
        except exceptions.TelegramAPIError as error:
            # Один недоступный админ не мешает остальным.
            _LOG.warning(
                "Уведомление о снятиях бизнеса %s не доставлено админу "
                "%s: %s", business.id, admin_id, error,
            )


def format_business_details(business: models.Business) -> str:
    """Собирает описание бизнеса для карточки владельца и заявки.

    Args:
        business: Бизнес.

    Returns:
        Название, категория, район, адрес и телефон по строкам.
    """
    return (
        f"{business.name}\n"
        f"Категория: {business.category}\n"
        f"Район: {business.district}\n"
        f"Адрес: {business.address}\n"
        f"Телефон: {business.phone}"
    )


async def notify_new_application(
    bot: aiogram.Bot,
    admin_ids: collections.abc.Iterable[int],
    business: models.Business,
    owner_name: str,
) -> None:
    """Отправляет новую заявку бизнеса каждому администратору (B4.7).

    Args:
        bot: Бот.
        admin_ids: Telegram ID администраторов из .env.
        business: Новый бизнес.
        owner_name: Имя владельца из Telegram.
    """
    text = (
        "Новая заявка бизнеса\n\n"
        f"{format_business_details(business)}\n"
        f"Владелец: {owner_name}"
    )
    markup = keyboards.build_application_keyboard(business.id)
    for admin_id in admin_ids:
        try:
            await bot.send_message(admin_id, text, reply_markup=markup)
        except exceptions.TelegramAPIError as error:
            # Админ мог не запускать бота: остальные всё равно получат
            # заявку, а /pending покажет её позже.
            _LOG.warning(
                "Заявка %s не доставлена админу %s: %s",
                business.id, admin_id, error,
            )

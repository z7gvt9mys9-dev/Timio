"""Администрирование: заявки, блокировки, статистика (B3.3, B4.7, B4.9)."""

import collections.abc
import dataclasses
import enum
import logging

import aiogram
from aiogram import exceptions
from aiogram import filters
from aiogram import types
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import channel
from bot import keyboards
from core import businesses
from core import reports
from core import users
from db import models

_LOG = logging.getLogger(__name__)

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
_DecideFunction = collections.abc.Callable[
    [sa_asyncio.AsyncSession, int],
    collections.abc.Awaitable[businesses.BusinessDecision | None],
]

_NO_PENDING_TEXT = "Заявок нет."
_SLOTS_FILE_NAME = "slots.csv"
_BOOKINGS_FILE_NAME = "bookings.csv"
_ALREADY_DECIDED_TEXT = "Заявка уже рассмотрена."
_APPROVED_OWNER_TEXT = (
    "Заявка одобрена: бизнес «{name}» прошёл проверку. "
    "Скоро вы сможете выставлять пустые слоты на продажу."
)
_REJECTED_OWNER_TEXT = (
    "Заявка бизнеса «{name}» отклонена. Если это ошибка, напишите "
    "команде SlotMarket."
)
_TARGET_FORMAT_TEXT = (
    "Формат: /{command} user <Telegram ID> или "
    "/{command} business <ID бизнеса>.\n"
    "Пример: /{command} user 123456789 или /{command} business 5"
)
_USER_NOT_FOUND_TEXT = "Пользователь с Telegram ID {target_id} не найден."
_USER_BLOCKED_TEXT = (
    "Покупатель с Telegram ID {target_id} заблокирован бессрочно."
)
_USER_UNBLOCKED_TEXT = (
    "Блокировка покупателя с Telegram ID {target_id} снята."
)
_BUSINESS_BLOCKED_TEXT = (
    "Бизнес «{name}» (ID {target_id}) заблокирован. Его открытые слоты "
    "убраны из ленты, существующие брони сохраняются."
)
_BUSINESS_NOT_BLOCKABLE_TEXT = (
    "Бизнес с ID {target_id} не найден или уже заблокирован."
)
_BUSINESS_UNBLOCKED_TEXT = (
    "Бизнес «{name}» (ID {target_id}) разблокирован, его открытые слоты "
    "вернутся в ленту."
)
_BUSINESS_NOT_UNBLOCKABLE_TEXT = (
    "Бизнес с ID {target_id} не найден или не заблокирован."
)


class _TargetKind(enum.StrEnum):
    """Кого блокирует админ: первое слово после команды."""

    USER = "user"
    BUSINESS = "business"


@dataclasses.dataclass(frozen=True)
class _Target:
    kind: _TargetKind
    target_id: int


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
            _format_for_admin(business),
            reply_markup=keyboards.build_application_keyboard(business.id),
        )


@router.message(filters.Command("stats"))
async def handle_stats(
    message: types.Message, session_factory: _SessionFactory
) -> None:
    """Показывает статистику за всё время и за 7 дней (B4.9, B10).

    Args:
        message: Входящее сообщение.
        session_factory: Фабрика сессий базы.
    """
    async with session_factory() as db_session:
        stats = await reports.build_team_stats(db_session, models.utc_now())
    await message.answer(reports.format_team_stats(stats))


@router.message(filters.Command("export"))
async def handle_export(
    message: types.Message, session_factory: _SessionFactory
) -> None:
    """Присылает слоты и брони двумя файлами CSV (B4.9).

    Args:
        message: Входящее сообщение.
        session_factory: Фабрика сессий базы.
    """
    async with session_factory() as db_session:
        slots_csv = await reports.export_slots_csv(db_session)
        bookings_csv = await reports.export_bookings_csv(db_session)
    await message.answer_document(
        types.BufferedInputFile(slots_csv, filename=_SLOTS_FILE_NAME)
    )
    await message.answer_document(
        types.BufferedInputFile(bookings_csv, filename=_BOOKINGS_FILE_NAME)
    )


@router.message(filters.Command("block"))
async def handle_block(
    message: types.Message,
    command: filters.CommandObject,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Блокирует покупателя бессрочно или бизнес (B4.9).

    Args:
        message: Входящее сообщение.
        command: Команда с аргументами «user <tg_id>» или
            «business <id>».
        session_factory: Фабрика сессий базы.
        bot: Бот, чтобы убрать посты бизнеса из канала.
        channel_config: Канал с лентой.
    """
    target = _parse_target(command.args)
    if target is None:
        await message.answer(_TARGET_FORMAT_TEXT.format(command="block"))
        return
    if target.kind == _TargetKind.USER:
        async with session_factory() as db_session:
            is_found = await users.block_user(db_session, target.target_id)
        text = _USER_BLOCKED_TEXT if is_found else _USER_NOT_FOUND_TEXT
        await message.answer(text.format(target_id=target.target_id))
        return
    async with session_factory() as db_session:
        blocked = await businesses.block_business(
            db_session, target.target_id
        )
    if blocked is None:
        await message.answer(
            _BUSINESS_NOT_BLOCKABLE_TEXT.format(target_id=target.target_id)
        )
        return
    await channel.remove_posts(bot, channel_config, blocked.channel_msg_ids)
    await message.answer(
        _BUSINESS_BLOCKED_TEXT.format(
            name=blocked.business.name, target_id=target.target_id
        )
    )


@router.message(filters.Command("unblock"))
async def handle_unblock(
    message: types.Message,
    command: filters.CommandObject,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Снимает блокировку с покупателя или бизнеса (B4.9).

    Args:
        message: Входящее сообщение.
        command: Команда с аргументами «user <tg_id>» или
            «business <id>».
        session_factory: Фабрика сессий базы.
        bot: Бот, чтобы вернуть посты бизнеса в канал.
        channel_config: Канал с лентой.
    """
    target = _parse_target(command.args)
    if target is None:
        await message.answer(_TARGET_FORMAT_TEXT.format(command="unblock"))
        return
    if target.kind == _TargetKind.USER:
        async with session_factory() as db_session:
            is_found = await users.unblock_user(db_session, target.target_id)
        text = _USER_UNBLOCKED_TEXT if is_found else _USER_NOT_FOUND_TEXT
        await message.answer(text.format(target_id=target.target_id))
        return
    async with session_factory() as db_session:
        business = await businesses.unblock_business(
            db_session, target.target_id
        )
    if business is None:
        await message.answer(
            _BUSINESS_NOT_UNBLOCKABLE_TEXT.format(target_id=target.target_id)
        )
        return
    # Не ждём планировщика: открытые слоты сразу возвращаются в канал.
    await channel.publish_unposted(bot, session_factory, channel_config)
    await message.answer(
        _BUSINESS_UNBLOCKED_TEXT.format(
            name=business.name, target_id=target.target_id
        )
    )


def _parse_target(args: str | None) -> _Target | None:
    parts = (args or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    try:
        kind = _TargetKind(parts[0].lower())
    except ValueError:
        return None
    return _Target(kind=kind, target_id=int(parts[1]))


def _format_for_admin(business: models.Business) -> str:
    # ID нужен админу для /block business <id>.
    return f"ID бизнеса: {business.id}\n{format_business_details(business)}"


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
        _format_for_admin(business),
        "",
        "Решите, нужно ли блокировать бизнес: /block business "
        f"{business.id}",
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
        f"{_format_for_admin(business)}\n"
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

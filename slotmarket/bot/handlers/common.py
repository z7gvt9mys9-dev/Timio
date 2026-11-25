"""Общие команды: /start, /calc, согласие и главное меню (B2, B5, B8)."""

import datetime
import enum

import aiogram
from aiogram import filters
from aiogram import types
from aiogram.fsm import context
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import keyboards
from bot.handlers import buyer
from bot.handlers import owner
from core import feed
from core import users

router = aiogram.Router()
# Подключается последним: ловит нажатия, которые не обработал никто,
# например кнопки диалога, который уже закончился.
fallback_router = aiogram.Router()

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_OWNER_DEEP_LINK = "owner"
# Ключи в данных диалога: куда вести пользователя после согласия
# и какой слот открыть после него (deep link slot_<id>, B8).
_NEXT_STEP_KEY = "next_step"
_SLOT_ID_KEY = "slot_id"
_CONSENT_TEXT = (
    "Перед началом примите согласие на обработку персональных данных.\n\n"
    "Мы храним только ваше имя и Telegram ID, а при первой брони — "
    "телефон. Телефон увидит только бизнес, у которого вы забронировали "
    "слот."
)
_MENU_TEXT = "Выберите, кто вы:"
_BUYER_TEXT = (
    "Свободные слоты со скидкой публикуются в канале. Цена снижается "
    "по мере приближения времени.\n\n"
    "Нажмите «Забронировать» под постом, и бот откроет бронь."
)


class _NextStep(enum.StrEnum):
    """Куда вести пользователя после входа."""

    MENU = "menu"
    OWNER = "owner"
    CALC = "calc"
    BUSINESS = "business"
    NEW_SLOT = "new_slot"
    SLOT = "slot"


# Шаги, после которых пользователь считается владельцем (B3.1).
_OWNER_STEPS = frozenset(
    {_NextStep.OWNER, _NextStep.BUSINESS, _NextStep.NEW_SLOT}
)


@router.message(filters.CommandStart())
async def handle_start(
    message: types.Message,
    command: filters.CommandObject,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Сбрасывает диалог и открывает меню, калькулятор или слот (B8).

    Args:
        message: Входящее сообщение.
        command: Команда с аргументом deep link.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    await state.clear()
    slot_id = _parse_slot_link(command.args)
    if slot_id is not None:
        await _open_slot_link(
            message, message.from_user, state, session_factory, city_tz,
            slot_id,
        )
        return
    is_owner_link = command.args == _OWNER_DEEP_LINK
    next_step = _NextStep.OWNER if is_owner_link else _NextStep.MENU
    await _open_step(
        message, message.from_user, state, session_factory, next_step
    )


@router.message(filters.Command("calc"))
async def handle_calc(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
) -> None:
    """Сбрасывает диалог и запускает калькулятор потерь.

    Args:
        message: Входящее сообщение.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    await state.clear()
    await _open_step(
        message, message.from_user, state, session_factory, _NextStep.CALC
    )


@router.message(filters.Command("business"))
async def handle_business(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
) -> None:
    """Сбрасывает диалог и открывает бизнес и услуги владельца (B8).

    Args:
        message: Входящее сообщение.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    await state.clear()
    await _open_step(
        message, message.from_user, state, session_factory,
        _NextStep.BUSINESS,
    )


@router.message(filters.Command("new"))
async def handle_new_slot(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
) -> None:
    """Сбрасывает диалог и начинает создание слота (B8).

    Args:
        message: Входящее сообщение.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    await state.clear()
    await _open_step(
        message, message.from_user, state, session_factory,
        _NextStep.NEW_SLOT,
    )


@router.callback_query(aiogram.F.data == keyboards.CONSENT_CALLBACK)
async def handle_consent(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Создаёт пользователя после согласия и ведёт к выбранному шагу.

    Args:
        callback: Нажатие кнопки «Принимаю».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    async with session_factory() as db_session:
        user = await users.register_user(
            db_session, callback.from_user.id, callback.from_user.full_name
        )
    answers = await state.get_data()
    next_step = _NextStep(answers.get(_NEXT_STEP_KEY, _NextStep.MENU))
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    if next_step == _NextStep.SLOT:
        await buyer.open_slot(
            callback.message, state, session_factory, user.id,
            answers[_SLOT_ID_KEY], city_tz,
        )
        return
    await _go_to_step(
        callback.message, state, session_factory, user.id, next_step
    )


@router.callback_query(aiogram.F.data == keyboards.ROLE_OWNER_CALLBACK)
async def handle_owner_role(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: _SessionFactory,
) -> None:
    """Отмечает владельца и запускает калькулятор (B3.1).

    Args:
        callback: Нажатие кнопки «Я владелец».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    await callback.answer()
    await state.clear()
    await _open_step(
        callback.message,
        callback.from_user,
        state,
        session_factory,
        _NextStep.OWNER,
    )


@router.callback_query(aiogram.F.data == keyboards.ROLE_BUYER_CALLBACK)
async def handle_buyer_role(callback: types.CallbackQuery) -> None:
    """Объясняет покупателю, как бронировать слоты (B3.2).

    Args:
        callback: Нажатие кнопки «Я покупатель».
    """
    await callback.answer()
    await callback.message.answer(_BUYER_TEXT)


async def _open_step(
    message: types.Message,
    tg_user: types.User,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    next_step: _NextStep,
) -> None:
    async with session_factory() as db_session:
        user = await users.find_user(db_session, tg_user.id)
    if user is None:
        await _request_consent(message, state, next_step)
        return
    await _go_to_step(message, state, session_factory, user.id, next_step)


async def _open_slot_link(
    message: types.Message,
    tg_user: types.User,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
    slot_id: int,
) -> None:
    async with session_factory() as db_session:
        user = await users.find_user(db_session, tg_user.id)
    if user is None:
        await state.update_data({_SLOT_ID_KEY: slot_id})
        await _request_consent(message, state, _NextStep.SLOT)
        return
    await buyer.open_slot(
        message, state, session_factory, user.id, slot_id, city_tz
    )


async def _request_consent(
    message: types.Message,
    state: context.FSMContext,
    next_step: _NextStep,
) -> None:
    # Без согласия записи в users нет: шаг запоминаем, чтобы продолжить
    # после нажатия «Принимаю».
    await state.update_data({_NEXT_STEP_KEY: next_step.value})
    await message.answer(
        _CONSENT_TEXT, reply_markup=keyboards.build_consent_keyboard()
    )


def _parse_slot_link(args: str | None) -> int | None:
    if args is None or not args.startswith(feed.SLOT_DEEP_LINK_PREFIX):
        return None
    slot_id = args.removeprefix(feed.SLOT_DEEP_LINK_PREFIX)
    return int(slot_id) if slot_id.isdigit() else None


async def _go_to_step(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    user_id: int,
    next_step: _NextStep,
) -> None:
    if next_step == _NextStep.MENU:
        await state.clear()
        await message.answer(
            _MENU_TEXT, reply_markup=keyboards.build_role_keyboard()
        )
        return
    if next_step in _OWNER_STEPS:
        async with session_factory() as db_session:
            await users.mark_owner(db_session, user_id)
    if next_step == _NextStep.BUSINESS:
        await owner.open_business(message, state, session_factory, user_id)
        return
    if next_step == _NextStep.NEW_SLOT:
        await owner.start_new_slot(message, state, session_factory, user_id)
        return
    await owner.start_calc(message, state, user_id)


@fallback_router.callback_query()
async def handle_stale_button(callback: types.CallbackQuery) -> None:
    """Отвечает на нажатие устаревшей кнопки, чтобы не крутился индикатор.

    Args:
        callback: Нажатие, которое не обработал ни один роутер.
    """
    await callback.answer()

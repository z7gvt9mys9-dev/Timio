"""Общие команды: /start, /calc, согласие и главное меню (B2, B5, B8)."""

import enum

import aiogram
from aiogram import filters
from aiogram import types
from aiogram.fsm import context
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import keyboards
from bot.handlers import owner
from core import users

router = aiogram.Router()

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]

_OWNER_DEEP_LINK = "owner"
# Ключ в данных диалога: куда вести пользователя после согласия.
_NEXT_STEP_KEY = "next_step"
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


@router.message(filters.CommandStart())
async def handle_start(
    message: types.Message,
    command: filters.CommandObject,
    state: context.FSMContext,
    session_factory: _SessionFactory,
) -> None:
    """Сбрасывает диалог и открывает меню или калькулятор по deep link.

    Args:
        message: Входящее сообщение.
        command: Команда с аргументом deep link.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    await state.clear()
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


@router.callback_query(aiogram.F.data == keyboards.CONSENT_CALLBACK)
async def handle_consent(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: _SessionFactory,
) -> None:
    """Создаёт пользователя после согласия и ведёт к выбранному шагу.

    Args:
        callback: Нажатие кнопки «Принимаю».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    async with session_factory() as db_session:
        user = await users.register_user(
            db_session, callback.from_user.id, callback.from_user.full_name
        )
    answers = await state.get_data()
    next_step = _NextStep(answers.get(_NEXT_STEP_KEY, _NextStep.MENU))
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
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
    # Без согласия записи в users нет: сначала просим согласие, а шаг
    # запоминаем, чтобы продолжить после нажатия «Принимаю».
    async with session_factory() as db_session:
        user = await users.find_user(db_session, tg_user.id)
    if user is None:
        await state.update_data({_NEXT_STEP_KEY: next_step.value})
        await message.answer(
            _CONSENT_TEXT, reply_markup=keyboards.build_consent_keyboard()
        )
        return
    await _go_to_step(message, state, session_factory, user.id, next_step)


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
    if next_step == _NextStep.OWNER:
        async with session_factory() as db_session:
            await users.mark_owner(db_session, user_id)
    await owner.start_calc(message, state, user_id)

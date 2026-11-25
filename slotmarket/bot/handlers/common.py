"""Общие команды для всех пользователей (B8)."""

import aiogram
from aiogram import filters
from aiogram import types

router = aiogram.Router()


@router.message(filters.CommandStart())
async def handle_start(message: types.Message) -> None:
    """Отвечает на /start. Меню и deep link появятся в шаге 3.

    Args:
        message: Входящее сообщение.
    """
    await message.answer("Бот SlotMarket запущен.")

"""Клавиатуры бота."""

from aiogram import types

SKIP_TEXT = "Пропустить"


def build_skip_keyboard() -> types.ReplyKeyboardMarkup:
    """Создаёт клавиатуру с одной кнопкой «Пропустить».

    Returns:
        Клавиатура, которая скрывается после нажатия.
    """
    return types.ReplyKeyboardMarkup(
        keyboard=[[types.KeyboardButton(text=SKIP_TEXT)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )

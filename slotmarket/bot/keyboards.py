"""Клавиатуры бота."""

from aiogram import types

SKIP_TEXT = "Пропустить"
CONSENT_CALLBACK = "consent:accept"
ROLE_OWNER_CALLBACK = "role:owner"
ROLE_BUYER_CALLBACK = "role:buyer"


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


def build_consent_keyboard() -> types.InlineKeyboardMarkup:
    """Создаёт кнопку согласия на обработку данных (B5).

    Returns:
        Клавиатура с одной кнопкой «Принимаю».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Принимаю", callback_data=CONSENT_CALLBACK
                )
            ]
        ]
    )


def build_role_keyboard() -> types.InlineKeyboardMarkup:
    """Создаёт главное меню с выбором роли (B2).

    Returns:
        Клавиатура «Я владелец» и «Я покупатель».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Я владелец", callback_data=ROLE_OWNER_CALLBACK
                ),
                types.InlineKeyboardButton(
                    text="Я покупатель", callback_data=ROLE_BUYER_CALLBACK
                ),
            ]
        ]
    )

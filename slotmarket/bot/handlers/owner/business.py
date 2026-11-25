"""Регистрация бизнеса и его карточка (B3.1, пункт 3; B4.2)."""

import aiogram
from aiogram import filters
from aiogram import types
from aiogram.fsm import context

from bot import keyboards
from bot import states
from bot.handlers import admin
from bot.handlers.owner import shared
from core import businesses

router = aiogram.Router()

# Ключи совпадают с полями BusinessInput.
_BUSINESS_STEPS = (
    shared.Step(
        states.BusinessStates.name, "name",
        shared.text_parser(businesses.TextField.BUSINESS_NAME),
        "Как называется ваш бизнес?\nПример: Барбершоп «Борода»",
    ),
    shared.Step(
        states.BusinessStates.category, "category",
        businesses.parse_category, "Выберите категорию:",
        keyboards.build_category_keyboard(),
    ),
    shared.Step(
        states.BusinessStates.district, "district",
        shared.text_parser(businesses.TextField.DISTRICT),
        "В каком районе вы находитесь?\nПример: Хамовники",
    ),
    shared.Step(
        states.BusinessStates.address, "address",
        shared.text_parser(businesses.TextField.ADDRESS),
        "Какой у вас адрес?\nПример: ул. Ленина, 1",
    ),
    shared.Step(
        states.BusinessStates.phone, "phone", businesses.parse_phone,
        "Какой телефон для клиентов?\nПример: +79001234567",
    ),
)
_BUSINESS_INTRO_TEXT = "Зарегистрируем ваш бизнес.\n\n"
_APPLICATION_SENT_TEXT = (
    "Заявка отправлена на проверку. Мы сообщим, когда её одобрят.\n"
    "Пока ждёте, добавьте услуги."
)
_ALREADY_REGISTERED_TEXT = "У вас уже есть бизнес. Открыть: /business"


async def open_business(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
    user_id: int,
) -> None:
    """Показывает карточку бизнеса или начинает его регистрацию.

    Args:
        message: Сообщение, в чат которого отвечает бот.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        user_id: ID владельца.
    """
    await state.clear()
    is_sent = await shared.send_card(message, session_factory, user_id)
    if not is_sent:
        await shared.start_steps(
            message, state, _BUSINESS_STEPS, {shared.USER_ID_KEY: user_id},
            _BUSINESS_INTRO_TEXT,
        )


@router.message(
    filters.StateFilter(*(step.state for step in _BUSINESS_STEPS)),
    shared.ANSWER_FILTER,
)
async def handle_business_answer(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
    bot: aiogram.Bot,
    admin_ids: frozenset[int],
) -> None:
    """Принимает ответ при регистрации бизнеса.

    Args:
        message: Ответ пользователя.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        bot: Бот, чтобы уведомить администраторов.
        admin_ids: Telegram ID администраторов.
    """
    answers = await shared.accept_answer(
        message, state, _BUSINESS_STEPS, message.text
    )
    if answers is None:
        return
    user_id = answers.pop(shared.USER_ID_KEY)
    business_input = businesses.BusinessInput(**answers)
    async with session_factory() as db_session:
        try:
            business = await businesses.create_business(
                db_session, user_id, business_input
            )
        except businesses.BusinessAlreadyExistsError:
            await message.answer(_ALREADY_REGISTERED_TEXT)
            return
    await admin.notify_new_application(
        bot, admin_ids, business, message.from_user.full_name
    )
    await message.answer(_APPLICATION_SENT_TEXT)
    await shared.send_card(message, session_factory, user_id)


@router.callback_query(
    filters.StateFilter(states.BusinessStates.category),
    aiogram.F.data.startswith(keyboards.CATEGORY_PREFIX),
)
async def handle_category_button(
    callback: types.CallbackQuery, state: context.FSMContext
) -> None:
    """Принимает категорию, выбранную кнопкой.

    Категория не последний шаг, поэтому регистрация здесь не
    завершается.

    Args:
        callback: Нажатие кнопки категории.
        state: Состояние диалога.
    """
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    category = callback.data.removeprefix(keyboards.CATEGORY_PREFIX)
    await shared.accept_answer(
        callback.message, state, _BUSINESS_STEPS, category
    )

"""Услуги: добавить, скрыть или показать, изменить (B4.2)."""

import aiogram
from aiogram import filters
from aiogram import types
from aiogram.fsm import context

from bot import keyboards
from bot import states
from bot.handlers.owner import shared
from core import businesses
from core import feed
from db import models

router = aiogram.Router()

# Ключи совпадают с полями ServiceInput.
_SERVICE_STEPS = (
    shared.Step(
        states.ServiceStates.title, "title",
        shared.text_parser(businesses.TextField.SERVICE_TITLE),
        "Как называется услуга?\nПример: Мужская стрижка",
    ),
    shared.Step(
        states.ServiceStates.duration_min, "duration_min",
        businesses.parse_duration,
        "Сколько длится услуга, в минутах?\nПример: 60",
    ),
    shared.Step(
        states.ServiceStates.regular_price, "regular_price",
        businesses.parse_price, "Какая обычная цена услуги, ₽?\nПример: 1500",
    ),
)
# Ключ в данных диалога: какому бизнесу добавить услугу.
_BUSINESS_ID_KEY = "business_id"
_SERVICE_FIELD_KEY = "service_field"
_EDIT_QUESTIONS = {
    businesses.ServiceField.TITLE:
        "Введите новое название услуги.\nПример: Мужская стрижка",
    businesses.ServiceField.DURATION_MIN:
        "Введите новую длительность в минутах.\nПример: 60",
    businesses.ServiceField.REGULAR_PRICE:
        "Введите новую обычную цену, ₽. Она будет действовать только "
        "для новых слотов.\nПример: 1500",
}
_DURATION_LOCKED_TEXT = (
    "Длительность нельзя изменить, пока у услуги есть открытые "
    "или забронированные слоты."
)
_SERVICE_UPDATED_TEXT = "Услуга изменена."


@router.callback_query(aiogram.F.data == keyboards.SERVICE_ADD_CALLBACK)
async def handle_add_service(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Начинает добавление услуги.

    Args:
        callback: Нажатие «Добавить услугу».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    await callback.answer()
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    if business is None:
        await callback.message.answer(shared.NO_BUSINESS_TEXT)
        return
    await shared.start_steps(
        callback.message, state, _SERVICE_STEPS,
        {shared.USER_ID_KEY: business.owner_id, _BUSINESS_ID_KEY: business.id},
    )


@router.message(
    filters.StateFilter(*(step.state for step in _SERVICE_STEPS)),
    shared.ANSWER_FILTER,
)
async def handle_service_answer(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Принимает ответ при добавлении услуги.

    Args:
        message: Ответ пользователя.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    answers = await shared.accept_answer(
        message, state, _SERVICE_STEPS, message.text
    )
    if answers is None:
        return
    user_id = answers.pop(shared.USER_ID_KEY)
    business_id = answers.pop(_BUSINESS_ID_KEY)
    async with session_factory() as db_session:
        await businesses.add_service(
            db_session, business_id, businesses.ServiceInput(**answers)
        )
    await message.answer("Услуга добавлена.")
    await shared.send_card(message, session_factory, user_id)


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SERVICE_HIDE_PREFIX)
)
async def handle_hide_service(
    callback: types.CallbackQuery, session_factory: shared.SessionFactory
) -> None:
    """Скрывает услугу и обновляет карточку бизнеса.

    Args:
        callback: Нажатие «Скрыть».
        session_factory: Фабрика сессий базы.
    """
    await _toggle_service(
        callback, session_factory, keyboards.SERVICE_HIDE_PREFIX, False
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SERVICE_SHOW_PREFIX)
)
async def handle_show_service(
    callback: types.CallbackQuery, session_factory: shared.SessionFactory
) -> None:
    """Показывает скрытую услугу и обновляет карточку бизнеса.

    Args:
        callback: Нажатие «Показать».
        session_factory: Фабрика сессий базы.
    """
    await _toggle_service(
        callback, session_factory, keyboards.SERVICE_SHOW_PREFIX, True
    )


async def _toggle_service(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    prefix: str,
    is_active: bool,
) -> None:
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    service_id = callback.data.removeprefix(prefix)
    if business is None or not service_id.isdigit():
        await callback.answer(shared.SERVICE_NOT_FOUND_TEXT)
        return
    async with session_factory() as db_session:
        is_changed = await businesses.set_service_active(
            db_session, business.owner_id, int(service_id), is_active
        )
    if not is_changed:
        await callback.answer(shared.SERVICE_NOT_FOUND_TEXT)
        return
    await callback.answer()
    await shared.send_card(
        callback.message, session_factory, business.owner_id, is_edit=True
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SERVICE_EDIT_PREFIX)
)
async def handle_edit_service(
    callback: types.CallbackQuery, session_factory: shared.SessionFactory
) -> None:
    """Предлагает выбрать поле услуги для правки (B4.2).

    Args:
        callback: Нажатие «Изменить».
        session_factory: Фабрика сессий базы.
    """
    owned = await shared.find_owned_service(
        session_factory,
        callback.from_user.id,
        callback.data.removeprefix(keyboards.SERVICE_EDIT_PREFIX),
    )
    if owned is None:
        await callback.answer(shared.SERVICE_NOT_FOUND_TEXT)
        return
    await callback.answer()
    await callback.message.answer(
        f"Что изменить в услуге «{owned.service.title}»?",
        reply_markup=keyboards.build_service_fields_keyboard(
            owned.service.id
        ),
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SERVICE_FIELD_PREFIX)
)
async def handle_service_field(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Запрашивает новое значение выбранного поля услуги.

    Args:
        callback: Нажатие «Название», «Длительность» или «Цена».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    payload = callback.data.removeprefix(keyboards.SERVICE_FIELD_PREFIX)
    service_id, _, field_value = payload.partition(
        keyboards.SERVICE_FIELD_SEPARATOR
    )
    owned = await shared.find_owned_service(
        session_factory, callback.from_user.id, service_id
    )
    field = _parse_service_field(field_value)
    if owned is None or field is None:
        await callback.answer(shared.SERVICE_NOT_FOUND_TEXT)
        return
    if await _is_duration_locked(session_factory, owned.service, field):
        await callback.answer(_DURATION_LOCKED_TEXT, show_alert=True)
        return
    await callback.answer()
    await state.clear()
    await state.set_state(states.ServiceEditStates.value)
    await state.update_data({
        shared.USER_ID_KEY: owned.owner_id,
        shared.SERVICE_ID_KEY: owned.service.id,
        _SERVICE_FIELD_KEY: field.value,
    })
    await callback.message.answer(
        f"Сейчас: {_format_service_field(owned.service, field)}\n\n"
        f"{_EDIT_QUESTIONS[field]}"
    )


@router.message(
    filters.StateFilter(states.ServiceEditStates.value), shared.ANSWER_FILTER
)
async def handle_service_edit_value(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Сохраняет новое значение поля услуги и показывает карточку.

    Args:
        message: Новое значение.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    answers = await state.get_data()
    field = businesses.ServiceField(answers[_SERVICE_FIELD_KEY])
    try:
        async with session_factory() as db_session:
            result = await businesses.update_service(
                db_session, answers[shared.USER_ID_KEY],
                answers[shared.SERVICE_ID_KEY], field, message.text,
            )
    except ValueError as error:
        await message.answer(f"{error}\n\n{_EDIT_QUESTIONS[field]}")
        return
    await state.clear()
    result_texts = {
        businesses.ServiceUpdateResult.UPDATED: _SERVICE_UPDATED_TEXT,
        businesses.ServiceUpdateResult.NOT_FOUND: shared.SERVICE_NOT_FOUND_TEXT,
        businesses.ServiceUpdateResult.DURATION_LOCKED: _DURATION_LOCKED_TEXT,
    }
    await message.answer(result_texts[result])
    await shared.send_card(
        message, session_factory, answers[shared.USER_ID_KEY]
    )


def _parse_service_field(
    field_value: str,
) -> businesses.ServiceField | None:
    try:
        return businesses.ServiceField(field_value)
    except ValueError:
        return None


async def _is_duration_locked(
    session_factory: shared.SessionFactory,
    service: models.Service,
    field: businesses.ServiceField,
) -> bool:
    # Ранняя проверка, чтобы не просить ввод зря. Окончательная стоит
    # в update_service.
    if field != businesses.ServiceField.DURATION_MIN:
        return False
    async with session_factory() as db_session:
        return await businesses.has_active_slots(db_session, service.id)


def _format_service_field(
    service: models.Service, field: businesses.ServiceField
) -> str:
    if field == businesses.ServiceField.TITLE:
        return service.title
    if field == businesses.ServiceField.DURATION_MIN:
        return f"{service.duration_min} мин"
    return feed.format_rubles(service.regular_price)

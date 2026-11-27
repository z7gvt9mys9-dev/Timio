"""Слоты владельца: /new, список, снятие, повтор вчерашних (B4.3)."""

import datetime
import logging

import aiogram
from aiogram import exceptions
from aiogram import filters
from aiogram import types
from aiogram.fsm import context

from bot import channel
from bot import keyboards
from bot import states
from bot.handlers import admin
from bot.handlers.owner import shared
from core import bookings
from core import businesses
from core import calculator
from core import feed
from core import pricing
from core import slots
from db import models

router = aiogram.Router()

_LOG = logging.getLogger(__name__)

# Ключи в данных диалога создания слота.
_DAY_OFFSET_KEY = "day_offset"
_STARTS_AT_KEY = "starts_at"
_START_PRICE_KEY = "start_price"
_MIN_PRICE_KEY = "min_price"
_HIDE_NAME_KEY = "hide_name"
_NOT_APPROVED_TEXT = "Слоты можно выставлять после одобрения заявки."
_NO_ACTIVE_SERVICES_TEXT = "Сначала добавьте услугу: /business"
_NEW_SLOT_TEXT = "Новый слот. Выберите услугу:"
_DAY_QUESTION = "Какой день?"
_TIME_QUESTION = "Во сколько начало? Время города.\nПример: 15:00"
_HIDE_NAME_QUESTION = (
    "Скрыть название бизнеса в ленте? Покупатель увидит категорию, "
    "район, время и цену, а название и адрес — после брони."
)
_NO_SLOTS_TEXT = "Ближайших слотов нет. Создать: /new"
_SLOT_NOT_WITHDRAWN_TEXT = "Слот уже забронирован или закрыт."
_SLOT_NOT_BOOKED_TEXT = "На слоте уже нет брони. Обновите список: «Слоты»."
_CONFIRM_WITHDRAW_TEXT = (
    "Точно снять слот? На него есть бронь: клиент получит уведомление "
    "об отмене, отмена будет записана на бизнес."
)
_WITHDRAWN_TEXT = "Слот снят."
_SLOT_STATUS_LABELS = {
    models.SlotStatus.OPEN: "открыт",
    models.SlotStatus.BOOKED: "забронирован",
}


async def start_new_slot(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
    user_id: int,
) -> None:
    """Начинает создание слота с выбора услуги (B3.1, пункт 6).

    Args:
        message: Сообщение, в чат которого отвечает бот.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        user_id: ID владельца.
    """
    await state.clear()
    async with session_factory() as db_session:
        business = await businesses.find_owner_business(db_session, user_id)
        services = [] if business is None else [
            service
            for service in await businesses.list_services(
                db_session, business.id
            )
            if service.is_active
        ]
    if business is None:
        await message.answer(shared.NO_BUSINESS_TEXT)
        return
    if business.status != models.BusinessStatus.APPROVED:
        await message.answer(_NOT_APPROVED_TEXT)
        return
    if not services:
        await message.answer(_NO_ACTIVE_SERVICES_TEXT)
        return
    await state.set_state(states.NewSlotStates.service)
    await state.update_data({shared.USER_ID_KEY: user_id})
    await message.answer(
        _NEW_SLOT_TEXT,
        reply_markup=keyboards.build_new_slot_services_keyboard(services),
    )


@router.callback_query(
    filters.StateFilter(states.NewSlotStates.service),
    aiogram.F.data.startswith(keyboards.NEW_SLOT_SERVICE_PREFIX),
)
async def handle_new_slot_service(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Запоминает услугу слота и спрашивает день.

    Args:
        callback: Нажатие кнопки услуги.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    owned = await shared.find_owned_service(
        session_factory,
        callback.from_user.id,
        callback.data.removeprefix(keyboards.NEW_SLOT_SERVICE_PREFIX),
    )
    if owned is None or not owned.service.is_active:
        await callback.answer(shared.SERVICE_NOT_FOUND_TEXT)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data({shared.SERVICE_ID_KEY: owned.service.id})
    await state.set_state(states.NewSlotStates.day)
    await callback.message.answer(
        f"Услуга: {owned.service.title}\n\n{_DAY_QUESTION}",
        reply_markup=keyboards.build_day_keyboard(),
    )


@router.callback_query(
    filters.StateFilter(states.NewSlotStates.day),
    aiogram.F.data.startswith(keyboards.NEW_SLOT_DAY_PREFIX),
)
async def handle_new_slot_day(
    callback: types.CallbackQuery, state: context.FSMContext
) -> None:
    """Запоминает день слота и спрашивает время.

    Args:
        callback: Нажатие «Сегодня», «Завтра» или «Послезавтра».
        state: Состояние диалога.
    """
    offset = callback.data.removeprefix(keyboards.NEW_SLOT_DAY_PREFIX)
    if not offset.isdigit() or int(offset) not in keyboards.DAY_OFFSETS:
        await callback.answer()
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data({_DAY_OFFSET_KEY: int(offset)})
    await state.set_state(states.NewSlotStates.time)
    await callback.message.answer(
        f"День: {keyboards.DAY_OFFSETS[int(offset)]}\n\n{_TIME_QUESTION}"
    )


@router.message(
    filters.StateFilter(states.NewSlotStates.time), shared.ANSWER_FILTER
)
async def handle_new_slot_time(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Проверяет время начала и спрашивает стартовую цену.

    Args:
        message: Время в виде ЧЧ:ММ.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    answers = await state.get_data()
    now = models.utc_now()
    day = now.astimezone(city_tz).date() + datetime.timedelta(
        days=answers[_DAY_OFFSET_KEY]
    )
    try:
        starts_at = slots.build_starts_at(day, message.text, city_tz)
        slots.validate_starts_at(starts_at, now)
    except ValueError as error:
        await message.answer(f"{error}\n\n{_TIME_QUESTION}")
        return
    await state.update_data({_STARTS_AT_KEY: starts_at.isoformat()})
    await state.set_state(states.NewSlotStates.start_price)
    async with session_factory() as db_session:
        service = await businesses.find_owner_service(
            db_session, answers[shared.USER_ID_KEY],
            answers[shared.SERVICE_ID_KEY],
        )
    await message.answer(_start_price_question(service.regular_price))


@router.message(
    filters.StateFilter(states.NewSlotStates.start_price),
    shared.ANSWER_FILTER,
)
async def handle_new_slot_start_price(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Проверяет стартовую цену и спрашивает минимальную (B4.3).

    Args:
        message: Стартовая цена, ₽.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    answers = await state.get_data()
    async with session_factory() as db_session:
        service = await businesses.find_owner_service(
            db_session, answers[shared.USER_ID_KEY],
            answers[shared.SERVICE_ID_KEY],
        )
    try:
        start_price = slots.parse_start_price(
            message.text, service.regular_price
        )
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.update_data({_START_PRICE_KEY: start_price})
    await state.set_state(states.NewSlotStates.min_price)
    await message.answer(
        await _min_price_question(
            session_factory,
            answers[shared.USER_ID_KEY],
            answers[shared.SERVICE_ID_KEY],
            start_price,
        )
    )


@router.message(
    filters.StateFilter(states.NewSlotStates.min_price), shared.ANSWER_FILTER
)
async def handle_new_slot_min_price(
    message: types.Message,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Проверяет минимальную цену и спрашивает про скрытое название.

    Args:
        message: Минимальная цена, ₽.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    answers = await state.get_data()
    user_id = answers[shared.USER_ID_KEY]
    async with session_factory() as db_session:
        service = await businesses.find_owner_service(
            db_session, user_id, answers[shared.SERVICE_ID_KEY]
        )
        variable_cost = await calculator.find_latest_variable_cost(
            db_session, user_id
        )
    try:
        min_price = slots.parse_min_price(
            message.text, service.regular_price, answers[_START_PRICE_KEY]
        )
    except ValueError as error:
        await message.answer(str(error))
        return
    await state.update_data({_MIN_PRICE_KEY: min_price})
    await state.set_state(states.NewSlotStates.hide_name)
    text = _HIDE_NAME_QUESTION
    if variable_cost is not None and pricing.is_below_variable_cost(
        min_price, variable_cost, service.duration_min
    ):
        text = (
            "Эта цена ниже ваших переменных расходов на услугу из "
            f"калькулятора. Продажа по ней будет в убыток.\n\n{text}"
        )
    await message.answer(
        text, reply_markup=keyboards.build_hide_name_keyboard()
    )


@router.callback_query(
    filters.StateFilter(states.NewSlotStates.hide_name),
    aiogram.F.data.startswith(keyboards.NEW_SLOT_HIDE_PREFIX),
)
async def handle_new_slot_hide_name(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Запоминает режим скрытого названия и показывает сводку.

    Args:
        callback: Нажатие «Да, скрыть» или «Нет».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    choice = callback.data.removeprefix(keyboards.NEW_SLOT_HIDE_PREFIX)
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await state.update_data({_HIDE_NAME_KEY: choice == keyboards.HIDE_NAME_YES})
    await state.set_state(states.NewSlotStates.confirm)
    answers = await state.get_data()
    async with session_factory() as db_session:
        service = await businesses.find_owner_service(
            db_session,
            answers[shared.USER_ID_KEY],
            answers[shared.SERVICE_ID_KEY],
        )
    await callback.message.answer(
        _format_slot_summary(_slot_input(answers), service, city_tz),
        reply_markup=keyboards.build_publish_keyboard(),
    )


@router.callback_query(
    filters.StateFilter(states.NewSlotStates.confirm),
    aiogram.F.data == keyboards.NEW_SLOT_PUBLISH_CALLBACK,
)
async def handle_new_slot_publish(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Публикует слот и его пост в канале (B4.6).

    Args:
        callback: Нажатие «Опубликовать».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        bot: Бот, который пишет в канал.
        channel_config: Канал и часовой пояс.
    """
    answers = await state.get_data()
    await state.clear()
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    try:
        async with session_factory() as db_session:
            slot = await slots.create_slot(
                db_session, answers[shared.USER_ID_KEY], _slot_input(answers),
                models.utc_now(),
            )
    except slots.SlotRejectedError as error:
        # Например, пока владелец думал, до начала осталось меньше 2 ч.
        await callback.message.answer(f"{error}\n\nНачать заново: /new")
        return
    await callback.message.answer(
        "Слот опубликован. Цена сейчас "
        f"{feed.format_rubles(slot.current_price)}, к 2 часам до начала "
        f"снизится до {feed.format_rubles(slot.min_price)}."
        "\n\nВаши слоты: кнопка «Слоты» в /business"
    )
    await channel.publish_slots(
        bot, session_factory, channel_config, [slot.id]
    )


@router.callback_query(
    filters.StateFilter(states.NewSlotStates.confirm),
    aiogram.F.data == keyboards.NEW_SLOT_RESTART_CALLBACK,
)
async def handle_new_slot_restart(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
) -> None:
    """Начинает создание слота заново.

    Args:
        callback: Нажатие «Заново».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
    """
    answers = await state.get_data()
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=None)
    await start_new_slot(
        callback.message, state, session_factory, answers[shared.USER_ID_KEY]
    )


@router.callback_query(aiogram.F.data == keyboards.NEW_SLOT_REPEAT_CALLBACK)
async def handle_repeat_yesterday(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Копирует вчерашние слоты на сегодня и публикует их (B4.3, B4.6).

    Args:
        callback: Нажатие «Повторить вчерашние слоты».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
        bot: Бот, который пишет в канал.
        channel_config: Канал и часовой пояс.
    """
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    if business is None:
        await callback.answer(shared.NO_BUSINESS_TEXT)
        return
    await callback.answer()
    await state.clear()
    async with session_factory() as db_session:
        result = await slots.repeat_yesterday(
            db_session, business.owner_id, models.utc_now(), city_tz
        )
    await callback.message.answer(
        f"Создано слотов: {len(result.created_ids)}. "
        f"Пропущено: {result.skipped} "
        "(уже есть, меньше 2 часов до начала или услуга скрыта)."
    )
    await _send_slots(
        callback.message, session_factory, business.owner_id, city_tz
    )
    await channel.publish_slots(
        bot, session_factory, channel_config, result.created_ids
    )


@router.callback_query(aiogram.F.data == keyboards.SLOTS_LIST_CALLBACK)
async def handle_slots_list(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Показывает ближайшие слоты владельца с кнопками «Снять».

    Args:
        callback: Нажатие «Слоты».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    if business is None:
        await callback.answer(shared.NO_BUSINESS_TEXT)
        return
    await callback.answer()
    await _send_slots(
        callback.message, session_factory, business.owner_id, city_tz
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SLOT_WITHDRAW_PREFIX)
)
async def handle_withdraw_slot(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Снимает открытый слот, обновляет список и пост (B4.3, B4.6).

    Args:
        callback: Нажатие «Снять».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
        bot: Бот, который пишет в канал.
        channel_config: Канал и часовой пояс.
    """
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    slot_id = callback.data.removeprefix(keyboards.SLOT_WITHDRAW_PREFIX)
    is_withdrawn = False
    if business is not None and slot_id.isdigit():
        async with session_factory() as db_session:
            is_withdrawn = await slots.withdraw_slot(
                db_session, business.owner_id, int(slot_id)
            )
    if not is_withdrawn:
        await callback.answer(_SLOT_NOT_WITHDRAWN_TEXT, show_alert=True)
        return
    await callback.answer("Слот снят.")
    await _send_slots(
        callback.message, session_factory, business.owner_id, city_tz,
        is_edit=True,
    )
    await channel.refresh_posts(
        bot, session_factory, channel_config, [int(slot_id)]
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SLOT_WITHDRAW_BOOKED_PREFIX)
)
async def handle_withdraw_booked(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Спрашивает подтверждение перед снятием слота с бронью (B9).

    Args:
        callback: Нажатие «Снять ... (есть бронь)».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    item = await _find_booked_owner_slot(
        session_factory,
        callback.from_user.id,
        callback.data.removeprefix(keyboards.SLOT_WITHDRAW_BOOKED_PREFIX),
    )
    if item is None:
        await callback.answer(_SLOT_NOT_BOOKED_TEXT, show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        f"{_CONFIRM_WITHDRAW_TEXT}\n\n{item.service_title}, "
        f"{feed.format_slot_time(item.slot.starts_at, city_tz)}",
        reply_markup=keyboards.build_confirm_withdraw_keyboard(item.slot.id),
    )


@router.callback_query(
    aiogram.F.data == keyboards.SLOT_WITHDRAW_KEEP_CALLBACK
)
async def handle_withdraw_keep(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Оставляет слот с бронью и возвращает список слотов.

    Args:
        callback: Нажатие «Нет».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    await callback.answer()
    if business is not None:
        await _send_slots(
            callback.message, session_factory, business.owner_id, city_tz,
            is_edit=True,
        )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SLOT_WITHDRAW_CONFIRM_PREFIX)
)
async def handle_withdraw_confirm(
    callback: types.CallbackQuery,
    session_factory: shared.SessionFactory,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
    admin_ids: frozenset[int],
) -> None:
    """Снимает слот с бронью: booked → withdrawn, уведомляет клиента (B9).

    С третьего такого снятия сообщает админам.

    Args:
        callback: Нажатие «Да, снять».
        session_factory: Фабрика сессий базы.
        bot: Бот для уведомлений и канала.
        channel_config: Канал и часовой пояс.
        admin_ids: Telegram ID администраторов.
    """
    business = await shared.find_business_by_tg_id(
        session_factory, callback.from_user.id
    )
    slot_id = callback.data.removeprefix(
        keyboards.SLOT_WITHDRAW_CONFIRM_PREFIX
    )
    details = None
    if business is not None and slot_id.isdigit():
        async with session_factory() as db_session:
            details = await bookings.withdraw_booked_slot(
                db_session, business.owner_id, int(slot_id),
                models.utc_now(),
            )
    if details is None:
        # Например, клиент успел отменить бронь сам.
        await callback.answer(_SLOT_NOT_BOOKED_TEXT, show_alert=True)
        return
    await callback.answer(_WITHDRAWN_TEXT)
    city_tz = channel_config.city_tz
    await _send_slots(
        callback.message, session_factory, business.owner_id, city_tz,
        is_edit=True,
    )
    await _notify_buyer_withdrawn(bot, details, city_tz)
    await channel.refresh_posts(
        bot, session_factory, channel_config, [details.slot_id]
    )
    async with session_factory() as db_session:
        withdrawals = await bookings.count_owner_withdrawals(
            db_session, business.owner_id
        )
    if bookings.should_alert_admins(withdrawals):
        await admin.notify_owner_withdrawals(
            bot, admin_ids, business, withdrawals
        )


async def _find_booked_owner_slot(
    session_factory: shared.SessionFactory, tg_id: int, slot_id: str
) -> slots.OwnerSlot | None:
    business = await shared.find_business_by_tg_id(session_factory, tg_id)
    if business is None or not slot_id.isdigit():
        return None
    async with session_factory() as db_session:
        owner_slots = await slots.list_owner_slots(
            db_session, business.owner_id, models.utc_now()
        )
    for item in owner_slots:
        is_booked = item.slot.status == models.SlotStatus.BOOKED
        if item.slot.id == int(slot_id) and is_booked:
            return item
    return None


async def _notify_buyer_withdrawn(
    bot: aiogram.Bot,
    details: bookings.BookingDetails,
    city_tz: datetime.tzinfo,
) -> None:
    # Уведомление «Слот снят бизнесом после брони» (B4.7).
    text = "\n".join([
        "Бизнес снял слот, ваша бронь отменена.",
        "",
        f"{details.service_title}, "
        f"{feed.format_slot_time(details.starts_at, city_tz)}",
        details.business_name,
        "",
        "Другие слоты: /slots",
    ])
    try:
        await bot.send_message(details.buyer_tg_id, text)
    except exceptions.TelegramAPIError as error:
        # Покупатель мог заблокировать бота: бронь уже отменена (B9).
        _LOG.warning(
            "Уведомление о снятии слота %s не доставлено: %s",
            details.slot_id, error,
        )


def _start_price_question(regular_price: int) -> str:
    price_range = pricing.calc_start_price_range(regular_price)
    default_price = pricing.calc_start_price(regular_price)
    return "\n".join([
        "Какая стартовая цена, ₽? С неё цена начнёт снижаться.",
        f"Допустимо: от {price_range.lower} до {price_range.upper} "
        "(обычная цена).",
        f"Рекомендуем: {default_price} (обычная минус 15%).",
        f"Пример: {default_price}",
    ])


async def _min_price_question(
    session_factory: shared.SessionFactory,
    user_id: int,
    service_id: int,
    start_price: int,
) -> str:
    async with session_factory() as db_session:
        service = await businesses.find_owner_service(
            db_session, user_id, service_id
        )
        variable_cost = await calculator.find_latest_variable_cost(
            db_session, user_id
        )
    price_range = pricing.calc_min_price_range(
        service.regular_price, start_price
    )
    lines = [
        "Какая минимальная цена, ₽? Цена будет снижаться от "
        f"{feed.format_rubles(price_range.upper)} до неё.",
        f"Допустимо: от {price_range.lower} до {price_range.upper}.",
    ]
    if variable_cost is not None:
        recommendation = pricing.recommend_min_price(
            variable_cost, service.duration_min, service.regular_price,
            start_price,
        )
        lines.append(
            f"Рекомендуем: {recommendation.price} (расходы из калькулятора "
            "плюс 10%)."
        )
    lines.append(f"Пример: {price_range.upper}")
    return "\n".join(lines)


def _slot_input(answers: dict[str, object]) -> slots.SlotInput:
    return slots.SlotInput(
        service_id=answers[shared.SERVICE_ID_KEY],
        starts_at=datetime.datetime.fromisoformat(answers[_STARTS_AT_KEY]),
        start_price=answers[_START_PRICE_KEY],
        min_price=answers[_MIN_PRICE_KEY],
        hide_name=answers[_HIDE_NAME_KEY],
    )


def _format_slot_summary(
    slot_input: slots.SlotInput,
    service: models.Service,
    city_tz: datetime.tzinfo,
) -> str:
    current_price = slots.calc_input_price(slot_input, models.utc_now())
    hide_label = "да" if slot_input.hide_name else "нет"
    return "\n".join([
        "Проверьте слот:",
        f"Услуга: {service.title}",
        f"Начало: {shared.format_local(slot_input.starts_at, city_tz)}",
        f"Обычная цена: {feed.format_rubles(service.regular_price)}",
        f"Стартовая цена: {feed.format_rubles(slot_input.start_price)}",
        f"Цена сейчас: {feed.format_rubles(current_price)}",
        f"Снизится до: {feed.format_rubles(slot_input.min_price)}",
        f"Скрыть название: {hide_label}",
    ])


async def _send_slots(
    message: types.Message,
    session_factory: shared.SessionFactory,
    owner_id: int,
    city_tz: datetime.tzinfo,
    is_edit: bool = False,
) -> None:
    async with session_factory() as db_session:
        owner_slots = await slots.list_owner_slots(
            db_session, owner_id, models.utc_now()
        )
    if owner_slots:
        lines = ["Ближайшие слоты:"]
        lines.extend(_format_owner_slot(item, city_tz) for item in owner_slots)
        text = "\n".join(lines)
    else:
        text = _NO_SLOTS_TEXT
    markup = keyboards.build_owner_slots_keyboard(owner_slots, city_tz)
    if is_edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)


def _format_owner_slot(
    item: slots.OwnerSlot, city_tz: datetime.tzinfo
) -> str:
    slot = item.slot
    return (
        f"- {shared.format_local(slot.starts_at, city_tz)} — "
        f"{item.service_title}, "
        f"{feed.format_rubles(slot.current_price)} "
        f"(до {feed.format_rubles(slot.min_price)}), "
        f"{_SLOT_STATUS_LABELS[slot.status]}"
    )

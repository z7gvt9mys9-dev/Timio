"""Покупатель: /slots, карточка слота и бронь (B3.2, B4.5, B4.6, B8).

/slots работает без согласия на обработку данных: просмотр ничего
не сохраняет (B4.6). Бронь — только после согласия и с телефоном.
"""

import collections.abc
import dataclasses
import datetime
import logging

import aiogram
from aiogram import exceptions
from aiogram import filters
from aiogram import types
from aiogram.enums import ParseMode
from aiogram.fsm import context
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import channel
from bot import keyboards
from bot import states
from core import bookings
from core import businesses
from core import feed
from core import users
from db import models

router = aiogram.Router()

_LOG = logging.getLogger(__name__)

_SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]
# Сообщение Telegram вмещает 4096 символов: больше 20 слотов не влезет.
MAX_LISTED_SLOTS = 20
_HEADER_TEXT = "Слоты на сегодня и завтра"
_EMPTY_TEXT = (
    "Свободных слотов на сегодня и завтра нет. Новые слоты появляются "
    "в канале."
)
_TRUNCATED_TEXT = (
    "Показаны первые {shown} из {total}. Выберите категорию, чтобы "
    "увидеть остальные."
)
_NOT_MODIFIED_ERROR = "message is not modified"
# Ключ в данных диалога: какой слот бронировать после телефона.
_SLOT_ID_KEY = "slot_id"
_ANSWER_FILTER = aiogram.F.text & ~aiogram.F.text.startswith("/")
_SLOT_UNAVAILABLE_TEXT = "Слот уже недоступен. Другие слоты: /slots"
_HIDDEN_NAME_NOTE = "Название и адрес откроются после брони."
_PAY_ON_SITE_TEXT = "Оплата на месте."
_START_FIRST_TEXT = "Сначала нажмите /start."
_PHONE_REQUEST_TEXT = (
    "Для брони нужен телефон. Его увидит только бизнес, у которого вы "
    f"бронируете слот.\n\nНажмите кнопку «{keyboards.SEND_PHONE_TEXT}» "
    "ниже."
)
_OWN_CONTACT_TEXT = (
    f"Отправьте свой номер кнопкой «{keyboards.SEND_PHONE_TEXT}»."
)
_PHONE_SAVED_TEXT = "Телефон сохранён."
_NO_PHONE_TEXT = "Сначала отправьте телефон: откройте слот ещё раз."
_BLOCKED_TEXT = "Бронирование недоступно до {date}."
_PERMANENT_BLOCKED_TEXT = "Бронирование недоступно."
_BLOCKED_DATE_FORMAT = "%d.%m.%Y"
_OUTCOME_TEXTS = {
    bookings.BookOutcome.TAKEN: "Слот только что забрали. Другие слоты: /slots",
    bookings.BookOutcome.LIMIT_REACHED: (
        f"У вас уже {bookings.MAX_ACTIVE_BOOKINGS} активные брони. "
        "Посмотреть или отменить: /my"
    ),
    bookings.BookOutcome.OWN_SLOT: "Нельзя забронировать слот своего бизнеса.",
    bookings.BookOutcome.NO_PHONE: _NO_PHONE_TEXT,
    bookings.BookOutcome.UNAVAILABLE: _SLOT_UNAVAILABLE_TEXT,
}


_NO_BOOKINGS_TEXT = "Броней пока нет. Свободные слоты: /slots"
_MY_HEADER_TEXT = "Ваши брони:"
_MY_FOOTER_TEXT = "Отменить можно не позднее чем за час до начала."
_TOO_LATE_NOTE = "Отменить уже нельзя: до начала меньше часа."
_CANCELLED_TEXT = "Бронь отменена."
_CONFIRM_CANCEL_TEXT = "Точно отменить бронь?"
_CANCEL_OUTCOME_TEXTS = {
    bookings.CancelOutcome.TOO_LATE: _TOO_LATE_NOTE,
    bookings.CancelOutcome.NOT_FOUND: "Бронь не найдена или уже отменена.",
}


@dataclasses.dataclass(frozen=True)
class _MyBookings:
    text: str
    markup: types.InlineKeyboardMarkup | None


@dataclasses.dataclass(frozen=True)
class _BookingDeps:
    """Что нужно для брони: база, бот и канал для пометки поста."""

    session_factory: _SessionFactory
    bot: aiogram.Bot
    channel_config: channel.ChannelConfig


@dataclasses.dataclass(frozen=True)
class _Listing:
    text: str
    markup: types.InlineKeyboardMarkup | None


@router.message(filters.Command("slots"))
async def handle_slots(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    city_tz: datetime.tzinfo,
) -> None:
    """Сбрасывает диалог и показывает открытые слоты (B8).

    Args:
        message: Входящее сообщение.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        bot: Бот: нужен username для ссылок на бронь.
        city_tz: Часовой пояс города.
    """
    await state.clear()
    listing = await _build_listing(bot, session_factory, city_tz, None)
    await message.answer(
        listing.text, parse_mode=ParseMode.HTML, reply_markup=listing.markup
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.SLOTS_CATEGORY_PREFIX)
)
async def handle_slots_category(
    callback: types.CallbackQuery,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    city_tz: datetime.tzinfo,
) -> None:
    """Показывает слоты выбранной категории в том же сообщении.

    Args:
        callback: Нажатие кнопки категории или «Все».
        session_factory: Фабрика сессий базы.
        bot: Бот: нужен username для ссылок на бронь.
        city_tz: Часовой пояс города.
    """
    await callback.answer()
    value = callback.data.removeprefix(keyboards.SLOTS_CATEGORY_PREFIX)
    # Категорию из кнопки сверяем со списком: данные кнопки можно подделать.
    category = value if value in businesses.CATEGORIES else None
    listing = await _build_listing(bot, session_factory, city_tz, category)
    try:
        await callback.message.edit_text(
            listing.text,
            parse_mode=ParseMode.HTML,
            reply_markup=listing.markup,
        )
    except exceptions.TelegramBadRequest as error:
        if _NOT_MODIFIED_ERROR not in str(error):
            _LOG.warning("Список слотов не обновлён: %s", error)


def build_slots_text(
    cards: collections.abc.Sequence[feed.SlotCard],
    total: int,
    category: str | None,
    now: datetime.datetime,
    city_tz: datetime.tzinfo,
) -> str:
    """Собирает текст списка /slots с номерами для кнопок брони.

    Название бизнеса в режиме скрытого названия не раскрывается:
    строки собирает feed.build_list_item_text (CLAUDE.md §5).

    Args:
        cards: Показываемые слоты, не больше MAX_LISTED_SLOTS.
        total: Сколько слотов нашлось всего.
        category: Выбранная категория или None.
        now: Текущее время для «Сегодня» и «Завтра».
        city_tz: Часовой пояс города.

    Returns:
        Текст в HTML.
    """
    header = _HEADER_TEXT if category is None else (
        f"{_HEADER_TEXT}, категория: {category}"
    )
    if not cards:
        return f"{header}\n\n{_EMPTY_TEXT}"
    items = [
        f"{number}. {feed.build_list_item_text(card, now, city_tz)}"
        for number, card in enumerate(cards, start=1)
    ]
    text = "\n\n".join([header, *items])
    if total > len(cards):
        text += "\n\n" + _TRUNCATED_TEXT.format(
            shown=len(cards), total=total
        )
    return text


async def _build_listing(
    bot: aiogram.Bot,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
    category: str | None,
) -> _Listing:
    now = models.utc_now()
    async with session_factory() as db_session:
        all_cards = await feed.list_open_cards(db_session, now, city_tz)
        cards = all_cards if category is None else (
            await feed.list_open_cards(db_session, now, city_tz, category)
        )
    present = {card.category for card in all_cards}
    categories = [item for item in businesses.CATEGORIES if item in present]
    shown = cards[:MAX_LISTED_SLOTS]
    booking_urls = [
        await channel.build_booking_url(bot, card.slot_id) for card in shown
    ]
    return _Listing(
        text=build_slots_text(shown, len(cards), category, now, city_tz),
        markup=keyboards.build_slots_list_keyboard(booking_urls, categories),
    )


async def open_slot(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    user_id: int,
    slot_id: int,
    city_tz: datetime.tzinfo,
) -> None:
    """Показывает карточку слота из deep link с кнопкой брони (B3.2).

    При скрытом названии карточка, как и пост, не раскрывает название
    и адрес (CLAUDE.md §5).

    Args:
        message: Сообщение, в чат которого отвечает бот.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        user_id: ID покупателя, давшего согласие.
        slot_id: ID слота из deep link.
        city_tz: Часовой пояс города.
    """
    await state.clear()
    now = models.utc_now()
    async with session_factory() as db_session:
        cards = await feed.load_cards(db_session, [slot_id])
        price = await bookings.quote_price(db_session, slot_id, now)
        if cards:
            await bookings.record_slot_opened(db_session, user_id, slot_id)
    if not cards or price is None:
        await message.answer(_SLOT_UNAVAILABLE_TEXT)
        return
    # Цена на карточке — на этот момент, а не из последнего пересчёта.
    card = dataclasses.replace(cards[0], current_price=price)
    lines = [feed.build_list_item_text(card, now, city_tz)]
    if card.hide_name:
        lines.append(_HIDDEN_NAME_NOTE)
    lines.append(_PAY_ON_SITE_TEXT)
    await message.answer(
        "\n\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboards.build_book_keyboard(slot_id, price),
    )


@router.callback_query(aiogram.F.data.startswith(keyboards.BOOK_PREFIX))
async def handle_book(
    callback: types.CallbackQuery,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Бронирует слот или сначала просит телефон (B3.2, пункты 2–3).

    Args:
        callback: Нажатие «Забронировать за N ₽».
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        bot: Бот для уведомления владельца и канала.
        channel_config: Канал и часовой пояс.
    """
    slot_id = callback.data.removeprefix(keyboards.BOOK_PREFIX)
    async with session_factory() as db_session:
        user = await users.find_user(db_session, callback.from_user.id)
    await callback.answer()
    if user is None or not slot_id.isdigit():
        await callback.message.answer(_START_FIRST_TEXT)
        return
    await callback.message.edit_reply_markup(reply_markup=None)
    if user.phone is None:
        await state.set_state(states.BookingStates.phone)
        await state.update_data({_SLOT_ID_KEY: int(slot_id)})
        await callback.message.answer(
            _PHONE_REQUEST_TEXT,
            reply_markup=keyboards.build_contact_keyboard(),
        )
        return
    deps = _BookingDeps(session_factory, bot, channel_config)
    await _book(callback.message, deps, user.id, int(slot_id))


@router.message(
    filters.StateFilter(states.BookingStates.phone), aiogram.F.contact
)
async def handle_contact(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Сохраняет телефон покупателя и бронирует слот (B3.2, пункт 2).

    Args:
        message: Контакт, отправленный кнопкой Telegram.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        bot: Бот для уведомления владельца и канала.
        channel_config: Канал и часовой пояс.
    """
    # Принимаем только свой контакт: чужой номер через кнопку не придёт,
    # но его можно переслать вручную.
    if message.contact.user_id != message.from_user.id:
        await message.answer(
            _OWN_CONTACT_TEXT, reply_markup=keyboards.build_contact_keyboard()
        )
        return
    answers = await state.get_data()
    async with session_factory() as db_session:
        user = await users.find_user(db_session, message.from_user.id)
        try:
            await users.set_phone(
                db_session, user.id, message.contact.phone_number
            )
        except ValueError as error:
            await message.answer(
                str(error), reply_markup=keyboards.build_contact_keyboard()
            )
            return
    await state.clear()
    await message.answer(
        _PHONE_SAVED_TEXT, reply_markup=types.ReplyKeyboardRemove()
    )
    deps = _BookingDeps(session_factory, bot, channel_config)
    await _book(message, deps, user.id, answers[_SLOT_ID_KEY])


@router.message(
    filters.StateFilter(states.BookingStates.phone), _ANSWER_FILTER
)
async def handle_phone_text(message: types.Message) -> None:
    """Напоминает, что телефон нужно отправить кнопкой.

    Args:
        message: Текст вместо контакта.
    """
    await message.answer(
        _PHONE_REQUEST_TEXT, reply_markup=keyboards.build_contact_keyboard()
    )


async def _book(
    message: types.Message,
    deps: _BookingDeps,
    user_id: int,
    slot_id: int,
) -> None:
    async with deps.session_factory() as db_session:
        result = await bookings.book_slot(
            db_session, user_id, slot_id, models.utc_now()
        )
    city_tz = deps.channel_config.city_tz
    if result.outcome == bookings.BookOutcome.BOOKED:
        await message.answer(
            _format_buyer_confirmation(result.details, city_tz)
        )
        await _notify_owner(deps.bot, result.details, city_tz)
        await channel.refresh_posts(
            deps.bot, deps.session_factory, deps.channel_config, [slot_id]
        )
        return
    if result.outcome == bookings.BookOutcome.BLOCKED:
        await message.answer(_format_blocked(result.blocked_until, city_tz))
        return
    await message.answer(_OUTCOME_TEXTS[result.outcome])


def _format_blocked(
    blocked_until: datetime.datetime, city_tz: datetime.tzinfo
) -> str:
    # Бессрочная блокировка админа хранится как 2100 год: дату такой
    # блокировки покупателю не показываем (B4.9).
    if bookings.is_permanent_block(blocked_until):
        return _PERMANENT_BLOCKED_TEXT
    blocked_date = blocked_until.astimezone(city_tz)
    return _BLOCKED_TEXT.format(
        date=blocked_date.strftime(_BLOCKED_DATE_FORMAT)
    )


def _format_buyer_confirmation(
    details: bookings.BookingDetails, city_tz: datetime.tzinfo
) -> str:
    # Название, адрес и телефон бизнеса покупатель узнаёт после брони
    # (B3.2, пункт 4; B4.7).
    return "\n".join([
        "Слот забронирован.",
        "",
        f"{details.service_title}, "
        f"{feed.format_slot_time(details.starts_at, city_tz)}",
        details.business_name,
        f"Адрес: {details.business_address}",
        f"Телефон: {details.business_phone}",
        f"Цена: {feed.format_rubles(details.price)}, оплата на месте.",
        "",
        "Отменить можно не позднее чем за час до начала: /my",
    ])


async def _notify_owner(
    bot: aiogram.Bot,
    details: bookings.BookingDetails,
    city_tz: datetime.tzinfo,
) -> None:
    # Телефон покупателя виден владельцу только после брони (B5).
    text = "\n".join([
        "Новая бронь.",
        "",
        f"{details.service_title}, "
        f"{feed.format_slot_time(details.starts_at, city_tz)}",
        f"Клиент: {details.buyer_name}, {details.buyer_phone}",
        f"Цена: {feed.format_rubles(details.price)}",
    ])
    try:
        await bot.send_message(details.owner_tg_id, text)
    except exceptions.TelegramAPIError as error:
        # Телефон в лог не пишем: только ID брони.
        _LOG.warning(
            "Уведомление о брони %s не доставлено владельцу: %s",
            details.booking_id, error,
        )


@router.message(filters.Command("my"))
async def handle_my(
    message: types.Message,
    state: context.FSMContext,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Показывает активные брони с кнопками отмены (B4.5, B8).

    Без согласия отвечает «Броней пока нет» и согласие не запрашивает.

    Args:
        message: Входящее сообщение.
        state: Состояние диалога.
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    await state.clear()
    my_bookings = await _load_my_bookings(
        session_factory, message.from_user.id, city_tz
    )
    await message.answer(my_bookings.text, reply_markup=my_bookings.markup)


@router.callback_query(
    aiogram.F.data.startswith(keyboards.CANCEL_BOOKING_PREFIX)
)
async def handle_cancel_booking(
    callback: types.CallbackQuery,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Спрашивает «Точно отменить бронь?» в том же сообщении /my.

    Args:
        callback: Нажатие «Отменить N».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    booking_id = callback.data.removeprefix(keyboards.CANCEL_BOOKING_PREFIX)
    details = None
    async with session_factory() as db_session:
        user = await users.find_user(db_session, callback.from_user.id)
        if user is not None and booking_id.isdigit():
            details = await bookings.find_buyer_booking(
                db_session, user.id, int(booking_id)
            )
    outcome = _cancel_precheck(details)
    if outcome is not None:
        await callback.answer(_CANCEL_OUTCOME_TEXTS[outcome], show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_text(
        "\n".join([
            _CONFIRM_CANCEL_TEXT,
            "",
            f"{details.service_title}, "
            f"{feed.format_slot_time(details.starts_at, city_tz)}",
            details.business_name,
        ]),
        reply_markup=keyboards.build_confirm_cancel_keyboard(
            details.booking_id
        ),
    )


@router.callback_query(aiogram.F.data == keyboards.KEEP_BOOKING_CALLBACK)
async def handle_keep_booking(
    callback: types.CallbackQuery,
    session_factory: _SessionFactory,
    city_tz: datetime.tzinfo,
) -> None:
    """Оставляет бронь и возвращает список /my.

    Args:
        callback: Нажатие «Нет».
        session_factory: Фабрика сессий базы.
        city_tz: Часовой пояс города.
    """
    await callback.answer()
    my_bookings = await _load_my_bookings(
        session_factory, callback.from_user.id, city_tz
    )
    await callback.message.edit_text(
        my_bookings.text, reply_markup=my_bookings.markup
    )


@router.callback_query(
    aiogram.F.data.startswith(keyboards.CONFIRM_CANCEL_PREFIX)
)
async def handle_confirm_cancel(
    callback: types.CallbackQuery,
    session_factory: _SessionFactory,
    bot: aiogram.Bot,
    channel_config: channel.ChannelConfig,
) -> None:
    """Отменяет бронь после подтверждения и возвращает слот в ленту.

    Args:
        callback: Нажатие «Да, отменить».
        session_factory: Фабрика сессий базы.
        bot: Бот для уведомления владельца и канала.
        channel_config: Канал и часовой пояс.
    """
    booking_id = callback.data.removeprefix(keyboards.CONFIRM_CANCEL_PREFIX)
    async with session_factory() as db_session:
        user = await users.find_user(db_session, callback.from_user.id)
        result = bookings.CancelResult(bookings.CancelOutcome.NOT_FOUND)
        if user is not None and booking_id.isdigit():
            result = await bookings.cancel_booking(
                db_session, user.id, int(booking_id), models.utc_now()
            )
    if result.outcome != bookings.CancelOutcome.CANCELLED:
        await callback.answer(
            _CANCEL_OUTCOME_TEXTS[result.outcome], show_alert=True
        )
        return
    await callback.answer(_CANCELLED_TEXT)
    city_tz = channel_config.city_tz
    my_bookings = await _load_my_bookings(
        session_factory, callback.from_user.id, city_tz
    )
    await callback.message.edit_text(
        my_bookings.text, reply_markup=my_bookings.markup
    )
    await _notify_owner_cancelled(bot, result.details, city_tz)
    # Слот снова открыт: в посте возвращаются кнопка и текущая цена.
    await channel.refresh_posts(
        bot, session_factory, channel_config, [result.details.slot_id]
    )


def _cancel_precheck(
    details: bookings.BookingDetails | None,
) -> bookings.CancelOutcome | None:
    # Ранняя проверка, чтобы не спрашивать «Точно?» зря. Окончательная
    # стоит в cancel_booking.
    if details is None:
        return bookings.CancelOutcome.NOT_FOUND
    if not bookings.can_cancel(details.starts_at, models.utc_now()):
        return bookings.CancelOutcome.TOO_LATE
    return None


def build_my_text(
    my_bookings: collections.abc.Sequence[bookings.BookingDetails],
    now: datetime.datetime,
    city_tz: datetime.tzinfo,
) -> str:
    """Собирает текст /my: брони с адресом и телефоном бизнеса.

    Args:
        my_bookings: Активные брони покупателя.
        now: Текущее время: по нему видно, можно ли ещё отменить.
        city_tz: Часовой пояс города.

    Returns:
        Обычный текст без разметки.
    """
    if not my_bookings:
        return _NO_BOOKINGS_TEXT
    items = []
    for number, details in enumerate(my_bookings, start=1):
        lines = [
            f"{number}. {details.service_title}, "
            f"{feed.format_slot_time(details.starts_at, city_tz)}",
            f"{details.business_name}, {details.business_address}",
            f"Телефон: {details.business_phone}",
            f"Цена: {feed.format_rubles(details.price)}, оплата на месте.",
        ]
        if not bookings.can_cancel(details.starts_at, now):
            lines.append(_TOO_LATE_NOTE)
        items.append("\n".join(lines))
    return "\n\n".join([_MY_HEADER_TEXT, *items, _MY_FOOTER_TEXT])


async def _load_my_bookings(
    session_factory: _SessionFactory,
    tg_id: int,
    city_tz: datetime.tzinfo,
) -> _MyBookings:
    now = models.utc_now()
    async with session_factory() as db_session:
        user = await users.find_user(db_session, tg_id)
        my_bookings = [] if user is None else (
            await bookings.list_buyer_bookings(db_session, user.id, now)
        )
    cancellable = {
        number: details.booking_id
        for number, details in enumerate(my_bookings, start=1)
        if bookings.can_cancel(details.starts_at, now)
    }
    return _MyBookings(
        text=build_my_text(my_bookings, now, city_tz),
        markup=keyboards.build_my_bookings_keyboard(cancellable),
    )


async def _notify_owner_cancelled(
    bot: aiogram.Bot,
    details: bookings.BookingDetails,
    city_tz: datetime.tzinfo,
) -> None:
    text = "\n".join([
        "Клиент отменил бронь.",
        "",
        f"{details.service_title}, "
        f"{feed.format_slot_time(details.starts_at, city_tz)}",
        f"Клиент: {details.buyer_name}",
        "Слот снова в ленте.",
    ])
    try:
        await bot.send_message(details.owner_tg_id, text)
    except exceptions.TelegramAPIError as error:
        _LOG.warning(
            "Уведомление об отмене брони %s не доставлено: %s",
            details.booking_id, error,
        )

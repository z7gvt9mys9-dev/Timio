"""Клавиатуры бота."""

import collections.abc
import datetime

from aiogram import types

from core import businesses
from core import feed
from core import slots
from db import models

SKIP_TEXT = "Пропустить"
CONSENT_CALLBACK = "consent:accept"
ROLE_OWNER_CALLBACK = "role:owner"
ROLE_BUYER_CALLBACK = "role:buyer"
CATEGORY_PREFIX = "category:"
SERVICE_ADD_CALLBACK = "service:add"
SERVICE_HIDE_PREFIX = "service:hide:"
SERVICE_SHOW_PREFIX = "service:show:"
SERVICE_EDIT_PREFIX = "service:edit:"
# Формат: service:field:<id услуги>:<поле>.
SERVICE_FIELD_PREFIX = "service:field:"
SERVICE_FIELD_SEPARATOR = ":"
BUSINESS_APPROVE_PREFIX = "business:approve:"
NEW_SLOT_SERVICE_PREFIX = "newslot:service:"
NEW_SLOT_DAY_PREFIX = "newslot:day:"
NEW_SLOT_HIDE_PREFIX = "newslot:hide:"
NEW_SLOT_PUBLISH_CALLBACK = "newslot:publish"
NEW_SLOT_RESTART_CALLBACK = "newslot:restart"
NEW_SLOT_REPEAT_CALLBACK = "newslot:repeat"
SLOTS_LIST_CALLBACK = "slots:list"
# Формат: slots:cat:<категория>; пустая категория — все.
SLOTS_CATEGORY_PREFIX = "slots:cat:"
BOOK_PREFIX = "book:"
CANCEL_BOOKING_PREFIX = "cancel:"
CONFIRM_CANCEL_PREFIX = "cancelok:"
KEEP_BOOKING_CALLBACK = "cancelno"
SEND_PHONE_TEXT = "Отправить телефон"
SLOT_WITHDRAW_PREFIX = "slot:withdraw:"
# Снятие слота с бронью: сначала подтверждение (B9).
SLOT_WITHDRAW_BOOKED_PREFIX = "slot:withdrawbooked:"
SLOT_WITHDRAW_CONFIRM_PREFIX = "slot:withdrawok:"
SLOT_WITHDRAW_KEEP_CALLBACK = "slot:withdrawno"
# Ответ владельца «Клиент пришёл?» (B3.1, пункт 8).
VISIT_CAME_PREFIX = "visit:came:"
VISIT_NO_SHOW_PREFIX = "visit:noshow:"
HIDE_NAME_YES = "1"
HIDE_NAME_NO = "0"
# Кнопки дня передают смещение от сегодня, а не дату: дата считается
# в момент ответа, поэтому «Сегодня» не устаревает после полуночи.
DAY_OFFSETS = {0: "Сегодня", 1: "Завтра", 2: "Послезавтра"}
_SLOT_TIME_FORMAT = "%d.%m %H:%M"
_ALL_CATEGORIES_TEXT = "Все"
_CATEGORY_BUTTONS_PER_ROW = 3
BUSINESS_REJECT_PREFIX = "business:reject:"


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


def build_category_keyboard() -> types.InlineKeyboardMarkup:
    """Создаёт кнопки категорий бизнеса (B4.2).

    Returns:
        По одной кнопке на категорию из списка.
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=category, callback_data=CATEGORY_PREFIX + category
                )
            ]
            for category in businesses.CATEGORIES
        ]
    )


def build_business_keyboard(
    services: collections.abc.Sequence[models.Service],
) -> types.InlineKeyboardMarkup:
    """Создаёт кнопки карточки бизнеса.

    Args:
        services: Услуги бизнеса, включая скрытые.

    Returns:
        «Добавить услугу», «Слоты», по строке на услугу: «Изменить» и
        «Скрыть» или «Показать».
    """
    rows = [
        [
            types.InlineKeyboardButton(
                text="Добавить услугу", callback_data=SERVICE_ADD_CALLBACK
            ),
            types.InlineKeyboardButton(
                text="Слоты", callback_data=SLOTS_LIST_CALLBACK
            ),
        ]
    ]
    for service in services:
        if service.is_active:
            toggle_text, toggle_prefix = "Скрыть", SERVICE_HIDE_PREFIX
        else:
            toggle_text, toggle_prefix = "Показать", SERVICE_SHOW_PREFIX
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=f"Изменить: {service.title}",
                    callback_data=f"{SERVICE_EDIT_PREFIX}{service.id}",
                ),
                types.InlineKeyboardButton(
                    text=toggle_text,
                    callback_data=f"{toggle_prefix}{service.id}",
                ),
            ]
        )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def build_service_fields_keyboard(
    service_id: int,
) -> types.InlineKeyboardMarkup:
    """Создаёт кнопки выбора поля для правки услуги (B4.2).

    Args:
        service_id: ID услуги.

    Returns:
        Кнопки «Название», «Длительность», «Цена».
    """
    labels = {
        businesses.ServiceField.TITLE: "Название",
        businesses.ServiceField.DURATION_MIN: "Длительность",
        businesses.ServiceField.REGULAR_PRICE: "Цена",
    }
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=label,
                    callback_data=(
                        f"{SERVICE_FIELD_PREFIX}{service_id}"
                        f"{SERVICE_FIELD_SEPARATOR}{field.value}"
                    ),
                )
                for field, label in labels.items()
            ]
        ]
    )


def build_application_keyboard(
    business_id: int,
) -> types.InlineKeyboardMarkup:
    """Создаёт кнопки решения по заявке бизнеса (B4.9).

    Args:
        business_id: ID бизнеса.

    Returns:
        Кнопки «Одобрить» и «Отклонить».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Одобрить",
                    callback_data=f"{BUSINESS_APPROVE_PREFIX}{business_id}",
                ),
                types.InlineKeyboardButton(
                    text="Отклонить",
                    callback_data=f"{BUSINESS_REJECT_PREFIX}{business_id}",
                ),
            ]
        ]
    )


def build_new_slot_services_keyboard(
    services: collections.abc.Sequence[models.Service],
) -> types.InlineKeyboardMarkup:
    """Создаёт выбор услуги для нового слота и «Повторить вчерашние».

    Args:
        services: Активные услуги бизнеса.

    Returns:
        По кнопке на услугу и кнопка «Повторить вчерашние слоты».
    """
    rows = [
        [
            types.InlineKeyboardButton(
                text=service.title,
                callback_data=f"{NEW_SLOT_SERVICE_PREFIX}{service.id}",
            )
        ]
        for service in services
    ]
    rows.append(
        [
            types.InlineKeyboardButton(
                text="Повторить вчерашние слоты",
                callback_data=NEW_SLOT_REPEAT_CALLBACK,
            )
        ]
    )
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def build_day_keyboard() -> types.InlineKeyboardMarkup:
    """Создаёт выбор дня слота: сегодня, завтра, послезавтра.

    Returns:
        Три кнопки в ряд.
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=label, callback_data=f"{NEW_SLOT_DAY_PREFIX}{offset}"
                )
                for offset, label in DAY_OFFSETS.items()
            ]
        ]
    )


def build_hide_name_keyboard() -> types.InlineKeyboardMarkup:
    """Создаёт выбор режима скрытого названия (B4.6).

    Returns:
        Кнопки «Да, скрыть» и «Нет».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Да, скрыть",
                    callback_data=NEW_SLOT_HIDE_PREFIX + HIDE_NAME_YES,
                ),
                types.InlineKeyboardButton(
                    text="Нет",
                    callback_data=NEW_SLOT_HIDE_PREFIX + HIDE_NAME_NO,
                ),
            ]
        ]
    )


def build_publish_keyboard() -> types.InlineKeyboardMarkup:
    """Создаёт кнопки подтверждения нового слота.

    Returns:
        Кнопки «Опубликовать» и «Заново».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Опубликовать",
                    callback_data=NEW_SLOT_PUBLISH_CALLBACK,
                ),
                types.InlineKeyboardButton(
                    text="Заново", callback_data=NEW_SLOT_RESTART_CALLBACK
                ),
            ]
        ]
    )


def build_owner_slots_keyboard(
    owner_slots: collections.abc.Sequence[slots.OwnerSlot],
    city_tz: datetime.tzinfo,
) -> types.InlineKeyboardMarkup | None:
    """Создаёт кнопки «Снять» для слотов владельца (B4.3, B9).

    Открытый слот снимается сразу, слот с бронью — после подтверждения.

    Args:
        owner_slots: Ближайшие слоты владельца.
        city_tz: Часовой пояс города для подписи кнопки.

    Returns:
        По кнопке на слот или None, если слотов нет.
    """
    rows = []
    for item in owner_slots:
        label = "Снять " + item.slot.starts_at.astimezone(city_tz).strftime(
            _SLOT_TIME_FORMAT
        )
        if item.slot.status == models.SlotStatus.BOOKED:
            label += " (есть бронь)"
            prefix = SLOT_WITHDRAW_BOOKED_PREFIX
        else:
            prefix = SLOT_WITHDRAW_PREFIX
        rows.append(
            [
                types.InlineKeyboardButton(
                    text=label, callback_data=f"{prefix}{item.slot.id}"
                )
            ]
        )
    if not rows:
        return None
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def build_slot_post_keyboard(booking_url: str) -> types.InlineKeyboardMarkup:
    """Создаёт кнопку «Забронировать» под постом слота (B4.6).

    Args:
        booking_url: Deep link на бота: t.me/<бот>?start=slot_<id>.

    Returns:
        Клавиатура с одной кнопкой-ссылкой.
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [types.InlineKeyboardButton(text="Забронировать", url=booking_url)]
        ]
    )


def build_slots_list_keyboard(
    booking_urls: collections.abc.Sequence[str],
    categories: collections.abc.Sequence[str],
) -> types.InlineKeyboardMarkup | None:
    """Создаёт кнопки списка /slots: бронь и фильтр по категории (B4.6).

    Args:
        booking_urls: Deep link на бронь, по порядку номеров в списке.
        categories: Категории, по которым есть открытые слоты.

    Returns:
        Кнопки «1. Забронировать», ... и ряды категорий с «Все».
        None, если нет ни слотов, ни категорий.
    """
    rows = [
        [
            types.InlineKeyboardButton(
                text=f"{number}. Забронировать", url=booking_url
            )
        ]
        for number, booking_url in enumerate(booking_urls, start=1)
    ]
    category_buttons = [
        types.InlineKeyboardButton(
            text=label, callback_data=SLOTS_CATEGORY_PREFIX + value
        )
        for label, value in [(_ALL_CATEGORIES_TEXT, "")]
        + [(category, category) for category in categories]
    ]
    if categories:
        rows.extend(
            category_buttons[start:start + _CATEGORY_BUTTONS_PER_ROW]
            for start in range(
                0, len(category_buttons), _CATEGORY_BUTTONS_PER_ROW
            )
        )
    if not rows:
        return None
    return types.InlineKeyboardMarkup(inline_keyboard=rows)


def build_book_keyboard(
    slot_id: int, price: int
) -> types.InlineKeyboardMarkup:
    """Создаёт кнопку брони на карточке слота (B3.2, пункт 3).

    Args:
        slot_id: ID слота.
        price: Цена сейчас, ₽.

    Returns:
        Кнопка «Забронировать за N ₽».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=f"Забронировать за {feed.format_rubles(price)}",
                    callback_data=f"{BOOK_PREFIX}{slot_id}",
                )
            ]
        ]
    )


def build_contact_keyboard() -> types.ReplyKeyboardMarkup:
    """Создаёт кнопку Telegram «Отправить телефон» (B3.2, пункт 2).

    Returns:
        Клавиатура, которая отправляет контакт пользователя.
    """
    return types.ReplyKeyboardMarkup(
        keyboard=[
            [types.KeyboardButton(text=SEND_PHONE_TEXT, request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def build_my_bookings_keyboard(
    cancellable: collections.abc.Mapping[int, int],
) -> types.InlineKeyboardMarkup | None:
    """Создаёт кнопки «Отменить N» для броней в /my (B4.5).

    Args:
        cancellable: Номер брони в списке → ID брони, только те,
            которые ещё можно отменить.

    Returns:
        По кнопке на бронь или None, если отменять нечего.
    """
    if not cancellable:
        return None
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text=f"Отменить {number}",
                    callback_data=f"{CANCEL_BOOKING_PREFIX}{booking_id}",
                )
            ]
            for number, booking_id in cancellable.items()
        ]
    )


def build_confirm_cancel_keyboard(
    booking_id: int,
) -> types.InlineKeyboardMarkup:
    """Создаёт кнопки подтверждения отмены брони покупателем.

    Args:
        booking_id: ID брони.

    Returns:
        Кнопки «Да, отменить» и «Нет».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Да, отменить",
                    callback_data=f"{CONFIRM_CANCEL_PREFIX}{booking_id}",
                ),
                types.InlineKeyboardButton(
                    text="Нет", callback_data=KEEP_BOOKING_CALLBACK
                ),
            ]
        ]
    )


def build_confirm_withdraw_keyboard(
    slot_id: int,
) -> types.InlineKeyboardMarkup:
    """Создаёт кнопки подтверждения снятия слота с бронью (B9).

    Args:
        slot_id: ID слота.

    Returns:
        Кнопки «Да, снять» и «Нет».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Да, снять",
                    callback_data=f"{SLOT_WITHDRAW_CONFIRM_PREFIX}{slot_id}",
                ),
                types.InlineKeyboardButton(
                    text="Нет", callback_data=SLOT_WITHDRAW_KEEP_CALLBACK
                ),
            ]
        ]
    )


def build_visit_keyboard(booking_id: int) -> types.InlineKeyboardMarkup:
    """Создаёт кнопки ответа на «Клиент пришёл?» (B3.1, пункт 8).

    Args:
        booking_id: ID брони.

    Returns:
        Кнопки «Пришёл» и «Не пришёл».
    """
    return types.InlineKeyboardMarkup(
        inline_keyboard=[
            [
                types.InlineKeyboardButton(
                    text="Пришёл",
                    callback_data=f"{VISIT_CAME_PREFIX}{booking_id}",
                ),
                types.InlineKeyboardButton(
                    text="Не пришёл",
                    callback_data=f"{VISIT_NO_SHOW_PREFIX}{booking_id}",
                ),
            ]
        ]
    )

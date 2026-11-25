"""Общее для сценариев владельца: шаги диалога, карточка, формат."""

import collections.abc
import dataclasses
import datetime
import functools

import aiogram
from aiogram import types
from aiogram.fsm import context
from aiogram.fsm import state as fsm_state
from sqlalchemy.ext import asyncio as sa_asyncio

from bot import keyboards
from bot.handlers import admin
from core import businesses
from core import feed
from core import users
from db import models

SessionFactory = sa_asyncio.async_sessionmaker[sa_asyncio.AsyncSession]


@dataclasses.dataclass(frozen=True)
class Step:
    """Вопрос диалога: состояние, ключ ответа и проверка из core."""

    state: fsm_state.State
    key: str
    parse: collections.abc.Callable[[str], str | int]
    question: str
    keyboard: types.InlineKeyboardMarkup | None = None


def text_parser(
    field: businesses.TextField,
) -> collections.abc.Callable[[str], str]:
    """Создаёт проверку текстового поля для шага диалога.

    Args:
        field: Какое поле проверяется.

    Returns:
        Функция, которая принимает текст и возвращает его или бросает
        ValueError с примером.
    """
    return functools.partial(businesses.parse_text_field, field)


# Ключ в данных диалога: ID владельца, для которого идёт диалог.
USER_ID_KEY = "user_id"
# Ответом считается любой текст, кроме команд: /start, /calc и другие
# команды во время диалога обрабатываются своими обработчиками.
ANSWER_FILTER = aiogram.F.text & ~aiogram.F.text.startswith("/")
# Ключ в данных диалога: услуга при её правке и при создании слота.
SERVICE_ID_KEY = "service_id"
NO_BUSINESS_TEXT = "Сначала зарегистрируйте бизнес: /business"
SERVICE_NOT_FOUND_TEXT = "Услуга не найдена."
_STATUS_LABELS = {
    models.BusinessStatus.PENDING: "на проверке",
    models.BusinessStatus.APPROVED: "одобрен",
    models.BusinessStatus.BLOCKED: "заблокирован",
}
_PENDING_HINT_TEXT = "Слоты можно будет выставлять после одобрения заявки."
_SLOT_TIME_FORMAT = "%d.%m %H:%M"


@dataclasses.dataclass(frozen=True)
class OwnedService:
    """Услуга вместе с ID владельца, который её запросил."""

    owner_id: int
    service: models.Service


async def start_steps(
    message: types.Message,
    state: context.FSMContext,
    steps: collections.abc.Sequence[Step],
    initial_data: dict[str, int],
    intro_text: str = "",
) -> None:
    """Сбрасывает диалог и задаёт первый вопрос из списка шагов.

    Args:
        message: Сообщение, в чат которого задаётся вопрос.
        state: Состояние диалога.
        steps: Шаги диалога по порядку.
        initial_data: Данные, которые нужны в конце диалога.
        intro_text: Текст перед первым вопросом.
    """
    await state.clear()
    first_step = steps[0]
    await state.set_state(first_step.state)
    await state.update_data(initial_data)
    await message.answer(
        intro_text + first_step.question, reply_markup=first_step.keyboard
    )


async def accept_answer(
    message: types.Message,
    state: context.FSMContext,
    steps: collections.abc.Sequence[Step],
    raw_text: str,
) -> dict[str, str | int] | None:
    """Проверяет ответ на текущий шаг и задаёт следующий вопрос.

    Args:
        message: Сообщение, в чат которого отвечает бот.
        state: Состояние диалога.
        steps: Шаги диалога по порядку.
        raw_text: Ответ пользователя.

    Returns:
        Все ответы после последнего шага. None, если ответ неверный
        и вопрос повторён или если задан следующий вопрос.
    """
    current_state = await state.get_state()
    index = next(
        position for position, step in enumerate(steps)
        if step.state.state == current_state
    )
    step = steps[index]
    try:
        value = step.parse(raw_text)
    except ValueError as error:
        await message.answer(
            f"{error}\n\n{step.question}", reply_markup=step.keyboard
        )
        return None
    await state.update_data({step.key: value})
    if index + 1 < len(steps):
        next_step = steps[index + 1]
        await state.set_state(next_step.state)
        await message.answer(
            next_step.question, reply_markup=next_step.keyboard
        )
        return None
    answers = await state.get_data()
    await state.clear()
    return answers


async def find_business_by_tg_id(
    session_factory: SessionFactory, tg_id: int
) -> models.Business | None:
    """Ищет бизнес пользователя Telegram.

    Args:
        session_factory: Фабрика сессий базы.
        tg_id: Telegram ID.

    Returns:
        Бизнес или None, если нет пользователя или бизнеса.
    """
    async with session_factory() as db_session:
        user = await users.find_user(db_session, tg_id)
        if user is None:
            return None
        return await businesses.find_owner_business(db_session, user.id)


async def find_owned_service(
    session_factory: SessionFactory, tg_id: int, service_id: str
) -> OwnedService | None:
    """Ищет услугу, если она принадлежит бизнесу этого пользователя.

    ID приходит из данных кнопки, поэтому проверяются и формат,
    и владелец.

    Args:
        session_factory: Фабрика сессий базы.
        tg_id: Telegram ID того, кто нажал кнопку.
        service_id: ID услуги текстом из данных кнопки.

    Returns:
        Услуга с ID владельца или None.
    """
    if not service_id.isdigit():
        return None
    business = await find_business_by_tg_id(session_factory, tg_id)
    if business is None:
        return None
    async with session_factory() as db_session:
        service = await businesses.find_owner_service(
            db_session, business.owner_id, int(service_id)
        )
    if service is None:
        return None
    return OwnedService(owner_id=business.owner_id, service=service)


async def send_card(
    message: types.Message,
    session_factory: SessionFactory,
    owner_id: int,
    is_edit: bool = False,
) -> bool:
    """Отправляет или обновляет карточку бизнеса с услугами.

    Args:
        message: Сообщение, в чат которого отвечает бот.
        session_factory: Фабрика сессий базы.
        owner_id: ID владельца.
        is_edit: True — изменить это сообщение, False — отправить новое.

    Returns:
        False, если у владельца ещё нет бизнеса.
    """
    async with session_factory() as db_session:
        business = await businesses.find_owner_business(db_session, owner_id)
        if business is None:
            return False
        services = await businesses.list_services(db_session, business.id)
    text = _format_card(business, services)
    markup = keyboards.build_business_keyboard(services)
    if is_edit:
        await message.edit_text(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)
    return True


def _format_card(
    business: models.Business,
    services: collections.abc.Sequence[models.Service],
) -> str:
    lines = [
        admin.format_business_details(business),
        f"Статус: {_STATUS_LABELS[business.status]}",
        "",
        "Услуги:",
    ]
    for service in services:
        hidden_mark = "" if service.is_active else " (скрыта)"
        lines.append(
            f"- {service.title} — {service.duration_min} мин, "
            f"{feed.format_rubles(service.regular_price)}{hidden_mark}"
        )
    if not services:
        lines.append("Пока нет. Добавьте первую кнопкой ниже.")
    if business.status == models.BusinessStatus.PENDING:
        lines.extend(["", _PENDING_HINT_TEXT])
    return "\n".join(lines)


def format_local(
    moment: datetime.datetime, city_tz: datetime.tzinfo
) -> str:
    """Показывает время по часам города: «26.11 15:00».

    Args:
        moment: Время, tz-aware.
        city_tz: Часовой пояс города.

    Returns:
        День, месяц и время.
    """
    return moment.astimezone(city_tz).strftime(_SLOT_TIME_FORMAT)

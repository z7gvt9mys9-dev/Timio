"""Общие фикстуры тестов: тестовый бот и сеть Telegram без запросов."""

import collections.abc
import dataclasses
import datetime

import aiogram
import pytest
from aiogram import exceptions
from aiogram import methods
from aiogram import types
from aiogram.client.session import base

from bot.handlers import admin
from bot.handlers import buyer
from bot.handlers import common
from bot.handlers import owner

FAKE_BOT_USERNAME = "slot_bot"


@dataclasses.dataclass
class SentMessage:
    """Отправленное ботом сообщение."""

    chat_id: int
    text: str
    reply_markup: types.InlineKeyboardMarkup | None = None


@dataclasses.dataclass
class EditedMessage:
    """Изменённое ботом сообщение."""

    chat_id: int
    message_id: int
    text: str
    reply_markup: types.InlineKeyboardMarkup | None = None


@dataclasses.dataclass(frozen=True)
class _SendResult:
    message_id: int


class FakeBot:
    """Записывает отправки и правки вместо запросов к Telegram.

    Args:
        blocked_chat_ids: Чаты, отправка в которые падает, как будто
            пользователь заблокировал бота или канал недоступен.
        edit_error: Ошибка, которую бросает каждая правка.
        network_error_chat_ids: Чаты, отправка в которые падает
            с сетевой ошибкой: её стоит повторить позже.
    """

    def __init__(
        self,
        blocked_chat_ids: frozenset[int] = frozenset(),
        edit_error: exceptions.TelegramAPIError | None = None,
        network_error_chat_ids: frozenset[int] = frozenset(),
    ) -> None:
        self.sent: list[SentMessage] = []
        self.edited: list[EditedMessage] = []
        self._blocked_chat_ids = blocked_chat_ids
        self._edit_error = edit_error
        self._network_error_chat_ids = network_error_chat_ids

    async def me(self) -> types.User:
        """Возвращает пользователя бота с username для deep link."""
        return types.User(
            id=1, is_bot=True, first_name="SlotMarket",
            username=FAKE_BOT_USERNAME,
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: types.InlineKeyboardMarkup | None = None,
    ) -> _SendResult:
        """Записывает сообщение или падает для заданных чатов."""
        if chat_id in self._network_error_chat_ids:
            raise exceptions.TelegramNetworkError(
                method=methods.SendMessage(chat_id=chat_id, text=text),
                message="Request timeout error",
            )
        if chat_id in self._blocked_chat_ids:
            raise exceptions.TelegramForbiddenError(
                method=methods.SendMessage(chat_id=chat_id, text=text),
                message="Forbidden: bot was blocked by the user",
            )
        self.sent.append(SentMessage(chat_id, text, reply_markup))
        return _SendResult(message_id=len(self.sent))

    async def edit_message_text(
        self,
        text: str,
        chat_id: int,
        message_id: int,
        parse_mode: str | None = None,
        reply_markup: types.InlineKeyboardMarkup | None = None,
    ) -> bool:
        """Записывает правку или бросает заданную ошибку."""
        if self._edit_error is not None:
            raise self._edit_error
        self.edited.append(
            EditedMessage(chat_id, message_id, text, reply_markup)
        )
        return True


@pytest.fixture
def make_fake_bot() -> collections.abc.Callable[..., FakeBot]:
    """Возвращает конструктор тестового бота."""
    return FakeBot


class FakeTelegramSession(base.BaseSession):
    """Сеть Telegram для настоящего aiogram.Bot: записывает запросы."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[methods.TelegramMethod] = []

    async def make_request(
        self,
        bot: aiogram.Bot,
        method: methods.TelegramMethod,
        timeout: int | None = None,
    ) -> object:
        """Записывает запрос и отвечает так, как ответил бы Telegram."""
        self.requests.append(method)
        if isinstance(method, methods.GetMe):
            return types.User(
                id=1, is_bot=True, first_name="SlotMarket",
                username=FAKE_BOT_USERNAME,
            )
        if isinstance(method, methods.SendMessage):
            return types.Message(
                message_id=len(self.requests),
                date=datetime.datetime.now(datetime.UTC),
                chat=types.Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        return True

    async def stream_content(
        self, *args: object, **kwargs: object
    ) -> collections.abc.AsyncIterator[bytes]:
        """Файлы в тестах не скачиваются."""
        yield b""

    async def close(self) -> None:
        """Закрывать нечего."""

    def sent_to(self, chat_id: int) -> list[methods.SendMessage]:
        """Возвращает сообщения, отправленные в этот чат."""
        return [
            request for request in self.requests
            if isinstance(request, methods.SendMessage)
            and request.chat_id == chat_id
        ]


@pytest.fixture(scope="session")
def dispatcher() -> aiogram.Dispatcher:
    """Один Dispatcher со всеми роутерами бота, как в main.py.

    Роутер можно подключить только к одному Dispatcher, поэтому он
    общий на все тесты. Настройки передаются в feed_update.
    """
    session_dispatcher = aiogram.Dispatcher()
    session_dispatcher.include_routers(
        common.router, owner.router, admin.router, buyer.router,
        common.fallback_router,
    )
    return session_dispatcher

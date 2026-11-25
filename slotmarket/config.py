"""Настройки приложения из файла .env."""

import dataclasses
import os
import pathlib
import zoneinfo

import dotenv

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = "slotmarket.db"
_ADMIN_IDS_SEPARATOR = ","


class ConfigError(Exception):
    """Ошибка в настройках .env."""


@dataclasses.dataclass(frozen=True)
class Config:
    """Настройки приложения.

    Attributes:
        bot_token: Токен Telegram-бота. Не попадает в repr, чтобы не
            утечь в лог.
        admin_ids: Telegram ID администраторов.
        channel_id: ID канала с лентой слотов.
        city_tz: Часовой пояс города, в нём время показывается людям.
        db_path: Путь к файлу SQLite.
    """

    bot_token: str = dataclasses.field(repr=False)
    admin_ids: frozenset[int]
    channel_id: int
    city_tz: zoneinfo.ZoneInfo
    db_path: pathlib.Path


def load_config() -> Config:
    """Читает .env из корня проекта и собирает настройки.

    Returns:
        Готовые настройки приложения.

    Raises:
        ConfigError: Если обязательной переменной нет или она неверна.
    """
    dotenv.load_dotenv(PROJECT_ROOT / ".env")
    return Config(
        bot_token=_require("BOT_TOKEN"),
        admin_ids=_parse_admin_ids(_require("ADMIN_IDS")),
        channel_id=_parse_int("CHANNEL_ID", _require("CHANNEL_ID")),
        city_tz=_parse_timezone(_require("CITY_TZ")),
        db_path=_resolve_db_path(os.getenv("DB_PATH", _DEFAULT_DB_PATH)),
    )


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(f"В .env не задана переменная {name}")
    return value


def _parse_int(name: str, value: str) -> int:
    try:
        return int(value)
    except ValueError as error:
        raise ConfigError(
            f"{name} должно быть целым числом, например -1001234567890"
        ) from error


def _parse_admin_ids(value: str) -> frozenset[int]:
    parts = [part.strip() for part in value.split(_ADMIN_IDS_SEPARATOR)]
    return frozenset(_parse_int("ADMIN_IDS", part) for part in parts if part)


def _parse_timezone(value: str) -> zoneinfo.ZoneInfo:
    try:
        return zoneinfo.ZoneInfo(value)
    except zoneinfo.ZoneInfoNotFoundError as error:
        raise ConfigError(
            f"Неизвестный часовой пояс CITY_TZ={value}, "
            "пример: Europe/Moscow"
        ) from error


def _resolve_db_path(value: str) -> pathlib.Path:
    # Относительный путь считаем от корня проекта, а не от текущей папки,
    # чтобы бот и тесты всегда находили один и тот же файл.
    path = pathlib.Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path

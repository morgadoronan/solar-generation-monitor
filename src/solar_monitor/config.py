"""Environment-only application configuration with secret-safe values."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


@dataclass(frozen=True, slots=True, repr=False)
class Secret:
    """A deliberately non-printing secret value."""

    _value: str

    def __post_init__(self) -> None:
        if not self._value:
            raise ConfigError("secret values must not be empty")

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('********')"

    def __str__(self) -> str:
        return "********"


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    username: str
    password: Secret
    plant_id: str


@dataclass(frozen=True, slots=True)
class LocationConfig:
    latitude: float
    longitude: float
    timezone: ZoneInfo


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: Secret
    chat_id: str


@dataclass(frozen=True, slots=True)
class AppConfig:
    generation: GenerationConfig
    location: LocationConfig
    telegram: TelegramConfig | None
    live_monitoring_enabled: bool
    baseline_day: date = date(2026, 6, 7)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AppConfig:
        values = os.environ if env is None else env
        generation = GenerationConfig(
            username=_required(values, "RENOVIGI_USERNAME"),
            password=Secret(_required(values, "RENOVIGI_PASSWORD")),
            plant_id=_required(values, "RENOVIGI_PLANT_ID"),
        )
        location = LocationConfig(
            latitude=_coordinate(values, "SOLAR_LATITUDE", -90, 90),
            longitude=_coordinate(values, "SOLAR_LONGITUDE", -180, 180),
            timezone=_timezone(values),
        )
        telegram = _telegram(values)
        enabled = _boolean(values, "ENABLE_LIVE_MONITORING", default=False)
        baseline_day = _date(
            values,
            "SOLAR_BASELINE_DATE",
            date(2026, 6, 7),
            alias="SOLAR_BASELINE_DAY",
        )
        if enabled and telegram is None:
            raise ConfigError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required "
                "when live monitoring is enabled"
            )
        return cls(
            generation=generation,
            location=location,
            telegram=telegram,
            live_monitoring_enabled=enabled,
            baseline_day=baseline_day,
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _coordinate(env: Mapping[str, str], name: str, minimum: float, maximum: float) -> float:
    raw = _required(env, name)
    try:
        value = float(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be a number") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _timezone(env: Mapping[str, str]) -> ZoneInfo:
    name = _required(env, "SOLAR_TIMEZONE")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ConfigError("SOLAR_TIMEZONE must be a valid IANA timezone") from error


def _telegram(env: Mapping[str, str]) -> TelegramConfig | None:
    token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
    if bool(token) != bool(chat_id):
        raise ConfigError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set together")
    if not token:
        return None
    return TelegramConfig(bot_token=Secret(token), chat_id=chat_id)


def _boolean(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ConfigError(f"{name} must be either true or false")


def _date(env: Mapping[str, str], name: str, default: date, *, alias: str | None = None) -> date:
    raw = env.get(name, env.get(alias, "") if alias is not None else "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an ISO calendar date") from error

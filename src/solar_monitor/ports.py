"""Provider boundaries for domain and orchestration code."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from solar_monitor.models import (
    DeviceStatus,
    GenerationDay,
    Notification,
    WeatherDay,
    WeatherSnapshot,
)


class ProviderError(RuntimeError):
    """A sanitized external-provider failure safe to expose to orchestration."""


class InvalidCredentialsError(ProviderError):
    """Credentials were rejected and must not be retried automatically."""


class SessionExpiredError(ProviderError):
    """The provider explicitly reported an expired authenticated session."""


class MalformedProviderResponseError(ProviderError):
    """The provider returned a response that could not be validated."""


class GenerationProvider(Protocol):
    def get_generation_day(self, day: date) -> GenerationDay:
        """Return power samples and authoritative energy, when available."""
        ...

    def get_device_status(self, observed_at: datetime) -> DeviceStatus:
        """Return connectivity state for the configured plant."""
        ...


class WeatherProvider(Protocol):
    def get_snapshot(self, observed_at: datetime) -> WeatherSnapshot:
        """Return weather evidence nearest the requested instant."""
        ...

    def get_weather_day(self, day: date) -> WeatherDay:
        """Return daily weather aggregates in the configured local timezone."""
        ...


class NotificationProvider(Protocol):
    def send(self, notification: Notification) -> None:
        """Attempt delivery; callers persist deduplication state separately."""
        ...


class StateStore(Protocol):
    """Storage boundary for a versioned, checksummed state payload."""

    def load_latest(self) -> bytes | None:
        """Return the newest eligible payload, or None when none is available."""
        ...

    def save(self, payload: bytes) -> None:
        """Persist one immutable payload for the current successful run."""
        ...

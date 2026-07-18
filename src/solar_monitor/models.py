"""Validated, provider-neutral domain values used by the monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_non_negative(value: float, field: str) -> None:
    if value < 0:
        raise ValueError(f"{field} must be non-negative")


@dataclass(frozen=True, slots=True)
class PowerSample:
    """One provider power reading in kW at an absolute point in time."""

    observed_at: datetime
    power_kw: float

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_non_negative(self.power_kw, "power_kw")


@dataclass(frozen=True, slots=True)
class GenerationDay:
    """A provider's generation record for one local calendar day."""

    day: date
    samples: tuple[PowerSample, ...]
    energy_kwh: float | None
    complete: bool

    def __post_init__(self) -> None:
        if self.energy_kwh is not None:
            _require_non_negative(self.energy_kwh, "energy_kwh")
        timestamps = tuple(sample.observed_at for sample in self.samples)
        if timestamps != tuple(sorted(timestamps)):
            raise ValueError("samples must be ordered by observed_at")
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("samples must have unique observed_at values")

    @property
    def latest_sample(self) -> PowerSample | None:
        return self.samples[-1] if self.samples else None


class DeviceCondition(StrEnum):
    """Provider-neutral inverter/device connectivity state."""

    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DeviceStatus:
    observed_at: datetime
    condition: DeviceCondition
    provider_code: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class WeatherSnapshot:
    """Weather evidence nearest a monitoring observation."""

    observed_at: datetime
    shortwave_radiation_w_m2: float
    precipitation_mm: float
    cloud_cover_percent: float | None = None

    def __post_init__(self) -> None:
        _require_aware(self.observed_at, "observed_at")
        _require_non_negative(self.shortwave_radiation_w_m2, "shortwave_radiation_w_m2")
        _require_non_negative(self.precipitation_mm, "precipitation_mm")
        if self.cloud_cover_percent is not None and not 0 <= self.cloud_cover_percent <= 100:
            raise ValueError("cloud_cover_percent must be between 0 and 100")


@dataclass(frozen=True, slots=True)
class WeatherDay:
    """Weather aggregates for a local calendar day."""

    day: date
    sunrise: datetime
    sunset: datetime
    sunshine_seconds: float
    daylight_seconds: float
    precipitation_mm: float
    shortwave_radiation_mj_m2: float | None
    complete: bool

    def __post_init__(self) -> None:
        _require_aware(self.sunrise, "sunrise")
        _require_aware(self.sunset, "sunset")
        if self.sunset <= self.sunrise:
            raise ValueError("sunset must be after sunrise")
        _require_non_negative(self.sunshine_seconds, "sunshine_seconds")
        _require_non_negative(self.daylight_seconds, "daylight_seconds")
        _require_non_negative(self.precipitation_mm, "precipitation_mm")
        if self.shortwave_radiation_mj_m2 is not None:
            _require_non_negative(self.shortwave_radiation_mj_m2, "shortwave_radiation_mj_m2")
        if self.sunshine_seconds > self.daylight_seconds:
            raise ValueError("sunshine_seconds cannot exceed daylight_seconds")

    @property
    def sunshine_ratio(self) -> float | None:
        if self.daylight_seconds == 0:
            return None
        return self.sunshine_seconds / self.daylight_seconds


class NotificationKind(StrEnum):
    INTERRUPTION = "interruption"
    CLEANING = "cleaning"
    RECOVERY = "recovery"
    MONTHLY_REPORT = "monthly_report"
    PROVIDER_FAILURE = "provider_failure"


@dataclass(frozen=True, slots=True)
class Notification:
    """A logical notification; event_id supports best-effort deduplication."""

    event_id: str
    kind: NotificationKind
    occurred_at: datetime
    title: str
    body: str

    def __post_init__(self) -> None:
        _require_aware(self.occurred_at, "occurred_at")
        if not self.event_id.strip():
            raise ValueError("event_id must not be blank")
        if not self.title.strip():
            raise ValueError("title must not be blank")
        if not self.body.strip():
            raise ValueError("body must not be blank")

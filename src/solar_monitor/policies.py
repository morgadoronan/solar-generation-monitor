"""Pure, immutable decision policies for monitoring and cleaning evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum

from solar_monitor.models import (
    DeviceCondition,
    GenerationDay,
    PowerSample,
    WeatherDay,
    WeatherSnapshot,
)

GENERATION_FRESHNESS = timedelta(minutes=20)
WEATHER_FRESHNESS = timedelta(minutes=90)
INTERRUPTION_SEPARATION = timedelta(minutes=20)


class InterruptionStatus(StrEnum):
    NIGHTTIME = "nighttime"
    OFFLINE = "offline"
    STALE_TELEMETRY = "stale_telemetry"
    UNKNOWN_WEATHER = "unknown_weather"
    STALE_WEATHER = "stale_weather"
    NORMAL = "normal"
    PENDING = "pending"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class InterruptionEvidence:
    first_observed_at: datetime
    last_observed_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.first_observed_at, "first_observed_at")
        _require_aware(self.last_observed_at, "last_observed_at")
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("last_observed_at cannot precede first_observed_at")


@dataclass(frozen=True, slots=True)
class InterruptionDecision:
    status: InterruptionStatus
    evidence: InterruptionEvidence | None

    @property
    def should_alert(self) -> bool:
        return self.status is InterruptionStatus.INTERRUPTED


def evaluate_interruption(
    *,
    evaluated_at: datetime,
    solar_elevation_degrees: float,
    power_sample: PowerSample | None,
    weather: WeatherSnapshot | None,
    device_condition: DeviceCondition,
    evidence: InterruptionEvidence | None = None,
) -> InterruptionDecision:
    """Evaluate one observation, using provider timestamps rather than run counts."""

    _require_aware(evaluated_at, "evaluated_at")
    if solar_elevation_degrees <= 10:
        return InterruptionDecision(InterruptionStatus.NIGHTTIME, None)
    if device_condition is DeviceCondition.OFFLINE:
        return InterruptionDecision(InterruptionStatus.OFFLINE, None)
    if power_sample is None or not _fresh(
        evaluated_at, power_sample.observed_at, GENERATION_FRESHNESS
    ):
        return InterruptionDecision(InterruptionStatus.STALE_TELEMETRY, None)
    if weather is None:
        return InterruptionDecision(InterruptionStatus.UNKNOWN_WEATHER, None)
    if not _fresh(evaluated_at, weather.observed_at, WEATHER_FRESHNESS):
        return InterruptionDecision(InterruptionStatus.STALE_WEATHER, None)
    if weather.shortwave_radiation_w_m2 < 200 or power_sample.power_kw > 0.05:
        return InterruptionDecision(InterruptionStatus.NORMAL, None)

    sample_time = power_sample.observed_at
    if evidence is None or sample_time < evidence.first_observed_at:
        new_evidence = InterruptionEvidence(sample_time, sample_time)
        return InterruptionDecision(InterruptionStatus.PENDING, new_evidence)
    if sample_time == evidence.last_observed_at:
        return InterruptionDecision(InterruptionStatus.PENDING, evidence)
    updated = InterruptionEvidence(evidence.first_observed_at, sample_time)
    if sample_time - evidence.first_observed_at >= INTERRUPTION_SEPARATION:
        return InterruptionDecision(InterruptionStatus.INTERRUPTED, updated)
    return InterruptionDecision(InterruptionStatus.PENDING, updated)


class SunnyDayStatus(StrEnum):
    QUALIFYING = "qualifying"
    NOT_SUNNY = "not_sunny"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class SunnyDayDecision:
    status: SunnyDayStatus
    sunshine_ratio: float | None


def qualify_sunny_day(generation: GenerationDay, weather: WeatherDay) -> SunnyDayDecision:
    if generation.day != weather.day:
        raise ValueError("generation and weather days must match")
    ratio = weather.sunshine_ratio
    if not generation.complete or not weather.complete or ratio is None:
        return SunnyDayDecision(SunnyDayStatus.INCOMPLETE, ratio)
    if ratio >= 0.70 and weather.precipitation_mm <= 0.5:
        return SunnyDayDecision(SunnyDayStatus.QUALIFYING, ratio)
    return SunnyDayDecision(SunnyDayStatus.NOT_SUNNY, ratio)


def performance_ratio(
    *,
    daily_kwh: float | None,
    daily_radiation_mj_m2: float | None,
    baseline_kwh: float | None,
    baseline_radiation_mj_m2: float | None,
) -> float | None:
    values = (
        daily_kwh,
        daily_radiation_mj_m2,
        baseline_kwh,
        baseline_radiation_mj_m2,
    )
    if any(value is None or value <= 0 for value in values):
        return None
    assert daily_kwh is not None
    assert daily_radiation_mj_m2 is not None
    assert baseline_kwh is not None
    assert baseline_radiation_mj_m2 is not None
    return (daily_kwh / daily_radiation_mj_m2) / (baseline_kwh / baseline_radiation_mj_m2)


@dataclass(frozen=True, slots=True)
class LowPerformanceDay:
    day: date
    ratio: float


@dataclass(frozen=True, slots=True)
class CleaningEvidence:
    low_days: tuple[LowPerformanceDay, ...] = ()


class CleaningStatus(StrEnum):
    UNKNOWN = "unknown"
    NOT_QUALIFYING = "not_qualifying"
    NORMAL = "normal"
    LOW = "low"
    ALERT = "alert"


@dataclass(frozen=True, slots=True)
class CleaningDecision:
    status: CleaningStatus
    performance_ratio: float | None
    evidence: CleaningEvidence

    @property
    def should_alert(self) -> bool:
        return self.status is CleaningStatus.ALERT


def evaluate_cleaning_day(
    *,
    generation: GenerationDay,
    weather: WeatherDay,
    baseline_kwh: float | None,
    baseline_radiation_mj_m2: float | None,
    evidence: CleaningEvidence | None = None,
) -> CleaningDecision:
    if evidence is None:
        evidence = CleaningEvidence()
    sunny = qualify_sunny_day(generation, weather)
    if sunny.status is not SunnyDayStatus.QUALIFYING:
        return CleaningDecision(CleaningStatus.NOT_QUALIFYING, None, evidence)
    ratio = performance_ratio(
        daily_kwh=generation.energy_kwh,
        daily_radiation_mj_m2=weather.shortwave_radiation_mj_m2,
        baseline_kwh=baseline_kwh,
        baseline_radiation_mj_m2=baseline_radiation_mj_m2,
    )
    if ratio is None:
        return CleaningDecision(CleaningStatus.UNKNOWN, None, evidence)
    if ratio >= 0.8:
        return CleaningDecision(CleaningStatus.NORMAL, ratio, CleaningEvidence())

    low_day = LowPerformanceDay(generation.day, ratio)
    retained = tuple(item for item in evidence.low_days if item.day != generation.day)
    updated = CleaningEvidence((*retained, low_day))
    status = CleaningStatus.ALERT if len(updated.low_days) >= 3 else CleaningStatus.LOW
    return CleaningDecision(status, ratio, updated)


def _fresh(evaluated_at: datetime, observed_at: datetime, maximum_age: timedelta) -> bool:
    age = evaluated_at - observed_at
    return timedelta(0) <= age <= maximum_age


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")

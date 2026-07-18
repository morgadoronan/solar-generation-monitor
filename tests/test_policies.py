from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solar_monitor.models import (
    DeviceCondition,
    GenerationDay,
    PowerSample,
    WeatherDay,
    WeatherSnapshot,
)
from solar_monitor.policies import (
    CleaningEvidence,
    CleaningStatus,
    InterruptionStatus,
    SunnyDayStatus,
    evaluate_cleaning_day,
    evaluate_interruption,
    performance_ratio,
    qualify_sunny_day,
)

ZONE = ZoneInfo("America/Sao_Paulo")
NOW = datetime(2026, 6, 7, 12, 30, tzinfo=ZONE)


def power(minutes_ago: int = 0, kw: float = 0.05) -> PowerSample:
    return PowerSample(NOW - timedelta(minutes=minutes_ago), kw)


def weather(minutes_ago: int = 0, radiation: float = 200) -> WeatherSnapshot:
    return WeatherSnapshot(NOW - timedelta(minutes=minutes_ago), radiation, 0)


def evaluate(**overrides: object):  # type: ignore[no-untyped-def]
    values = {
        "evaluated_at": NOW,
        "solar_elevation_degrees": 45,
        "power_sample": power(),
        "weather": weather(),
        "device_condition": DeviceCondition.ONLINE,
    }
    values.update(overrides)
    return evaluate_interruption(**values)  # type: ignore[arg-type]


def test_two_distinct_provider_observations_twenty_minutes_apart_interrupt() -> None:
    first = evaluate(power_sample=power(20))
    duplicate = evaluate(power_sample=power(20), evidence=first.evidence)
    second = evaluate(power_sample=power(), evidence=duplicate.evidence)

    assert first.status is InterruptionStatus.PENDING
    assert duplicate.status is InterruptionStatus.PENDING
    assert second.status is InterruptionStatus.INTERRUPTED


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"solar_elevation_degrees": 10}, InterruptionStatus.NIGHTTIME),
        ({"device_condition": DeviceCondition.OFFLINE}, InterruptionStatus.OFFLINE),
        ({"power_sample": power(21)}, InterruptionStatus.STALE_TELEMETRY),
        ({"weather": None}, InterruptionStatus.UNKNOWN_WEATHER),
        ({"weather": weather(91)}, InterruptionStatus.STALE_WEATHER),
        ({"weather": weather(radiation=199)}, InterruptionStatus.NORMAL),
        ({"power_sample": power(kw=0.051)}, InterruptionStatus.NORMAL),
    ],
)
def test_interruption_boundaries(
    overrides: dict[str, object], expected: InterruptionStatus
) -> None:
    assert evaluate(**overrides).status is expected


def generation(day: date, energy: float | None = 10, complete: bool = True) -> GenerationDay:
    return GenerationDay(day, (), energy, complete)


def weather_day(
    day: date,
    *,
    sunshine_ratio: float = 0.7,
    rain: float = 0.5,
    radiation: float | None = 20,
    complete: bool = True,
) -> WeatherDay:
    sunrise = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=6)
    return WeatherDay(
        day,
        sunrise,
        sunrise.replace(hour=18),
        sunshine_ratio * 43200,
        43200,
        rain,
        radiation,
        complete,
    )


def test_sunny_day_thresholds_are_inclusive_and_require_complete_data() -> None:
    day = date(2026, 6, 7)
    assert qualify_sunny_day(generation(day), weather_day(day)).status is SunnyDayStatus.QUALIFYING
    assert (
        qualify_sunny_day(generation(day), weather_day(day, sunshine_ratio=0.699)).status
        is SunnyDayStatus.NOT_SUNNY
    )
    assert (
        qualify_sunny_day(generation(day), weather_day(day, rain=0.501)).status
        is SunnyDayStatus.NOT_SUNNY
    )
    assert (
        qualify_sunny_day(generation(day, complete=False), weather_day(day)).status
        is SunnyDayStatus.INCOMPLETE
    )


def test_performance_ratio_and_unknown_inputs() -> None:
    assert performance_ratio(
        daily_kwh=8, daily_radiation_mj_m2=20, baseline_kwh=10, baseline_radiation_mj_m2=20
    ) == pytest.approx(0.8)
    assert performance_ratio(
        daily_kwh=0, daily_radiation_mj_m2=20, baseline_kwh=10, baseline_radiation_mj_m2=20
    ) is None


def test_three_low_qualifying_days_alert_and_normal_day_resets() -> None:
    evidence = CleaningEvidence()
    for offset in range(3):
        day = date(2026, 6, 7) + timedelta(days=offset)
        decision = evaluate_cleaning_day(
            generation=generation(day, 7.9),
            weather=weather_day(day),
            baseline_kwh=10,
            baseline_radiation_mj_m2=20,
            evidence=evidence,
        )
        evidence = decision.evidence
    assert decision.status is CleaningStatus.ALERT

    rainy_day = date(2026, 6, 10)
    skipped = evaluate_cleaning_day(
        generation=generation(rainy_day, 10),
        weather=weather_day(rainy_day, rain=1),
        baseline_kwh=10,
        baseline_radiation_mj_m2=20,
        evidence=evidence,
    )
    assert skipped.evidence == evidence

    normal_day = date(2026, 6, 11)
    reset = evaluate_cleaning_day(
        generation=generation(normal_day, 10),
        weather=weather_day(normal_day),
        baseline_kwh=10,
        baseline_radiation_mj_m2=20,
        evidence=evidence,
    )
    assert reset.status is CleaningStatus.NORMAL
    assert reset.evidence.low_days == ()

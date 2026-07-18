from datetime import UTC, date, datetime, timedelta

import pytest

from solar_monitor.models import GenerationDay, PowerSample, WeatherDay, WeatherSnapshot

NOW = datetime(2026, 6, 7, 12, 15, tzinfo=UTC)


def test_power_sample_requires_aware_non_negative_value() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PowerSample(datetime(2026, 6, 7, 12, 15), 1.0)
    with pytest.raises(ValueError, match="non-negative"):
        PowerSample(NOW, -0.01)


def test_generation_day_requires_ordered_unique_samples() -> None:
    later = PowerSample(NOW + timedelta(minutes=5), 2.0)
    earlier = PowerSample(NOW, 1.0)

    with pytest.raises(ValueError, match="ordered"):
        GenerationDay(date(2026, 6, 7), (later, earlier), 3.0, True)
    with pytest.raises(ValueError, match="unique"):
        GenerationDay(date(2026, 6, 7), (earlier, earlier), 3.0, True)


def test_generation_day_exposes_latest_sample() -> None:
    first = PowerSample(NOW, 1.0)
    last = PowerSample(NOW + timedelta(minutes=5), 2.0)
    day = GenerationDay(date(2026, 6, 7), (first, last), 3.0, True)

    assert day.latest_sample is last


def test_weather_snapshot_validates_cloud_cover() -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        WeatherSnapshot(NOW, 300.0, 0.0, 101.0)


def test_weather_day_calculates_sunshine_ratio() -> None:
    weather = WeatherDay(
        day=date(2026, 6, 7),
        sunrise=NOW - timedelta(hours=6),
        sunset=NOW + timedelta(hours=6),
        sunshine_seconds=7 * 60 * 60,
        daylight_seconds=10 * 60 * 60,
        precipitation_mm=0.1,
        shortwave_radiation_mj_m2=15.2,
        complete=True,
    )

    assert weather.sunshine_ratio == pytest.approx(0.7)


def test_weather_day_rejects_impossible_sunshine_duration() -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        WeatherDay(
            day=date(2026, 6, 7),
            sunrise=NOW - timedelta(hours=6),
            sunset=NOW + timedelta(hours=6),
            sunshine_seconds=11,
            daylight_seconds=10,
            precipitation_mm=0,
            shortwave_radiation_mj_m2=1,
            complete=True,
        )

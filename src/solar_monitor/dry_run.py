"""Deterministic, network-free providers used exclusively by CLI dry runs."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import pi, sin
from zoneinfo import ZoneInfo

from solar_monitor.models import (
    DeviceCondition,
    DeviceStatus,
    GenerationDay,
    PowerSample,
    WeatherDay,
    WeatherSnapshot,
)

DRY_RUN_TIMEZONE = ZoneInfo("America/Sao_Paulo")
DRY_RUN_LATITUDE = -16.65
DRY_RUN_LONGITUDE = -49.49
DRY_RUN_PLANT_ID = "synthetic-dry-run"


class SyntheticProvider:
    """Stable generation and weather data with no external transport."""

    def get_generation_day(self, day: date) -> GenerationDay:
        midnight = datetime.combine(day, datetime.min.time(), DRY_RUN_TIMEZONE)
        samples = tuple(
            PowerSample(midnight + timedelta(minutes=minute), _power(minute))
            for minute in range(0, 24 * 60, 5)
        )
        energy = sum(sample.power_kw for sample in samples) * 5 / 60
        return GenerationDay(day, samples, energy, True)

    def get_device_status(self, observed_at: datetime) -> DeviceStatus:
        return DeviceStatus(observed_at, DeviceCondition.ONLINE, "synthetic")

    def get_snapshot(self, observed_at: datetime) -> WeatherSnapshot:
        radiation = 650.0 if 6 <= observed_at.astimezone(DRY_RUN_TIMEZONE).hour < 18 else 0.0
        return WeatherSnapshot(observed_at, radiation, 0.0, 10.0)

    def get_weather_day(self, day: date) -> WeatherDay:
        sunrise = datetime.combine(day, datetime.min.time(), DRY_RUN_TIMEZONE).replace(hour=6)
        return WeatherDay(
            day=day,
            sunrise=sunrise,
            sunset=sunrise.replace(hour=18),
            sunshine_seconds=10 * 3600,
            daylight_seconds=12 * 3600,
            precipitation_mm=0.0,
            shortwave_radiation_mj_m2=22.0,
            complete=True,
        )


def _power(minute: int) -> float:
    if minute < 6 * 60 or minute > 18 * 60:
        return 0.0
    return 6.0 * sin(pi * (minute - 6 * 60) / (12 * 60))

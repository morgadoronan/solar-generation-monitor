"""Typed, secret-free Open-Meteo forecast and archive clients."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from solar_monitor.models import WeatherDay, WeatherSnapshot
from solar_monitor.ports import MalformedProviderResponseError, ProviderError

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, timeout: float) -> HttpResponse: ...


class UrllibTransport:
    """Small stdlib transport whose errors never expose the request URL."""

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        try:
            with urlopen(url, timeout=timeout) as response:
                return HttpResponse(status=response.status, body=response.read())
        except HTTPError as error:
            return HttpResponse(status=error.code, body=error.read())
        except (OSError, URLError) as error:
            raise ProviderError("Open-Meteo request failed") from error


Clock = Callable[[], datetime]


class OpenMeteoClient:
    """Weather provider for one configurable location and IANA timezone."""

    def __init__(
        self,
        *,
        latitude: float,
        longitude: float,
        timezone: ZoneInfo,
        transport: HttpTransport | None = None,
        clock: Clock | None = None,
        timeout: float = 15.0,
        archive_lag_days: int = 5,
    ) -> None:
        if not -90 <= latitude <= 90:
            raise ValueError("latitude must be between -90 and 90")
        if not -180 <= longitude <= 180:
            raise ValueError("longitude must be between -180 and 180")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if archive_lag_days < 0:
            raise ValueError("archive_lag_days must be non-negative")
        self._latitude = latitude
        self._longitude = longitude
        self._timezone = timezone
        self._transport = transport or UrllibTransport()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout = timeout
        self._archive_lag_days = archive_lag_days

    def get_snapshot(self, observed_at: datetime) -> WeatherSnapshot:
        _require_aware(observed_at, "observed_at")
        local_day = observed_at.astimezone(self._timezone).date()
        payload = self._request(
            FORECAST_URL,
            {
                "hourly": "shortwave_radiation,precipitation,cloud_cover",
                "start_date": local_day.isoformat(),
                "end_date": local_day.isoformat(),
            },
        )
        self._validate_timezone(payload)
        hourly = _mapping(payload, "hourly")
        units = _mapping(payload, "hourly_units")
        _unit(units, "shortwave_radiation", {"W/m²", "W/m2"})
        _unit(units, "precipitation", {"mm"})
        _unit(units, "cloud_cover", {"%"})
        times = _datetime_array(hourly, "time", self._timezone)
        radiation = _number_array(hourly, "shortwave_radiation")
        precipitation = _number_array(hourly, "precipitation")
        cloud_cover = _optional_number_array(hourly, "cloud_cover")
        _same_length(times, radiation, precipitation, cloud_cover)
        if not times:
            raise MalformedProviderResponseError("Open-Meteo returned no hourly data")
        elapsed = [item for item, value in enumerate(times) if value <= observed_at]
        index = elapsed[-1] if elapsed else 0
        cloud = cloud_cover[index]
        return WeatherSnapshot(
            observed_at=times[index],
            shortwave_radiation_w_m2=radiation[index],
            precipitation_mm=precipitation[index],
            cloud_cover_percent=cloud,
        )

    def get_weather_day(self, day: date) -> WeatherDay:
        today = self._now().astimezone(self._timezone).date()
        endpoint = (
            ARCHIVE_URL if day < today - timedelta(days=self._archive_lag_days) else FORECAST_URL
        )
        payload = self._request(
            endpoint,
            {
                "daily": (
                    "sunrise,sunset,sunshine_duration,daylight_duration,"
                    "precipitation_sum,shortwave_radiation_sum"
                ),
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
            },
        )
        self._validate_timezone(payload)
        daily = _mapping(payload, "daily")
        units = _mapping(payload, "daily_units")
        _unit(units, "sunshine_duration", {"s"})
        _unit(units, "daylight_duration", {"s"})
        _unit(units, "precipitation_sum", {"mm"})
        _unit(units, "shortwave_radiation_sum", {"MJ/m²", "MJ/m2"})
        days = _date_array(daily, "time")
        if day not in days:
            raise MalformedProviderResponseError("Open-Meteo omitted the requested day")
        index = days.index(day)
        sunrise = _datetime_array(daily, "sunrise", self._timezone)
        sunset = _datetime_array(daily, "sunset", self._timezone)
        sunshine = _optional_number_array(daily, "sunshine_duration")
        daylight = _optional_number_array(daily, "daylight_duration")
        precipitation = _optional_number_array(daily, "precipitation_sum")
        radiation = _optional_number_array(daily, "shortwave_radiation_sum")
        _same_length(days, sunrise, sunset, sunshine, daylight, precipitation, radiation)
        required = (sunshine[index], daylight[index], precipitation[index])
        if any(value is None for value in required):
            raise MalformedProviderResponseError("Open-Meteo daily data is incomplete")
        sunshine_value, daylight_value, precipitation_value = cast(
            tuple[float, float, float], required
        )
        return WeatherDay(
            day=day,
            sunrise=sunrise[index],
            sunset=sunset[index],
            sunshine_seconds=sunshine_value,
            daylight_seconds=daylight_value,
            precipitation_mm=precipitation_value,
            shortwave_radiation_mj_m2=radiation[index],
            complete=day < today and radiation[index] is not None,
        )

    def _validate_timezone(self, payload: Mapping[str, Any]) -> None:
        if payload.get("timezone") != self._timezone.key:
            raise MalformedProviderResponseError("Open-Meteo returned an unexpected timezone")

    def _now(self) -> datetime:
        value = self._clock()
        _require_aware(value, "clock result")
        return value

    def _request(self, endpoint: str, query: Mapping[str, str]) -> Mapping[str, Any]:
        parameters = {
            "latitude": str(self._latitude),
            "longitude": str(self._longitude),
            "timezone": self._timezone.key,
            **query,
        }
        response = self._transport.get(f"{endpoint}?{urlencode(parameters)}", timeout=self._timeout)
        if response.status != 200:
            raise ProviderError(f"Open-Meteo returned HTTP {response.status}")
        try:
            value = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MalformedProviderResponseError("Open-Meteo returned invalid JSON") from error
        if not isinstance(value, dict):
            raise MalformedProviderResponseError("Open-Meteo returned an invalid payload")
        if value.get("error") is True:
            raise ProviderError("Open-Meteo rejected the request")
        return cast(Mapping[str, Any], value)


OpenMeteoForecastClient = OpenMeteoClient
OpenMeteoArchiveClient = OpenMeteoClient


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise MalformedProviderResponseError(f"Open-Meteo omitted {key}")
    return cast(Mapping[str, Any], value)


def _unit(units: Mapping[str, Any], key: str, accepted: set[str]) -> None:
    if units.get(key) not in accepted:
        raise MalformedProviderResponseError(f"Open-Meteo returned an unexpected {key} unit")


def _sequence(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise MalformedProviderResponseError(f"Open-Meteo omitted {key}")
    return value


def _number_array(payload: Mapping[str, Any], key: str) -> list[float]:
    result = _optional_number_array(payload, key)
    if any(value is None for value in result):
        raise MalformedProviderResponseError(f"Open-Meteo returned incomplete {key}")
    return cast(list[float], result)


def _optional_number_array(payload: Mapping[str, Any], key: str) -> list[float | None]:
    values = _sequence(payload, key)
    if any(
        value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool))
        for value in values
    ):
        raise MalformedProviderResponseError(f"Open-Meteo returned invalid {key}")
    return [None if value is None else float(value) for value in values]


def _datetime_array(payload: Mapping[str, Any], key: str, timezone: ZoneInfo) -> list[datetime]:
    result: list[datetime] = []
    for value in _sequence(payload, key):
        if not isinstance(value, str):
            raise MalformedProviderResponseError(f"Open-Meteo returned invalid {key}")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise MalformedProviderResponseError(f"Open-Meteo returned invalid {key}") from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone)
        else:
            parsed = parsed.astimezone(timezone)
        result.append(parsed)
    return result


def _date_array(payload: Mapping[str, Any], key: str) -> list[date]:
    result: list[date] = []
    for value in _sequence(payload, key):
        if not isinstance(value, str):
            raise MalformedProviderResponseError(f"Open-Meteo returned invalid {key}")
        try:
            result.append(date.fromisoformat(value))
        except ValueError as error:
            raise MalformedProviderResponseError(f"Open-Meteo returned invalid {key}") from error
    return result


def _same_length(*values: object) -> None:
    lengths = {len(cast(list[object], value)) for value in values}
    if len(lengths) != 1:
        raise MalformedProviderResponseError("Open-Meteo returned misaligned arrays")

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest

from solar_monitor.open_meteo import ARCHIVE_URL, FORECAST_URL, HttpResponse, OpenMeteoClient
from solar_monitor.ports import MalformedProviderResponseError, ProviderError

ZONE = ZoneInfo("America/Sao_Paulo")
FIXTURES = Path(__file__).parent / "fixtures" / "open_meteo"


class FakeTransport:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.urls: list[str] = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        assert timeout == 15
        self.urls.append(url)
        return HttpResponse(self.status, self.payload)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def client(transport: FakeTransport) -> OpenMeteoClient:
    return OpenMeteoClient(
        latitude=-16.65,
        longitude=-49.49,
        timezone=ZONE,
        transport=transport,
        clock=lambda: datetime(2026, 7, 18, 12, tzinfo=ZONE),
    )


def test_snapshot_uses_nearest_hour_and_configured_timezone() -> None:
    transport = FakeTransport(fixture("hourly.json"))
    snapshot = client(transport).get_snapshot(datetime(2026, 6, 7, 12, 12, tzinfo=ZONE))

    assert snapshot.observed_at == datetime(2026, 6, 7, 12, tzinfo=ZONE)
    assert snapshot.shortwave_radiation_w_m2 == 742
    query = parse_qs(urlparse(transport.urls[0]).query)
    assert query["timezone"] == ["America/Sao_Paulo"]
    assert transport.urls[0].startswith(FORECAST_URL)


def test_old_daily_value_uses_archive_and_marks_complete() -> None:
    transport = FakeTransport(fixture("daily.json"))
    weather = client(transport).get_weather_day(date(2026, 6, 7))

    assert weather.complete
    assert weather.shortwave_radiation_mj_m2 == 18.6
    assert weather.sunrise.utcoffset() == ZONE.utcoffset(weather.sunrise)
    assert transport.urls[0].startswith(ARCHIVE_URL)


def test_recent_day_uses_forecast_to_tolerate_archive_publication_lag() -> None:
    value = json.loads(fixture("daily.json"))
    value["daily"]["time"] = ["2026-07-17"]
    value["daily"]["sunrise"] = ["2026-07-17T06:45"]
    value["daily"]["sunset"] = ["2026-07-17T17:45"]
    transport = FakeTransport(json.dumps(value).encode())

    assert client(transport).get_weather_day(date(2026, 7, 17)).complete
    assert transport.urls[0].startswith(FORECAST_URL)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["hourly_units"].__setitem__("shortwave_radiation", "kW/m²"),
        lambda value: value["hourly"].__setitem__("precipitation", []),
        lambda value: value["hourly"].__setitem__("time", ["not-a-time"]),
    ],
)
def test_malformed_hourly_payload_is_rejected(mutation: object) -> None:
    value = json.loads(fixture("hourly.json"))
    mutation(value)  # type: ignore[operator]
    transport = FakeTransport(json.dumps(value).encode())
    with pytest.raises(MalformedProviderResponseError):
        client(transport).get_snapshot(datetime(2026, 6, 7, 12, tzinfo=ZONE))


def test_http_error_is_sanitized() -> None:
    transport = FakeTransport(b'{"reason":"provider internals"}', 503)
    with pytest.raises(ProviderError, match="HTTP 503") as raised:
        client(transport).get_snapshot(datetime(2026, 6, 7, 12, tzinfo=ZONE))
    assert "latitude" not in str(raised.value)


def test_naive_snapshot_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        client(FakeTransport(fixture("hourly.json"))).get_snapshot(datetime(2026, 6, 7))

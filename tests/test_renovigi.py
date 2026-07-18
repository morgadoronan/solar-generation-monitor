from __future__ import annotations

import hashlib
import json
from collections import deque
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import pytest

from solar_monitor.config import GenerationConfig, Secret
from solar_monitor.models import DeviceCondition
from solar_monitor.ports import (
    InvalidCredentialsError,
    MalformedProviderResponseError,
    ProviderError,
)
from solar_monitor.renovigi import COMPANY_KEY, HttpResponse, RenovigiClient

FIXTURES = Path(__file__).parent / "fixtures" / "renovigi"
LOCAL = ZoneInfo("America/Sao_Paulo")
FIXED_NOW = datetime(2026, 6, 7, 15, 0, tzinfo=LOCAL)
CANARY = "canary-password-do-not-leak"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def response(name: str, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status, fixture(name))


class FakeTransport:
    def __init__(self, *script: HttpResponse | Exception) -> None:
        self.script = deque(script)
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        self.urls.append(url)
        self.timeouts.append(timeout)
        result = self.script.popleft()
        if isinstance(result, Exception):
            raise result
        return result


def client(
    transport: FakeTransport,
    *,
    password: str = CANARY,
    sleep: Any = None,
    retries: int = 3,
) -> RenovigiClient:
    kwargs: dict[str, Any] = {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    return RenovigiClient(
        GenerationConfig("Synthetic-User", Secret(password), "9999999"),
        LOCAL,
        transport=transport,
        clock=lambda: FIXED_NOW,
        transient_retries=retries,
        **kwargs,
    )


def json_response(payload: dict[str, object]) -> HttpResponse:
    return HttpResponse(200, json.dumps(payload, separators=(",", ":")).encode())


def complete_curve(*, power_kw: float = 1.0) -> HttpResponse:
    points = [
        {
            "val": str(power_kw),
            "ts": f"2026-06-07 {minute // 60:02d}:{minute % 60:02d}:00",
        }
        for minute in range(0, 24 * 60, 5)
    ]
    return json_response({"err": 0, "desc": "ERR_NONE", "dat": {"outputPower": points}})


def synthetic_baseline_curve() -> HttpResponse:
    """A sanitized triangular curve integrating to exactly 33.512083 kWh."""
    target_energy_kwh = 33.512083
    peak_kw = target_energy_kwh / 6
    points = [
        {
            "val": str(peak_kw * max(0.0, 1 - abs(minute - 720) / 360)),
            "ts": f"2026-06-07 {minute // 60:02d}:{minute % 60:02d}:00",
        }
        for minute in range(0, 24 * 60, 5)
    ]
    return json_response({"err": 0, "desc": "ERR_NONE", "dat": {"outputPower": points}})


def actions(transport: FakeTransport) -> list[str]:
    return [dict(parse_qsl(urlsplit(url).query))["action"] for url in transport.urls]


def test_auth_signing_uses_exact_sha1_and_query_order() -> None:
    transport = FakeTransport(response("auth_success.json"), response("current_power.json"))

    result = client(transport).get_current_power(FIXED_NOW)

    assert result is not None
    auth_query = urlsplit(transport.urls[0]).query
    pairs = parse_qsl(auth_query, keep_blank_values=True)
    assert [key for key, _ in pairs] == [
        "sign",
        "salt",
        "action",
        "usr",
        "company-key",
    ]
    salt = str(round(FIXED_NOW.timestamp() * 1000))
    action_segment = (
        "&action=auth&usr=synthetic-user"
        f"&company-key={COMPANY_KEY}"
    )
    password_hash = hashlib.sha1(CANARY.encode()).hexdigest()
    expected = hashlib.sha1(f"{salt}{password_hash}{action_segment}".encode()).hexdigest()
    assert dict(pairs)["sign"] == expected

    business_pairs = parse_qsl(urlsplit(transport.urls[1]).query)
    assert [key for key, _ in business_pairs] == [
        "sign",
        "salt",
        "token",
        "action",
        "plantid",
        "i18n",
        "lang",
    ]
    business_segment = (
        "&action=queryPlantActiveOuputPowerCurrent&plantid=9999999"
        "&i18n=pt_BR&lang=pt_BR"
    )
    expected_business = hashlib.sha1(
        f"{salt}fixture-secretfixture-token{business_segment}".encode()
    ).hexdigest()
    assert dict(business_pairs)["sign"] == expected_business


def test_session_is_cached_in_process() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("current_power.json"),
        response("status_online.json"),
    )
    provider = client(transport)

    provider.get_current_power(FIXED_NOW)
    provider.get_device_status(FIXED_NOW)

    assert actions(transport) == [
        "auth",
        "queryPlantActiveOuputPowerCurrent",
        "queryPlantDeviceStatus",
    ]


def test_invalid_credentials_are_not_retried() -> None:
    transport = FakeTransport(response("auth_failure.json"))

    with pytest.raises(InvalidCredentialsError, match="credentials were rejected"):
        client(transport).get_current_power(FIXED_NOW)

    assert len(transport.urls) == 1


def test_explicit_expiry_reauthenticates_exactly_once() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("session_expired.json"),
        response("auth_success.json"),
        response("current_power.json"),
    )

    result = client(transport).get_current_power(FIXED_NOW)

    assert result is not None
    assert actions(transport) == [
        "auth",
        "queryPlantActiveOuputPowerCurrent",
        "auth",
        "queryPlantActiveOuputPowerCurrent",
    ]


def test_second_expiry_is_exposed_without_more_reauthentication() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("session_expired.json"),
        response("auth_success.json"),
        response("session_expired.json"),
    )

    with pytest.raises(ProviderError, match="session expired"):
        client(transport).get_current_power(FIXED_NOW)

    assert actions(transport).count("auth") == 2


def test_transient_transport_failure_has_bounded_retry_and_backoff() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        response("auth_success.json"),
        TimeoutError(f"timeout at https://unsafe.invalid/?token={CANARY}"),
        response("current_power.json"),
    )

    result = client(transport, sleep=sleeps.append).get_current_power(FIXED_NOW)

    assert result is not None
    assert sleeps == [1.0]
    assert transport.timeouts == [30.0, 30.0, 30.0]


def test_default_retry_budget_survives_three_consecutive_transport_timeouts() -> None:
    sleeps: list[float] = []
    transport = FakeTransport(
        response("auth_success.json"),
        TimeoutError("timeout one"),
        TimeoutError("timeout two"),
        TimeoutError("timeout three"),
        response("current_power.json"),
    )

    result = client(transport, sleep=sleeps.append).get_current_power(FIXED_NOW)

    assert result is not None
    assert sleeps == [1.0, 2.0, 4.0]
    assert transport.timeouts == [30.0] * 5


def test_transient_provider_timeout_is_retried() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("transient_timeout.json"),
        response("current_power.json"),
    )

    assert client(transport, sleep=lambda _: None).get_current_power(FIXED_NOW) is not None


def test_retry_budget_is_enforced() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        TimeoutError(CANARY),
        TimeoutError(CANARY),
    )

    with pytest.raises(ProviderError, match="request failed") as caught:
        client(transport, sleep=lambda _: None, retries=1).get_current_power(FIXED_NOW)

    assert len(transport.urls) == 3
    assert CANARY not in str(caught.value)


def test_generation_day_prefers_authoritative_energy() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("power_curve.json"),
        response("energy.json"),
    )

    result = client(transport).get_generation_day(date(2026, 6, 7))

    assert [sample.power_kw for sample in result.samples] == [0, 1.2, 6.19]
    assert result.energy_kwh == 31.42
    assert not result.complete


def test_complete_288_sample_curve_uses_integrated_energy_fallback() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        synthetic_baseline_curve(),
        response("no_record.json"),
    )

    result = client(transport).get_generation_day(date(2026, 6, 7))

    assert result.complete
    assert len(result.samples) == 288
    assert result.energy_kwh == pytest.approx(33.512083)


def test_complete_curve_falls_back_after_transient_energy_failure() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        complete_curve(power_kw=1.0),
        TimeoutError(f"timeout at https://unsafe.invalid/?token={CANARY}"),
    )

    result = client(transport, retries=0).get_generation_day(date(2026, 6, 7))

    assert result.complete
    assert result.energy_kwh == pytest.approx(23 + 55 / 60)


def test_complete_curve_falls_back_after_malformed_energy() -> None:
    malformed_energy = json_response(
        {"err": 0, "desc": "ERR_NONE", "dat": {"energy": f"bad-{CANARY}"}}
    )
    transport = FakeTransport(
        response("auth_success.json"),
        complete_curve(power_kw=1.0),
        malformed_energy,
    )

    result = client(transport).get_generation_day(date(2026, 6, 7))

    assert result.complete
    assert result.energy_kwh == pytest.approx(23 + 55 / 60)


def test_energy_invalid_credentials_propagate_even_with_complete_curve() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        complete_curve(),
        response("auth_failure.json"),
    )

    with pytest.raises(InvalidCredentialsError):
        client(transport).get_generation_day(date(2026, 6, 7))


def test_energy_session_expiry_propagates_after_single_reauthentication() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        complete_curve(),
        response("session_expired.json"),
        response("auth_success.json"),
        response("session_expired.json"),
    )

    with pytest.raises(ProviderError, match="session expired"):
        client(transport).get_generation_day(date(2026, 6, 7))

    assert actions(transport).count("auth") == 2


def test_incomplete_curve_does_not_create_fallback_energy() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("power_curve.json"),
        response("no_record.json"),
    )

    result = client(transport).get_generation_day(date(2026, 6, 7))

    assert result.energy_kwh is None
    assert not result.complete


def test_incomplete_curve_propagates_energy_provider_failure() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("power_curve.json"),
        TimeoutError(CANARY),
    )

    with pytest.raises(ProviderError, match="request failed"):
        client(transport, retries=0).get_generation_day(date(2026, 6, 7))


def test_no_record_returns_empty_generation_day() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        response("no_record.json"),
        response("no_record.json"),
    )

    result = client(transport).get_generation_day(date(2026, 6, 7))

    assert result.samples == ()
    assert result.energy_kwh is None
    assert not result.complete


def test_malformed_power_data_raises_sanitized_error() -> None:
    transport = FakeTransport(response("auth_success.json"), response("malformed.json"))

    with pytest.raises(MalformedProviderResponseError, match="malformed power data"):
        client(transport).get_generation_day(date(2026, 6, 7))


def test_current_power_is_converted_to_kw_and_plant_timezone() -> None:
    transport = FakeTransport(response("auth_success.json"), response("current_power.json"))
    utc_observation = datetime(2026, 6, 7, 18, 0, tzinfo=ZoneInfo("UTC"))

    result = client(transport).get_current_power(utc_observation)

    assert result is not None
    assert result.power_kw == 6.19
    assert result.observed_at == FIXED_NOW
    assert result.observed_at.tzinfo == LOCAL


@pytest.mark.parametrize(
    ("fixture_name", "condition", "provider_code"),
    [
        ("status_online.json", DeviceCondition.ONLINE, "0x00"),
        ("status_warning.json", DeviceCondition.UNKNOWN, "0x04"),
        ("status_nested_offline.json", DeviceCondition.OFFLINE, "0x01"),
    ],
)
def test_device_status_is_parsed_in_plant_timezone(
    fixture_name: str, condition: DeviceCondition, provider_code: str
) -> None:
    transport = FakeTransport(response("auth_success.json"), response(fixture_name))
    utc_observation = datetime(2026, 6, 7, 18, 0, tzinfo=ZoneInfo("UTC"))

    result = client(transport).get_device_status(utc_observation)

    assert result.condition is condition
    assert result.provider_code == provider_code
    assert result.observed_at == FIXED_NOW


def test_naive_observation_is_rejected_before_network_io() -> None:
    transport = FakeTransport()

    with pytest.raises(ValueError, match="timezone-aware"):
        client(transport).get_device_status(datetime(2026, 6, 7, 15, 0))

    assert transport.urls == []


def test_provider_error_body_and_transport_details_are_never_exposed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    transport = FakeTransport(response("auth_success.json"), response("canary_error.json"))

    with pytest.raises(ProviderError) as caught:
        client(transport, retries=0).get_current_power(FIXED_NOW)

    exposed = f"{caught.value}\n{caplog.text}"
    for forbidden in (
        "do-not-leak",
        CANARY,
        "fixture-token",
        "fixture-secret",
        "https://",
        "?",
    ):
        assert forbidden not in exposed


def test_malformed_json_error_does_not_include_response_or_query() -> None:
    transport = FakeTransport(
        response("auth_success.json"),
        HttpResponse(200, f"not-json {CANARY} token=unsafe".encode()),
    )

    with pytest.raises(MalformedProviderResponseError) as caught:
        client(transport).get_current_power(FIXED_NOW)

    assert CANARY not in str(caught.value)
    assert "token=unsafe" not in str(caught.value)
    assert "?" not in str(caught.value)

"""Secret-safe Renovigi/ShineMonitor generation provider."""

from __future__ import annotations

import hashlib
import itertools
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo

from solar_monitor.config import GenerationConfig
from solar_monitor.models import (
    DeviceCondition,
    DeviceStatus,
    GenerationDay,
    PowerSample,
)
from solar_monitor.ports import (
    InvalidCredentialsError,
    MalformedProviderResponseError,
    ProviderError,
    SessionExpiredError,
)

BASE_URL = "https://web.shinemonitor.com/public/"
COMPANY_KEY = "bnrl_frRFjEz8Mkn"
_LOCALE_CONTEXT: tuple[tuple[str, str], ...] = (
    ("i18n", "pt_BR"),
    ("lang", "pt_BR"),
)
_INVALID_CREDENTIAL_CODES = frozenset({0x000F, 0x0010, 0x0019, 0x0105})
_SESSION_EXPIRED_CODES = frozenset({0x000A, 0x010E})
_TRANSIENT_PROVIDER_CODES = frozenset({0x0001, 0x0002, 0x0003, 0x000D})
_NO_RECORD_CODE = 0x000C
_ONLINE_STATUS = 0x00
_OFFLINE_STATUS = 0x01

JsonObject = dict[str, Any]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """Minimal HTTP response exposed by the injectable transport."""

    status: int
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, timeout: float) -> HttpResponse:
        """GET a fully signed URL without logging or decorating failures."""
        ...


class UrllibTransport:
    """Small stdlib transport that never includes request details in errors."""

    def get(self, url: str, *, timeout: float) -> HttpResponse:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "solar-monitor/0.1"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HttpResponse(status=response.status, body=response.read())
        except urllib.error.HTTPError as error:
            # HTTPError's string contains the URL, so only retain safe primitives.
            return HttpResponse(status=error.code, body=error.read())
        except Exception:
            raise OSError("Renovigi transport failed") from None


@dataclass(frozen=True, slots=True, repr=False)
class _Session:
    token: str
    secret: str


class RenovigiClient:
    """Typed synchronous adapter for Renovigi's ShineMonitor tenant."""

    def __init__(
        self,
        config: GenerationConfig,
        timezone: ZoneInfo,
        *,
        transport: HttpTransport | None = None,
        clock: Clock | None = None,
        sleep: Sleeper = time.sleep,
        timeout_seconds: float = 15.0,
        transient_retries: int = 2,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if transient_retries < 0:
            raise ValueError("transient_retries must be non-negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must be non-negative")
        self._config = config
        self._timezone = timezone
        self._transport = transport or UrllibTransport()
        self._clock = clock or (lambda: datetime.now().astimezone())
        self._sleep = sleep
        self._timeout_seconds = timeout_seconds
        self._transient_retries = transient_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._session: _Session | None = None

    def get_generation_day(self, day: date) -> GenerationDay:
        """Return the local-day curve and authoritative energy when available."""
        curve_payload = self._request(
            "queryPlantActiveOuputPowerOneDay",
            (("plantid", self._config.plant_id), ("date", day.isoformat())),
            no_record_is_empty=True,
        )
        samples = self._parse_power_curve(curve_payload, day)
        complete = _curve_is_complete(samples, day, self._timezone)

        try:
            energy_payload = self._request(
                "queryPlantEnergyDay",
                (("plantid", self._config.plant_id), ("date", day.isoformat())),
                no_record_is_empty=True,
            )
            energy_kwh = self._parse_energy(energy_payload)
        except (InvalidCredentialsError, SessionExpiredError):
            raise
        except ProviderError:
            if not complete:
                raise
            energy_kwh = None
        if energy_kwh is None and complete:
            energy_kwh = _integrate_power_curve(samples)
        return GenerationDay(
            day=day,
            samples=samples,
            energy_kwh=energy_kwh,
            complete=complete,
        )

    def get_current_power(self, observed_at: datetime) -> PowerSample | None:
        """Return current plant output, timestamped by the injected local clock."""
        _require_aware(observed_at)
        payload = self._request(
            "queryPlantActiveOuputPowerCurrent",
            (("plantid", self._config.plant_id),),
            no_record_is_empty=True,
        )
        if not payload:
            return None
        data = _required_mapping(payload, "dat")
        power_kw = _non_negative_number(data.get("outputPower"), "outputPower")
        return PowerSample(
            observed_at=observed_at.astimezone(self._timezone),
            power_kw=power_kw,
        )

    def get_device_status(self, observed_at: datetime) -> DeviceStatus:
        """Map the plant's aggregate device status into provider-neutral state."""
        _require_aware(observed_at)
        payload = self._request(
            "queryPlantDeviceStatus",
            (("plantid", self._config.plant_id),),
        )
        data = _required_mapping(payload, "dat")
        raw_status = _parse_device_status(data)
        if raw_status == _ONLINE_STATUS:
            condition = DeviceCondition.ONLINE
        elif raw_status == _OFFLINE_STATUS:
            condition = DeviceCondition.OFFLINE
        else:
            condition = DeviceCondition.UNKNOWN
        return DeviceStatus(
            observed_at=observed_at.astimezone(self._timezone),
            condition=condition,
            provider_code=f"0x{raw_status:02x}",
        )

    def _request(
        self,
        action: str,
        parameters: Sequence[tuple[str, str]],
        *,
        no_record_is_empty: bool = False,
    ) -> JsonObject:
        session = self._authenticate() if self._session is None else self._session
        try:
            return self._send_signed(action, parameters, session, no_record_is_empty)
        except SessionExpiredError:
            # An explicit provider expiry is the sole automatic reauthentication path.
            self._session = None
            session = self._authenticate()
            return self._send_signed(action, parameters, session, no_record_is_empty)

    def _authenticate(self) -> _Session:
        action_parameters: tuple[tuple[str, str], ...] = (
            ("usr", self._config.username.lower()),
            ("company-key", COMPANY_KEY),
        )
        salt = self._salt()
        action_segment = _action_segment("auth", action_parameters)
        password_digest = _sha1(self._config.password.reveal())
        sign = _sha1(f"{salt}{password_digest}{action_segment}")
        url = f"{BASE_URL}?sign={sign}&salt={quote(salt)}{action_segment}"
        payload = self._send(url, operation="authentication")
        err = _error_code(payload)
        if err in _INVALID_CREDENTIAL_CODES or err != 0:
            raise InvalidCredentialsError("Renovigi credentials were rejected")
        try:
            data = _required_mapping(payload, "dat")
            token = _required_text(data.get("token"), "token")
            secret = _required_text(data.get("secret"), "secret")
            expire = data.get("expire")
            if not isinstance(expire, int) or isinstance(expire, bool) or expire <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise MalformedProviderResponseError(
                "Renovigi returned malformed authentication data"
            ) from None
        self._session = _Session(token=token, secret=secret)
        return self._session

    def _send_signed(
        self,
        action: str,
        parameters: Sequence[tuple[str, str]],
        session: _Session,
        no_record_is_empty: bool,
    ) -> JsonObject:
        action_segment = _action_segment(action, (*parameters, *_LOCALE_CONTEXT))
        salt = self._salt()
        sign = _sha1(f"{salt}{session.secret}{session.token}{action_segment}")
        url = (
            f"{BASE_URL}?sign={sign}&salt={quote(salt)}&token={quote(session.token)}"
            f"{action_segment}"
        )
        payload = self._send(url, operation=action)
        err = _error_code(payload)
        if err == 0:
            return payload
        if err == _NO_RECORD_CODE and no_record_is_empty:
            return {}
        if err in _SESSION_EXPIRED_CODES:
            raise SessionExpiredError("Renovigi session expired")
        if err in _INVALID_CREDENTIAL_CODES:
            raise InvalidCredentialsError("Renovigi credentials were rejected")
        raise ProviderError(f"Renovigi {action} request failed")

    def _send(self, url: str, *, operation: str) -> JsonObject:
        for attempt in range(self._transient_retries + 1):
            try:
                response = self._transport.get(url, timeout=self._timeout_seconds)
            except Exception:
                if attempt >= self._transient_retries:
                    raise ProviderError(f"Renovigi {operation} request failed") from None
                self._backoff(attempt)
                continue
            if response.status == 429 or 500 <= response.status < 600:
                if attempt >= self._transient_retries:
                    raise ProviderError(f"Renovigi {operation} request failed")
                self._backoff(attempt)
                continue
            if not 200 <= response.status < 300:
                raise ProviderError(f"Renovigi {operation} request failed")
            payload = _decode_payload(response.body, operation)
            if _error_code(payload) in _TRANSIENT_PROVIDER_CODES:
                if attempt >= self._transient_retries:
                    raise ProviderError(f"Renovigi {operation} request failed")
                self._backoff(attempt)
                continue
            return payload
        raise AssertionError("retry loop must return or raise")

    def _backoff(self, attempt: int) -> None:
        self._sleep(self._retry_backoff_seconds * (2**attempt))

    def _salt(self) -> str:
        now = self._clock()
        _require_aware(now)
        return str(round(now.timestamp() * 1000))

    def _parse_power_curve(self, payload: JsonObject, day: date) -> tuple[PowerSample, ...]:
        if not payload:
            return ()
        data = _required_mapping(payload, "dat")
        raw_samples = data.get("outputPower")
        if not isinstance(raw_samples, list):
            raise MalformedProviderResponseError("Renovigi returned malformed power data")
        samples: list[PowerSample] = []
        try:
            for raw_sample in raw_samples:
                if not isinstance(raw_sample, Mapping):
                    raise ValueError
                timestamp = datetime.strptime(
                    _required_text(raw_sample.get("ts"), "ts"), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=self._timezone)
                if timestamp.date() != day:
                    raise ValueError
                power_kw = _non_negative_number(raw_sample.get("val"), "val")
                samples.append(PowerSample(timestamp, power_kw))
        except (TypeError, ValueError):
            raise MalformedProviderResponseError("Renovigi returned malformed power data") from None
        try:
            ordered = tuple(sorted(samples, key=lambda sample: sample.observed_at))
            if len({sample.observed_at for sample in ordered}) != len(ordered):
                raise ValueError
            return ordered
        except ValueError:
            raise MalformedProviderResponseError("Renovigi returned malformed power data") from None

    @staticmethod
    def _parse_energy(payload: JsonObject) -> float | None:
        if not payload:
            return None
        data = _required_mapping(payload, "dat")
        try:
            return _non_negative_number(data.get("energy"), "energy")
        except (TypeError, ValueError):
            raise MalformedProviderResponseError(
                "Renovigi returned malformed energy data"
            ) from None


def _sha1(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _action_segment(action: str, parameters: Sequence[tuple[str, str]]) -> str:
    pairs = (("action", action), *parameters)
    return "".join(f"&{quote(key, safe='_-')}={quote(value, safe='')}" for key, value in pairs)


def _decode_payload(body: bytes, operation: str) -> JsonObject:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedProviderResponseError(
            f"Renovigi returned malformed {operation} data"
        ) from None
    if not isinstance(decoded, dict):
        raise MalformedProviderResponseError(f"Renovigi returned malformed {operation} data")
    return cast(JsonObject, decoded)


def _error_code(payload: Mapping[str, Any]) -> int:
    err = payload.get("err")
    if not isinstance(err, int) or isinstance(err, bool):
        raise MalformedProviderResponseError("Renovigi returned malformed response metadata")
    return err


def _required_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise MalformedProviderResponseError("Renovigi returned malformed response data")
    return value


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing {field}")
    return value


def _parse_device_status(data: Mapping[str, Any]) -> int:
    """Read the documented plant status or conservatively aggregate devices."""
    plant_status = data.get("status")
    if isinstance(plant_status, int) and not isinstance(plant_status, bool):
        return plant_status

    collectors = data.get("collector")
    if not isinstance(collectors, list):
        raise MalformedProviderResponseError("Renovigi returned malformed device status")
    statuses: list[int] = []
    for collector in collectors:
        if not isinstance(collector, Mapping):
            raise MalformedProviderResponseError("Renovigi returned malformed device status")
        devices = collector.get("device")
        if not isinstance(devices, list):
            raise MalformedProviderResponseError("Renovigi returned malformed device status")
        for device in devices:
            if not isinstance(device, Mapping):
                raise MalformedProviderResponseError("Renovigi returned malformed device status")
            status = device.get("status")
            if not isinstance(status, int) or isinstance(status, bool):
                raise MalformedProviderResponseError("Renovigi returned malformed device status")
            statuses.append(status)
    if not statuses:
        raise MalformedProviderResponseError("Renovigi returned malformed device status")
    if any(status == _OFFLINE_STATUS for status in statuses):
        return _OFFLINE_STATUS
    if all(status == _ONLINE_STATUS for status in statuses):
        return _ONLINE_STATUS
    return next(status for status in statuses if status != _ONLINE_STATUS)


def _non_negative_number(value: object, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(field)
    number = float(cast(str | int | float, value))
    if number < 0:
        raise ValueError(field)
    return number


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")


def _curve_is_complete(samples: Sequence[PowerSample], day: date, timezone: ZoneInfo) -> bool:
    if len(samples) != 288:
        return False
    expected = {(hour, minute) for hour in range(24) for minute in range(0, 60, 5)}
    actual = {
        (
            sample.observed_at.astimezone(timezone).hour,
            sample.observed_at.astimezone(timezone).minute,
        )
        for sample in samples
        if sample.observed_at.astimezone(timezone).date() == day
    }
    return actual == expected


def _integrate_power_curve(samples: Sequence[PowerSample]) -> float:
    """Integrate a complete five-minute curve with the trapezoidal rule."""
    return sum(
        (left.power_kw + right.power_kw)
        / 2
        * (right.observed_at - left.observed_at).total_seconds()
        / 3600
        for left, right in itertools.pairwise(samples)
    )

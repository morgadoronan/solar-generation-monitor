"""Immutable, versioned persistent state for monitoring workflows."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

SCHEMA_VERSION: Final = 1
POLICY_RETENTION_DAYS: Final = 30
HISTORY_RETENTION_DAYS: Final = 400


class StateValidationError(ValueError):
    """A persisted state envelope or payload is not safe to use."""


class FailureKind(StrEnum):
    TELEMETRY = "telemetry"
    PRODUCTION = "production"


class InterruptionKind(StrEnum):
    TELEMETRY = "telemetry"
    PRODUCTION = "production"


class EventKind(StrEnum):
    INTERRUPTION_ALERT = "interruption_alert"
    INTERRUPTION_REMINDER = "interruption_reminder"
    RECOVERY = "recovery"
    CLEANING_ALERT = "cleaning_alert"
    CLEANING_REMINDER = "cleaning_reminder"
    PROVIDER_FAILURE = "provider_failure"
    MONTHLY_REPORT = "monthly_report"


def _aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _non_blank(value: str, field: str) -> None:
    if not value.strip():
        raise ValueError(f"{field} must not be blank")


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    """Identity of the workflow execution that produced this state."""

    run_id: str
    workflow: str

    def __post_init__(self) -> None:
        _non_blank(self.run_id, "run_id")
        _non_blank(self.workflow, "workflow")


@dataclass(frozen=True, slots=True)
class ProcessedSample:
    observed_at: datetime
    provider_id: str | None = None

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if self.provider_id is not None:
            _non_blank(self.provider_id, "provider_id")


@dataclass(frozen=True, slots=True)
class FailureObservation:
    """First and latest provider observations supporting an active failure."""

    first_observed_at: datetime
    last_observed_at: datetime

    def __post_init__(self) -> None:
        _aware(self.first_observed_at, "first_observed_at")
        _aware(self.last_observed_at, "last_observed_at")
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("last_observed_at must not precede first_observed_at")


@dataclass(frozen=True, slots=True)
class AlertTimestamps:
    last_alert_at: datetime | None = None
    last_reminder_at: datetime | None = None
    last_recovery_at: datetime | None = None

    def __post_init__(self) -> None:
        for field, value in (
            ("last_alert_at", self.last_alert_at),
            ("last_reminder_at", self.last_reminder_at),
            ("last_recovery_at", self.last_recovery_at),
        ):
            if value is not None:
                _aware(value, field)


@dataclass(frozen=True, slots=True)
class PerformanceEvidence:
    day: date
    performance_ratio: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.performance_ratio) or not self.performance_ratio >= 0:
            raise ValueError("performance_ratio must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class InterruptionEvent:
    event_id: str
    kind: InterruptionKind
    started_at: datetime
    ended_at: datetime | None = None

    def __post_init__(self) -> None:
        _non_blank(self.event_id, "event_id")
        _aware(self.started_at, "started_at")
        if self.ended_at is not None:
            _aware(self.ended_at, "ended_at")
            if self.ended_at < self.started_at:
                raise ValueError("ended_at must not precede started_at")


@dataclass(frozen=True, slots=True)
class CoverageGap:
    started_at: datetime
    ended_at: datetime | None
    reason: str

    def __post_init__(self) -> None:
        _aware(self.started_at, "started_at")
        if self.ended_at is not None:
            _aware(self.ended_at, "ended_at")
            if self.ended_at < self.started_at:
                raise ValueError("ended_at must not precede started_at")
        _non_blank(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    kind: EventKind
    occurred_at: datetime

    def __post_init__(self) -> None:
        _non_blank(self.event_id, "event_id")
        _aware(self.occurred_at, "occurred_at")


@dataclass(frozen=True, slots=True)
class MonitorState:
    """Complete schema-v1 policy and reporting state."""

    generated_at: datetime
    source: SourceMetadata
    last_processed_sample: ProcessedSample | None = None
    telemetry_failure: FailureObservation | None = None
    production_failure: FailureObservation | None = None
    active_telemetry_event_id: str | None = None
    active_production_event_id: str | None = None
    active_cleaning_event_id: str | None = None
    telemetry_timestamps: AlertTimestamps = AlertTimestamps()
    production_timestamps: AlertTimestamps = AlertTimestamps()
    cleaning_timestamps: AlertTimestamps = AlertTimestamps()
    qualifying_performance: tuple[PerformanceEvidence, ...] = ()
    interruption_events: tuple[InterruptionEvent, ...] = ()
    coverage_gaps: tuple[CoverageGap, ...] = ()
    event_history: tuple[EventRecord, ...] = ()

    def __post_init__(self) -> None:
        _aware(self.generated_at, "generated_at")
        for field, value in (
            ("active_telemetry_event_id", self.active_telemetry_event_id),
            ("active_production_event_id", self.active_production_event_id),
            ("active_cleaning_event_id", self.active_cleaning_event_id),
        ):
            if value is not None:
                _non_blank(value, field)
        _require_unique(self.qualifying_performance, lambda item: item.day, "performance day")
        _require_unique(self.interruption_events, lambda item: item.event_id, "interruption event")
        _require_unique(self.event_history, lambda item: item.event_id, "event history")


def _require_unique(values: tuple[Any, ...], key: Any, label: str) -> None:
    keys = [key(value) for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {label}")


def deterministic_event_id(kind: str | StrEnum, occurred_at: datetime, discriminator: str) -> str:
    """Return a stable, non-secret logical event identifier."""

    _aware(occurred_at, "occurred_at")
    _non_blank(str(kind), "kind")
    _non_blank(discriminator, "discriminator")
    instant = _format_datetime(occurred_at)
    material = f"v1\n{kind}\n{instant}\n{discriminator}".encode()
    return f"evt_v1_{hashlib.sha256(material).hexdigest()}"


def encode_state(state: MonitorState) -> bytes:
    """Encode state in a canonical JSON envelope protected by SHA-256."""

    if type(state) is not MonitorState:
        raise TypeError("state must be a MonitorState")
    payload = _state_to_dict(state)
    checksum = hashlib.sha256(_canonical(payload)).hexdigest()
    return _canonical(
        {"schema_version": SCHEMA_VERSION, "checksum": f"sha256:{checksum}", "payload": payload}
    )


def decode_state(encoded: bytes) -> MonitorState:
    """Validate and decode one envelope; never searches for an older payload."""

    try:
        document = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StateValidationError("state is not valid JSON") from error
    try:
        root = _object(document, {"schema_version", "checksum", "payload"}, "envelope")
        version = root["schema_version"]
        if type(version) is not int or version != SCHEMA_VERSION:
            raise StateValidationError("state schema version is incompatible")
        payload = _object(root["payload"], _STATE_FIELDS, "payload")
        expected = f"sha256:{hashlib.sha256(_canonical(payload)).hexdigest()}"
        if not isinstance(root["checksum"], str) or root["checksum"] != expected:
            raise StateValidationError("state checksum does not match payload")
        return _state_from_dict(payload)
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, StateValidationError):
            raise
        raise StateValidationError("state payload is invalid") from error


def decode_or_bootstrap(
    encoded: bytes | None, *, generated_at: datetime, source: SourceMetadata
) -> MonitorState:
    """Load one candidate or conservatively start over with an open coverage gap."""

    _aware(generated_at, "generated_at")
    if encoded is not None:
        try:
            return decode_state(encoded)
        except StateValidationError:
            reason = "invalid_or_incompatible_state"
    else:
        reason = "missing_state"
    return MonitorState(
        generated_at=generated_at,
        source=source,
        coverage_gaps=(CoverageGap(generated_at, None, reason),),
    )


def prune_state(state: MonitorState, *, as_of: datetime) -> MonitorState:
    """Apply the 30-day policy and 400-day reporting retention windows."""

    _aware(as_of, "as_of")
    policy_cutoff = as_of.date() - timedelta(days=POLICY_RETENTION_DAYS)
    history_cutoff = as_of - timedelta(days=HISTORY_RETENTION_DAYS)
    return replace(
        state,
        qualifying_performance=tuple(
            item for item in state.qualifying_performance if item.day >= policy_cutoff
        ),
        interruption_events=tuple(
            item
            for item in state.interruption_events
            if item.ended_at is None or item.ended_at >= history_cutoff
        ),
        coverage_gaps=tuple(
            item
            for item in state.coverage_gaps
            if item.ended_at is None or item.ended_at >= history_cutoff
        ),
        event_history=tuple(
            item for item in state.event_history if item.occurred_at >= history_cutoff
        ),
    )


_STATE_FIELDS = {
    "generated_at",
    "source",
    "last_processed_sample",
    "telemetry_failure",
    "production_failure",
    "active_event_ids",
    "timestamps",
    "qualifying_performance",
    "interruption_events",
    "coverage_gaps",
    "event_history",
}


def _state_to_dict(state: MonitorState) -> dict[str, Any]:
    return {
        "generated_at": _format_datetime(state.generated_at),
        "source": {"run_id": state.source.run_id, "workflow": state.source.workflow},
        "last_processed_sample": _optional_sample(state.last_processed_sample),
        "telemetry_failure": _optional_failure(state.telemetry_failure),
        "production_failure": _optional_failure(state.production_failure),
        "active_event_ids": {
            "telemetry": state.active_telemetry_event_id,
            "production": state.active_production_event_id,
            "cleaning": state.active_cleaning_event_id,
        },
        "timestamps": {
            "telemetry": _timestamps_to_dict(state.telemetry_timestamps),
            "production": _timestamps_to_dict(state.production_timestamps),
            "cleaning": _timestamps_to_dict(state.cleaning_timestamps),
        },
        "qualifying_performance": [
            {"day": item.day.isoformat(), "performance_ratio": item.performance_ratio}
            for item in state.qualifying_performance
        ],
        "interruption_events": [
            {
                "event_id": item.event_id,
                "kind": item.kind.value,
                "started_at": _format_datetime(item.started_at),
                "ended_at": _optional_datetime(item.ended_at),
            }
            for item in state.interruption_events
        ],
        "coverage_gaps": [
            {
                "started_at": _format_datetime(item.started_at),
                "ended_at": _optional_datetime(item.ended_at),
                "reason": item.reason,
            }
            for item in state.coverage_gaps
        ],
        "event_history": [
            {
                "event_id": item.event_id,
                "kind": item.kind.value,
                "occurred_at": _format_datetime(item.occurred_at),
            }
            for item in state.event_history
        ],
    }


def _state_from_dict(value: dict[str, Any]) -> MonitorState:
    source = _object(value["source"], {"run_id", "workflow"}, "source")
    active = _object(
        value["active_event_ids"], {"telemetry", "production", "cleaning"}, "active_event_ids"
    )
    timestamps = _object(value["timestamps"], {"telemetry", "production", "cleaning"}, "timestamps")
    return MonitorState(
        generated_at=_datetime(value["generated_at"]),
        source=SourceMetadata(_string(source["run_id"]), _string(source["workflow"])),
        last_processed_sample=_sample(value["last_processed_sample"]),
        telemetry_failure=_failure(value["telemetry_failure"]),
        production_failure=_failure(value["production_failure"]),
        active_telemetry_event_id=_optional_string(active["telemetry"]),
        active_production_event_id=_optional_string(active["production"]),
        active_cleaning_event_id=_optional_string(active["cleaning"]),
        telemetry_timestamps=_timestamps(timestamps["telemetry"]),
        production_timestamps=_timestamps(timestamps["production"]),
        cleaning_timestamps=_timestamps(timestamps["cleaning"]),
        qualifying_performance=tuple(
            PerformanceEvidence(
                day=date.fromisoformat(_string(item["day"])),
                performance_ratio=_number(item["performance_ratio"]),
            )
            for raw in _list(value["qualifying_performance"])
            for item in [_object(raw, {"day", "performance_ratio"}, "performance evidence")]
        ),
        interruption_events=tuple(
            InterruptionEvent(
                event_id=_string(item["event_id"]),
                kind=InterruptionKind(_string(item["kind"])),
                started_at=_datetime(item["started_at"]),
                ended_at=_optional_datetime_from_json(item["ended_at"]),
            )
            for raw in _list(value["interruption_events"])
            for item in [
                _object(raw, {"event_id", "kind", "started_at", "ended_at"}, "interruption event")
            ]
        ),
        coverage_gaps=tuple(
            CoverageGap(
                started_at=_datetime(item["started_at"]),
                ended_at=_optional_datetime_from_json(item["ended_at"]),
                reason=_string(item["reason"]),
            )
            for raw in _list(value["coverage_gaps"])
            for item in [_object(raw, {"started_at", "ended_at", "reason"}, "coverage gap")]
        ),
        event_history=tuple(
            EventRecord(
                event_id=_string(item["event_id"]),
                kind=EventKind(_string(item["kind"])),
                occurred_at=_datetime(item["occurred_at"]),
            )
            for raw in _list(value["event_history"])
            for item in [_object(raw, {"event_id", "kind", "occurred_at"}, "event record")]
        ),
    )


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _format_datetime(value: datetime) -> str:
    _aware(value, "datetime")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _datetime(value: Any) -> datetime:
    raw = _string(value)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise StateValidationError("invalid timestamp") from error
    try:
        _aware(parsed, "timestamp")
    except ValueError as error:
        raise StateValidationError(str(error)) from error
    return parsed


def _optional_datetime(value: datetime | None) -> str | None:
    return None if value is None else _format_datetime(value)


def _optional_datetime_from_json(value: Any) -> datetime | None:
    return None if value is None else _datetime(value)


def _optional_sample(value: ProcessedSample | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {"observed_at": _format_datetime(value.observed_at), "provider_id": value.provider_id}


def _sample(value: Any) -> ProcessedSample | None:
    if value is None:
        return None
    item = _object(value, {"observed_at", "provider_id"}, "last processed sample")
    return ProcessedSample(_datetime(item["observed_at"]), _optional_string(item["provider_id"]))


def _optional_failure(value: FailureObservation | None) -> dict[str, str] | None:
    if value is None:
        return None
    return {
        "first_observed_at": _format_datetime(value.first_observed_at),
        "last_observed_at": _format_datetime(value.last_observed_at),
    }


def _failure(value: Any) -> FailureObservation | None:
    if value is None:
        return None
    item = _object(value, {"first_observed_at", "last_observed_at"}, "failure observation")
    return FailureObservation(
        _datetime(item["first_observed_at"]), _datetime(item["last_observed_at"])
    )


def _timestamps_to_dict(value: AlertTimestamps) -> dict[str, str | None]:
    return {
        "last_alert_at": _optional_datetime(value.last_alert_at),
        "last_reminder_at": _optional_datetime(value.last_reminder_at),
        "last_recovery_at": _optional_datetime(value.last_recovery_at),
    }


def _timestamps(value: Any) -> AlertTimestamps:
    item = _object(value, {"last_alert_at", "last_reminder_at", "last_recovery_at"}, "timestamps")
    return AlertTimestamps(
        _optional_datetime_from_json(item["last_alert_at"]),
        _optional_datetime_from_json(item["last_reminder_at"]),
        _optional_datetime_from_json(item["last_recovery_at"]),
    )


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise StateValidationError(f"{label} must be an object")
    if set(value) != fields:
        raise StateValidationError(f"{label} has missing or unknown fields")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise StateValidationError("expected a list")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise StateValidationError("expected a string")
    return value


def _optional_string(value: Any) -> str | None:
    return None if value is None else _string(value)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateValidationError("expected a number")
    number = float(value)
    if not math.isfinite(number):
        raise StateValidationError("expected a finite number")
    return number

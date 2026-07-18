from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from solar_monitor.config import Secret
from solar_monitor.state import (
    AlertTimestamps,
    CoverageGap,
    EventKind,
    EventRecord,
    InterruptionEvent,
    InterruptionKind,
    MonitorState,
    PerformanceEvidence,
    ProcessedSample,
    SourceMetadata,
    StateValidationError,
    decode_or_bootstrap,
    decode_state,
    deterministic_event_id,
    encode_state,
    prune_state,
)

NOW = datetime(2026, 7, 18, 15, 0, tzinfo=UTC)
SOURCE = SourceMetadata(run_id="42", workflow="monitor")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _state() -> MonitorState:
    interruption_id = deterministic_event_id("production", NOW - timedelta(hours=2), "plant-1")
    return MonitorState(
        generated_at=NOW,
        source=SOURCE,
        last_processed_sample=ProcessedSample(NOW - timedelta(minutes=5), "sample-9"),
        active_production_event_id=interruption_id,
        production_timestamps=AlertTimestamps(last_alert_at=NOW - timedelta(hours=1)),
        qualifying_performance=(PerformanceEvidence(date(2026, 7, 17), 0.72),),
        interruption_events=(
            InterruptionEvent(
                interruption_id,
                InterruptionKind.PRODUCTION,
                NOW - timedelta(hours=2),
            ),
        ),
        coverage_gaps=(CoverageGap(NOW - timedelta(days=3), NOW - timedelta(days=2), "missing"),),
        event_history=(EventRecord(interruption_id, EventKind.INTERRUPTION_ALERT, NOW),),
    )


def test_state_round_trip_is_canonical_and_normalizes_timestamps() -> None:
    local = timezone(timedelta(hours=-3))
    state = replace(_state(), generated_at=NOW.astimezone(local))

    encoded = encode_state(state)
    decoded = decode_state(encoded)

    assert decoded == replace(state, generated_at=NOW)
    assert encode_state(decoded) == encoded
    assert encoded.endswith(b"}")


def test_checksum_tamper_is_rejected() -> None:
    document = json.loads(encode_state(_state()))
    document["payload"]["source"]["run_id"] = "attacker"

    with pytest.raises(StateValidationError, match="checksum"):
        decode_state(json.dumps(document).encode())


def test_incompatible_version_is_rejected_before_payload_use() -> None:
    document = json.loads(encode_state(_state()))
    document["schema_version"] = 2

    with pytest.raises(StateValidationError, match="version"):
        decode_state(json.dumps(document).encode())


def test_decoder_rejects_naive_timestamp_even_with_valid_checksum() -> None:
    document = json.loads(encode_state(_state()))
    document["payload"]["generated_at"] = "2026-07-18T15:00:00"
    digest = hashlib.sha256(_canonical(document["payload"])).hexdigest()
    document["checksum"] = f"sha256:{digest}"

    with pytest.raises(StateValidationError, match="timezone-aware"):
        decode_state(_canonical(document))


@pytest.mark.parametrize(
    "factory",
    [
        lambda: MonitorState(datetime(2026, 1, 1), SOURCE),
        lambda: ProcessedSample(datetime(2026, 1, 1)),
        lambda: deterministic_event_id("production", datetime(2026, 1, 1), "plant"),
    ],
)
def test_timestamps_must_be_timezone_aware(factory: object) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        factory()  # type: ignore[operator]


def test_deterministic_event_ids_are_stable_across_timezone_offsets() -> None:
    first = deterministic_event_id("production", NOW, "plant-1")
    same_instant = deterministic_event_id(
        "production", NOW.astimezone(timezone(timedelta(hours=-3))), "plant-1"
    )

    assert first == same_instant
    assert first != deterministic_event_id("telemetry", NOW, "plant-1")
    assert first.startswith("evt_v1_")


def test_retention_keeps_active_and_boundary_records() -> None:
    old = NOW - timedelta(days=401)
    boundary = NOW - timedelta(days=400)
    state = replace(
        _state(),
        qualifying_performance=(
            PerformanceEvidence((NOW - timedelta(days=31)).date(), 0.5),
            PerformanceEvidence((NOW - timedelta(days=30)).date(), 0.6),
        ),
        interruption_events=(
            InterruptionEvent("old", InterruptionKind.TELEMETRY, old, old),
            InterruptionEvent("boundary", InterruptionKind.PRODUCTION, boundary, boundary),
            InterruptionEvent("active", InterruptionKind.PRODUCTION, old),
        ),
        coverage_gaps=(
            CoverageGap(old, old, "old"),
            CoverageGap(boundary, boundary, "boundary"),
            CoverageGap(old, None, "active"),
        ),
        event_history=(
            EventRecord("old", EventKind.RECOVERY, old),
            EventRecord("boundary", EventKind.RECOVERY, boundary),
        ),
    )

    pruned = prune_state(state, as_of=NOW)

    assert [item.day for item in pruned.qualifying_performance] == [
        (NOW - timedelta(days=30)).date()
    ]
    assert [item.event_id for item in pruned.interruption_events] == ["boundary", "active"]
    assert [item.reason for item in pruned.coverage_gaps] == ["boundary", "active"]
    assert [item.event_id for item in pruned.event_history] == ["boundary"]


@pytest.mark.parametrize("encoded", [None, b"not-json"])
def test_bootstrap_is_conservative(encoded: bytes | None) -> None:
    state = decode_or_bootstrap(encoded, generated_at=NOW, source=SOURCE)

    assert state.telemetry_failure is None
    assert state.production_failure is None
    assert state.active_telemetry_event_id is None
    assert state.active_production_event_id is None
    assert state.telemetry_timestamps.last_recovery_at is None
    assert state.production_timestamps.last_recovery_at is None
    assert state.qualifying_performance == ()
    assert state.coverage_gaps[0].started_at == NOW
    assert state.coverage_gaps[0].ended_at is None


def test_bootstrap_does_not_fall_back_or_synthesize_recovery() -> None:
    corrupt_newest = encode_state(_state())[:-1]
    state = decode_or_bootstrap(corrupt_newest, generated_at=NOW, source=SOURCE)

    assert state.event_history == ()
    assert state.interruption_events == ()
    assert state.coverage_gaps[0].reason == "invalid_or_incompatible_state"


def test_encoder_cannot_generic_serialize_secrets_or_unknown_objects() -> None:
    with pytest.raises(TypeError, match="MonitorState"):
        encode_state(Secret("canary-secret"))  # type: ignore[arg-type]
    assert b"canary-secret" not in encode_state(_state())

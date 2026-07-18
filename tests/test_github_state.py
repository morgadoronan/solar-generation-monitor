from __future__ import annotations

import io
import json
import zipfile
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from solar_monitor.github_state import (
    STATE_FILENAME,
    GitHubArtifactStateStore,
    HttpResponse,
    LocalFileStateStore,
)
from solar_monitor.state import MonitorState, SourceMetadata, decode_or_bootstrap, encode_state


def zipped(payload: bytes, name: str = STATE_FILENAME) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr(name, payload)
    return output.getvalue()


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = deque(responses)
        self.urls: list[str] = []

    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
        self.urls.append(url)
        return self.responses.popleft()


def artifact(identifier: int, run: int, created: str) -> dict[str, object]:
    return {
        "id": identifier,
        "name": "solar-monitor-state-v1",
        "expired": False,
        "created_at": created,
        "archive_download_url": f"https://download/{identifier}",
        "workflow_run": {"id": run},
    }


def response(value: object) -> HttpResponse:
    return HttpResponse(200, json.dumps(value).encode())


def run(name: str = "Solar Monitor", branch: str = "main") -> HttpResponse:
    return response(
        {"status": "completed", "conclusion": "success", "head_branch": branch, "name": name}
    )


def store(
    tmp_path: Path, transport: FakeTransport, *, current_run_id: str = "99"
) -> GitHubArtifactStateStore:
    return GitHubArtifactStateStore(
        repository="owner/repo",
        token="canary",
        current_run_id=current_run_id,
        default_branch="main",
        allowed_workflows={"Solar Monitor"},
        output_path=tmp_path / STATE_FILENAME,
        transport=transport,
    )


def state_payload(run_id: str, workflow: str = "Solar Monitor") -> bytes:
    return encode_state(
        MonitorState(
            generated_at=datetime(2026, 7, 18, tzinfo=UTC),
            source=SourceMetadata(run_id, workflow),
        )
    )


def test_filters_runs_then_downloads_newest_verified_candidate(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            response(
                {
                    "artifacts": [
                        artifact(3, 30, "2026-07-03T00:00:00Z"),
                        artifact(2, 20, "2026-07-02T00:00:00Z"),
                    ]
                }
            ),
            run(branch="feature"),
            run(),
            HttpResponse(200, zipped(state_payload("20"))),
        ]
    )

    assert store(tmp_path, transport).load_latest() == state_payload("20")
    assert transport.urls[-1] == "https://download/2"


def test_paginates_and_excludes_current_and_expired_artifacts(tmp_path: Path) -> None:
    first_page = [
        artifact(identifier, 1_000 + identifier, "2026-07-01T00:00:00Z")
        for identifier in range(1, 101)
    ]
    current = artifact(101, 99, "2026-07-20T00:00:00Z")
    expired = artifact(102, 102, "2026-07-21T00:00:00Z")
    expired["expired"] = True
    chosen = artifact(103, 103, "2026-07-22T00:00:00Z")
    transport = FakeTransport(
        [
            response({"artifacts": first_page}),
            response({"artifacts": [current, expired, chosen]}),
            run(),
            HttpResponse(200, zipped(state_payload("103"))),
        ]
    )

    assert store(tmp_path, transport).load_latest() == state_payload("103")
    assert any("page=2" in url for url in transport.urls)
    assert not any(url.endswith("/runs/99") or url.endswith("/runs/102") for url in transport.urls)
    assert [url for url in transport.urls if "/actions/runs/" in url] == [
        "https://api.github.com/repos/owner/repo/actions/runs/103"
    ]


def test_stops_run_verification_after_first_newest_eligible_candidate(tmp_path: Path) -> None:
    candidates = [
        artifact(identifier, identifier, "2026-07-01T00:00:00Z")
        for identifier in range(1, 101)
    ]
    transport = FakeTransport(
        [
            response({"artifacts": candidates}),
            response({"artifacts": []}),
            run(branch="feature"),
            run(),
            HttpResponse(200, zipped(state_payload("99"))),
        ]
    )

    assert store(tmp_path, transport, current_run_id="999").load_latest() == state_payload("99")
    run_urls = [url for url in transport.urls if "/actions/runs/" in url]
    assert run_urls == [
        "https://api.github.com/repos/owner/repo/actions/runs/100",
        "https://api.github.com/repos/owner/repo/actions/runs/99",
    ]


def test_excludes_dry_run_artifact_namespace(tmp_path: Path) -> None:
    dry_run = artifact(1, 10, "2026-07-01T00:00:00Z")
    dry_run["name"] = "solar-monitor-state-v1-dry-run"
    transport = FakeTransport([response({"artifacts": [dry_run]})])

    assert store(tmp_path, transport).load_latest() is None
    assert all(not url.endswith("/runs/10") for url in transport.urls)


def test_rejects_unsuccessful_incomplete_wrong_branch_and_wrong_workflow_runs(
    tmp_path: Path,
) -> None:
    candidates = [
        artifact(1, 1, "2026-07-01T00:00:00Z"),
        artifact(2, 2, "2026-07-02T00:00:00Z"),
        artifact(3, 3, "2026-07-03T00:00:00Z"),
        artifact(4, 4, "2026-07-04T00:00:00Z"),
    ]
    transport = FakeTransport(
        [
            response({"artifacts": candidates}),
            response(
                {
                    "status": "completed",
                    "conclusion": "failure",
                    "head_branch": "main",
                    "name": "Solar Monitor",
                }
            ),
            response(
                {
                    "status": "in_progress",
                    "conclusion": None,
                    "head_branch": "main",
                    "name": "Solar Monitor",
                }
            ),
            run(branch="feature"),
            run(name="Untrusted Workflow"),
        ]
    )

    assert store(tmp_path, transport).load_latest() is None
    assert all(not url.startswith("https://download/") for url in transport.urls)


def test_newest_verified_corrupt_archive_bootstraps_without_older_fallback(
    tmp_path: Path,
) -> None:
    transport = FakeTransport(
        [
            response(
                {
                    "artifacts": [
                        artifact(2, 20, "2026-07-02T00:00:00Z"),
                        artifact(1, 10, "2026-07-01T00:00:00Z"),
                    ]
                }
            ),
            run(),
            HttpResponse(200, b"not-a-zip"),
        ]
    )

    encoded = store(tmp_path, transport).load_latest()
    assert encoded == b""
    assert transport.urls[-1] == "https://download/2"
    assert "https://download/1" not in transport.urls
    restored = decode_or_bootstrap(
        encoded,
        generated_at=datetime(2026, 7, 18, tzinfo=UTC),
        source=SourceMetadata("99", "Solar Monitor"),
    )
    assert restored.coverage_gaps[0].reason == "invalid_or_incompatible_state"


def test_source_metadata_must_match_selected_workflow_run(tmp_path: Path) -> None:
    for payload in (state_payload("other"), state_payload("20", "Another Workflow")):
        transport = FakeTransport(
            [
                response({"artifacts": [artifact(2, 20, "2026-07-02T00:00:00Z")]}),
                run(),
                HttpResponse(200, zipped(payload)),
            ]
        )

        assert store(tmp_path, transport).load_latest() == b""


def test_invalid_state_payload_is_returned_for_conservative_bootstrap(tmp_path: Path) -> None:
    transport = FakeTransport(
        [
            response({"artifacts": [artifact(2, 20, "2026-07-02T00:00:00Z")]}),
            run(),
            HttpResponse(200, zipped(b"invalid-state")),
        ]
    )

    assert store(tmp_path, transport).load_latest() == b"invalid-state"


def test_local_output_is_atomic_and_round_trips(tmp_path: Path) -> None:
    local = LocalFileStateStore(tmp_path / "nested" / "state.json")
    assert local.load_latest() is None
    local.save(b"payload")
    assert local.load_latest() == b"payload"

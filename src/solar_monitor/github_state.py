"""GitHub Actions artifact discovery and local state output adapters."""

from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from solar_monitor.state import StateValidationError, decode_state

ARTIFACT_NAME = "solar-monitor-state-v1"
STATE_FILENAME = "solar-monitor-state-v1.json"
LEGACY_STATE_FILENAME = "solar-monitor-state.json"


class GitHubStateError(RuntimeError):
    """A sanitized GitHub state access failure."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse: ...


class _CrossOriginRedirectHandler(HTTPRedirectHandler):
    """Never forward GitHub credentials to a signed artifact storage host."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            redirected is not None
            and urlsplit(req.full_url).netloc.lower() != urlsplit(newurl).netloc.lower()
        ):
            redirected.remove_header("Authorization")
        return redirected


class UrllibTransport:
    def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
        try:
            opener = build_opener(_CrossOriginRedirectHandler())
            with opener.open(Request(url, headers=headers), timeout=timeout) as response:
                return HttpResponse(response.status, response.read())
        except HTTPError as error:
            return HttpResponse(error.code, error.read())
        except (OSError, URLError) as error:
            raise GitHubStateError("GitHub state request failed") from error


class MemoryStateStore:
    """Small fake-safe store; a new instance is an isolated dry-run state."""

    def __init__(self, initial: bytes | None = None) -> None:
        self.payload = initial

    def load_latest(self) -> bytes | None:
        return self.payload

    def save(self, payload: bytes) -> None:
        self.payload = bytes(payload)


class LocalFileStateStore:
    """Atomic filesystem state store for local operation and tests."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load_latest(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def save(self, payload: bytes) -> None:
        _atomic_write(self.path, payload)


class GitHubArtifactStateStore:
    """Read one verified predecessor artifact and emit an atomic upload file."""

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        current_run_id: str,
        default_branch: str,
        allowed_workflows: set[str],
        output_path: Path,
        transport: HttpTransport | None = None,
        api_url: str = "https://api.github.com",
        timeout: float = 20.0,
    ) -> None:
        if "/" not in repository or not allowed_workflows:
            raise ValueError("repository and allowed_workflows must be configured")
        self._repository = repository
        self._token = token
        self._current_run_id = current_run_id
        self._default_branch = default_branch
        self._allowed_workflows = allowed_workflows
        self._output_path = output_path
        self._transport = transport or UrllibTransport()
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout

    def load_latest(self) -> bytes | None:
        candidates = sorted(
            self._list_candidates(),
            key=lambda item: (str(item["created_at"]), int(item["id"])),
            reverse=True,
        )
        for candidate in candidates:
            run_id = str(candidate["workflow_run"]["id"])
            run = self._json_get(f"{self._api_url}/repos/{self._repository}/actions/runs/{run_id}")
            workflow_name = run.get("name") or run.get("path")
            if (
                run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and run.get("head_branch") == self._default_branch
                and isinstance(workflow_name, str)
                and workflow_name in self._allowed_workflows
            ):
                response = self._get(str(candidate["archive_download_url"]))
                if response.status != 200:
                    raise GitHubStateError("GitHub state artifact download failed")
                try:
                    payload = _extract_state(response.body)
                except GitHubStateError:
                    # The newest verified artifact is authoritative. Returning an invalid
                    # candidate makes the state layer bootstrap with a coverage gap rather
                    # than silently rolling back to an older artifact.
                    return b""
                try:
                    state = decode_state(payload)
                except StateValidationError:
                    return payload
                if state.source.run_id != run_id or state.source.workflow != workflow_name:
                    return b""
                return payload
        return None

    def save(self, payload: bytes) -> None:
        # upload-artifact publishes this file; this adapter never creates artifacts itself.
        _atomic_write(self._output_path, payload)

    def _list_candidates(self) -> list[dict[str, Any]]:
        page = 1
        candidates: list[dict[str, Any]] = []
        while True:
            query = urlencode({"name": ARTIFACT_NAME, "per_page": "100", "page": str(page)})
            payload = self._json_get(
                f"{self._api_url}/repos/{self._repository}/actions/artifacts?{query}"
            )
            artifacts = payload.get("artifacts")
            if not isinstance(artifacts, list):
                raise GitHubStateError("GitHub artifact listing was malformed")
            for raw in artifacts:
                if not isinstance(raw, dict):
                    raise GitHubStateError("GitHub artifact listing was malformed")
                workflow_run = raw.get("workflow_run")
                if (
                    raw.get("name") == ARTIFACT_NAME
                    and raw.get("expired") is False
                    and isinstance(workflow_run, dict)
                    and isinstance(workflow_run.get("id"), int)
                    and str(workflow_run.get("id")) != self._current_run_id
                    and "dry-run" not in str(raw.get("name", "")).lower()
                    and all(key in raw for key in ("id", "created_at", "archive_download_url"))
                ):
                    candidates.append(raw)
            if len(artifacts) < 100:
                break
            page += 1
        return candidates

    def _json_get(self, url: str) -> dict[str, Any]:
        response = self._get(url)
        if response.status != 200:
            raise GitHubStateError("GitHub state request was rejected")
        try:
            payload = json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GitHubStateError("GitHub state response was malformed") from error
        if not isinstance(payload, dict):
            raise GitHubStateError("GitHub state response was malformed")
        return payload

    def _get(self, url: str) -> HttpResponse:
        try:
            return self._transport.get(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=self._timeout,
            )
        except GitHubStateError:
            raise
        except Exception as error:
            raise GitHubStateError("GitHub state request failed") from error


def _extract_state(archive: bytes) -> bytes:
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            safe_files = [
                info
                for info in bundle.infolist()
                if not info.is_dir()
                and not PurePosixPath(info.filename).is_absolute()
                and ".." not in PurePosixPath(info.filename).parts
            ]
            matches = [
                info
                for info in safe_files
                if info.filename in {STATE_FILENAME, LEGACY_STATE_FILENAME}
            ]
            if len(matches) != 1 or len(safe_files) != 1:
                raise GitHubStateError("GitHub state artifact contents were invalid")
            info = matches[0]
            if info.file_size > 5_000_000:
                raise GitHubStateError("GitHub state artifact was too large")
            return bundle.read(info)
    except (zipfile.BadZipFile, OSError) as error:
        raise GitHubStateError("GitHub state artifact was corrupt") from error


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
        raise

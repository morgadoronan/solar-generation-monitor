"""Repository-level checks that keep secrets and mutable Actions out of source."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEXT_SUFFIXES = {
    "",
    ".csv",
    ".env",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".yaml",
    ".yml",
}

# Build signatures in pieces so the scanner does not flag its own definitions.
HIGH_RISK_SIGNATURES = {
    "credential embedded in URL": re.compile(
        r"https?://[^\s/:@]+:[^\s/@]+@", re.IGNORECASE
    ),
    "private key": re.compile("-----BEGIN " + r"(?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\bgh" + r"[pousr]_[A-Za-z0-9]{36,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub" + r"_pat_[A-Za-z0-9_]{40,}\b"),
    "AWS access key": re.compile(r"\bAK" + r"IA[0-9A-Z]{16}\b"),
    "Slack token": re.compile(r"\bxox" + r"[aboprs]-[A-Za-z0-9-]{20,}\b"),
    "Telegram bot token": re.compile(r"(?<![\w-])\d{8,12}:[A-Za-z0-9_-]{30,}(?![\w-])"),
}


def _repository_text_files() -> list[Path]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    paths = []
    for raw_path in result.stdout.decode().split("\0"):
        if not raw_path:
            continue
        path = ROOT / raw_path
        if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
            paths.append(path)
    return paths


def test_tracked_source_docs_and_fixtures_contain_no_high_risk_secrets() -> None:
    findings: list[str] = []
    for path in _repository_text_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for label, signature in HIGH_RISK_SIGNATURES.items():
            if signature.search(text):
                findings.append(f"{path.relative_to(ROOT)}: {label}")
    assert not findings, "Potential secrets found:\n" + "\n".join(findings)


def test_all_third_party_actions_are_pinned_to_full_commit_shas() -> None:
    unpinned: list[str] = []
    action_line = re.compile(r"^\s*uses:\s*([^\s#]+)", re.MULTILINE)
    full_pin = re.compile(r"^[^@]+@[0-9a-f]{40}$")

    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for reference in action_line.findall(path.read_text(encoding="utf-8")):
            if reference.startswith("./"):
                continue
            if not full_pin.fullmatch(reference):
                unpinned.append(f"{path.name}: {reference}")

    assert not unpinned, "Unpinned third-party Actions:\n" + "\n".join(unpinned)

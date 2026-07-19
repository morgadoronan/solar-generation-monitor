from __future__ import annotations

import json

import pytest

from solar_monitor.cli import build_parser, main
from solar_monitor.github_state import GitHubStateError
from solar_monitor.ports import InvalidCredentialsError, ProviderError, SessionExpiredError
from solar_monitor.telegram import TelegramDeliveryError


def environment() -> dict[str, str]:
    return {
        "RENOVIGI_USERNAME": "user",
        "RENOVIGI_PASSWORD": "password-canary",
        "RENOVIGI_PLANT_ID": "plant",
        "SOLAR_LATITUDE": "-16.65",
        "SOLAR_LONGITUDE": "-49.49",
        "SOLAR_TIMEZONE": "America/Sao_Paulo",
    }


def test_commands_are_exactly_the_public_contract() -> None:
    parser = build_parser()
    action = next(item for item in parser._actions if item.dest == "command")
    assert set(action.choices) == {"calibrate", "monitor", "evaluate-day", "report-month"}  # type: ignore[attr-defined]


def test_production_command_is_inert_and_output_is_secret_safe(capsys: object) -> None:
    code = main(["monitor"], env=environment())
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert code == 0
    assert json.loads(output)["status"] == "disabled"
    assert "password-canary" not in output


def test_all_dry_run_commands_need_no_env_or_external_adapters(
    monkeypatch: object, capsys: object, tmp_path: object
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("external adapter was instantiated")

    for name in (
        "RenovigiClient",
        "OpenMeteoClient",
        "TelegramNotifier",
        "GitHubArtifactStateStore",
    ):
        monkeypatch.setattr(f"solar_monitor.cli.{name}", forbidden)  # type: ignore[attr-defined]
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]

    commands = (
        ["calibrate", "--date", "2026-06-07", "--dry-run"],
        ["monitor", "--at", "2026-06-07T02:00:00-03:00", "--dry-run"],
        ["evaluate-day", "--date", "2026-06-07", "--dry-run"],
        ["report-month", "--month", "2026-06", "--dry-run"],
    )
    for command in commands:
        assert main(command, env={}) == 0
    output = capsys.readouterr().out  # type: ignore[attr-defined]

    assert len(output.splitlines()) == 4
    assert list(tmp_path.iterdir()) == []  # type: ignore[attr-defined]


def test_primary_date_and_month_flags_parse() -> None:
    parser = build_parser()
    assert parser.parse_args(["calibrate", "--date", "2026-06-07"]).date.isoformat() == (
        "2026-06-07"
    )
    assert parser.parse_args(["evaluate-day", "--date", "2026-06-08"]).date.isoformat() == (
        "2026-06-08"
    )
    assert parser.parse_args(["report-month", "--month", "2026-06"]).month.isoformat() == (
        "2026-06-01"
    )
    assert parser.parse_args(
        ["report-month", "--force-notification"]
    ).force_notification is True


def test_configuration_errors_have_a_safe_operational_category(capsys: object) -> None:
    assert main(["calibrate"], env={}) == 2
    output = capsys.readouterr().out  # type: ignore[attr-defined]

    assert json.loads(output) == {"reason": "configuration_error", "status": "error"}


@pytest.mark.parametrize(
    ("error", "reason"),
    (
        (GitHubStateError("unsafe state detail"), "state_error"),
        (InvalidCredentialsError("unsafe auth detail"), "renovigi_auth_error"),
        (SessionExpiredError("unsafe session detail"), "renovigi_auth_error"),
        (TelegramDeliveryError("unsafe telegram detail"), "telegram_error"),
        (ProviderError("unsafe provider detail"), "provider_error"),
        (ValueError("unsafe validation detail"), "validation_error"),
    ),
)
def test_runtime_errors_emit_only_safe_categories(
    error: Exception, reason: str, monkeypatch: object, capsys: object
) -> None:
    values = {
        **environment(),
        "ENABLE_LIVE_MONITORING": "true",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "chat",
    }

    def fail(*args: object, **kwargs: object) -> object:
        raise error

    monkeypatch.setattr("solar_monitor.cli._state_store", fail)  # type: ignore[attr-defined]

    assert main(["monitor"], env=values) == 2
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert json.loads(output) == {"reason": reason, "status": "error"}
    assert "unsafe" not in output

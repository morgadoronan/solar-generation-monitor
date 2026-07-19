"""Argparse command line entry point with environment-only wiring."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from solar_monitor.config import AppConfig, ConfigError
from solar_monitor.dry_run import (
    DRY_RUN_LATITUDE,
    DRY_RUN_LONGITUDE,
    DRY_RUN_PLANT_ID,
    DRY_RUN_TIMEZONE,
    SyntheticProvider,
)
from solar_monitor.github_state import (
    GitHubArtifactStateStore,
    GitHubStateError,
    LocalFileStateStore,
    MemoryStateStore,
)
from solar_monitor.open_meteo import OpenMeteoClient
from solar_monitor.ports import (
    InvalidCredentialsError,
    ProviderError,
    SessionExpiredError,
    StateStore,
)
from solar_monitor.renovigi import RenovigiClient
from solar_monitor.service import (
    CalibrationService,
    DailyEvaluationService,
    MonitorService,
    MonthlyReportService,
)
from solar_monitor.state import SourceMetadata
from solar_monitor.telegram import TelegramDeliveryError, TelegramNotifier


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="solar-monitor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    calibrate = subparsers.add_parser("calibrate")
    calibrate.add_argument("--date", "--day", dest="date", type=date.fromisoformat)
    calibrate.add_argument("--dry-run", action="store_true")
    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("--at", type=datetime.fromisoformat)
    monitor.add_argument("--dry-run", action="store_true")
    evaluate = subparsers.add_parser("evaluate-day")
    evaluate.add_argument("--date", "--day", dest="date", type=date.fromisoformat)
    evaluate.add_argument("--dry-run", action="store_true")
    report = subparsers.add_parser("report-month")
    report.add_argument("--month", type=_month)
    report.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    values = dict(os.environ if env is None else env)
    dry_run = bool(args.dry_run)
    if dry_run:
        try:
            return _emit(_json_value(_run_dry(args)), values)
        except ValueError:
            return _emit(
                {"status": "error", "reason": "validation_error"}, values, code=2
            )
    try:
        config = AppConfig.from_env(values)
        if args.command != "calibrate" and not dry_run and not config.live_monitoring_enabled:
            return _emit({"status": "disabled", "reason": "live monitoring is not enabled"}, values)
        generation = RenovigiClient(config.generation, config.location.timezone)
        weather = OpenMeteoClient(
            latitude=config.location.latitude,
            longitude=config.location.longitude,
            timezone=config.location.timezone,
        )
        notifier = None if config.telegram is None else TelegramNotifier(config.telegram)
        store = _state_store(values)
        source = SourceMetadata(
            values.get("GITHUB_RUN_ID", "local"), values.get("GITHUB_WORKFLOW", args.command)
        )
        if args.command == "calibrate":
            result: object = CalibrationService(
                generation, weather, baseline_day=config.baseline_day
            ).run(args.date)
        elif args.command == "monitor":
            result = MonitorService(
                generation,
                weather,
                store,
                source=source,
                plant_id=config.generation.plant_id,
                latitude=config.location.latitude,
                longitude=config.location.longitude,
                timezone=config.location.timezone,
                notifier=notifier,
            ).run(args.at)
        elif args.command == "evaluate-day":
            result = DailyEvaluationService(
                generation,
                weather,
                store,
                source=source,
                plant_id=config.generation.plant_id,
                timezone=config.location.timezone,
                notifier=notifier,
                baseline_day=config.baseline_day,
            ).run(args.date)
        else:
            result = MonthlyReportService(
                generation,
                weather,
                store,
                source=source,
                plant_id=config.generation.plant_id,
                timezone=config.location.timezone,
                notifier=notifier,
                baseline_day=config.baseline_day,
                energy_rate_brl_per_kwh=config.energy_rate_brl_per_kwh,
                csv_path=Path(
                    values.get(
                        "SOLAR_REPORT_OUTPUT_PATH",
                        values.get("SOLAR_REPORT_PATH", "solar-monitor-report.csv"),
                    )
                ),
            ).run(args.month)
        return _emit(_json_value(result), values)
    except ConfigError:
        reason = "configuration_error"
    except GitHubStateError:
        reason = "state_error"
    except (InvalidCredentialsError, SessionExpiredError):
        reason = "renovigi_auth_error"
    except TelegramDeliveryError:
        reason = "telegram_error"
    except ProviderError:
        reason = "provider_error"
    except ValueError:
        reason = "validation_error"
    return _emit({"status": "error", "reason": reason}, values, code=2)


def _state_store(env: dict[str, str]) -> StateStore:
    repository = env.get("GITHUB_REPOSITORY")
    token = env.get("GITHUB_TOKEN")
    run_id = env.get("GITHUB_RUN_ID")
    if repository and token and run_id:
        workflows = {
            item.strip()
            for item in env.get(
                "SOLAR_STATE_WORKFLOWS", "Solar Monitor,Solar Daily,Solar Monthly"
            ).split(",")
            if item.strip()
        }
        return GitHubArtifactStateStore(
            repository=repository,
            token=token,
            current_run_id=run_id,
            default_branch=env.get("DEFAULT_BRANCH", env.get("GITHUB_DEFAULT_BRANCH", "main")),
            allowed_workflows=workflows,
            output_path=Path(
                env.get(
                    "SOLAR_STATE_OUTPUT_PATH",
                    env.get("SOLAR_STATE_OUTPUT", "solar-monitor-state-v1.json"),
                )
            ),
        )
    return LocalFileStateStore(Path(env.get("SOLAR_STATE_PATH", ".solar-monitor-state.json")))


def _run_dry(args: argparse.Namespace) -> object:
    provider = SyntheticProvider()
    store = MemoryStateStore()
    source = SourceMetadata("dry-run", f"dry-run-{args.command}")
    if args.command == "calibrate":
        return CalibrationService(provider, provider).run(args.date)
    if args.command == "monitor":
        return MonitorService(
            provider,
            provider,
            store,
            source=source,
            plant_id=DRY_RUN_PLANT_ID,
            latitude=DRY_RUN_LATITUDE,
            longitude=DRY_RUN_LONGITUDE,
            timezone=DRY_RUN_TIMEZONE,
        ).run(args.at)
    if args.command == "evaluate-day":
        return DailyEvaluationService(
            provider,
            provider,
            store,
            source=source,
            plant_id=DRY_RUN_PLANT_ID,
            timezone=DRY_RUN_TIMEZONE,
        ).run(args.date)
    return MonthlyReportService(
        provider,
        provider,
        store,
        source=source,
        plant_id=DRY_RUN_PLANT_ID,
        timezone=DRY_RUN_TIMEZONE,
        csv_path=None,
    ).run(args.month)


def _month(value: str) -> date:
    try:
        parsed = datetime.strptime(value, "%Y-%m").date()
    except ValueError as error:
        raise argparse.ArgumentTypeError("month must use YYYY-MM") from error
    return parsed


def _json_value(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError("unsupported command result")


def _emit(payload: object, env: dict[str, str], *, code: int = 0) -> int:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    print(rendered)
    summary = env.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with Path(summary).open("a", encoding="utf-8") as handle:
                handle.write(f"`{rendered}`\n")
        except OSError:
            pass
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

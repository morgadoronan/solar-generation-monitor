"""Sanitized monthly report calculations and CSV rendering."""

from __future__ import annotations

import csv
import io
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DayReport:
    day: date
    energy_kwh: float | None
    performance_ratio: float | None
    qualifying: bool
    complete: bool


@dataclass(frozen=True, slots=True)
class MonthlyReport:
    month: date
    total_kwh: float
    best_day: DayReport | None
    worst_day: DayReport | None
    interruption_hours: float
    availability_percent: float
    coverage_complete: bool
    coverage_gap_count: int
    days: tuple[DayReport, ...]

    @property
    def telegram_text(self) -> str:
        best = _day_summary(self.best_day)
        worst = _day_summary(self.worst_day)
        coverage = "complete" if self.coverage_complete else "incomplete"
        return (
            f"{self.month:%B %Y}: {self.total_kwh:.2f} kWh generated. "
            f"Availability {self.availability_percent:.2f}% with "
            f"{self.interruption_hours:.2f} interruption hours. "
            f"Best qualifying day {best}; worst {worst}. Coverage {coverage} "
            f"({self.coverage_gap_count} gap(s))."
        )


def render_csv(report: MonthlyReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["date", "energy_kwh", "performance_ratio", "qualifying", "complete"])
    for item in report.days:
        writer.writerow(
            [
                item.day.isoformat(),
                "" if item.energy_kwh is None else f"{item.energy_kwh:.3f}",
                "" if item.performance_ratio is None else f"{item.performance_ratio:.4f}",
                str(item.qualifying).lower(),
                str(item.complete).lower(),
            ]
        )
    return output.getvalue().encode("utf-8")


def write_csv_atomic(path: Path, report: MonthlyReport) -> None:
    payload = render_csv(report)
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


def previous_month(now: datetime) -> tuple[date, date]:
    """Return inclusive first and exclusive end of the prior local month."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = now.date().replace(day=1)
    prior_last = current.fromordinal(current.toordinal() - 1)
    return prior_last.replace(day=1), current


def _day_summary(value: DayReport | None) -> str:
    if value is None or value.performance_ratio is None:
        return "n/a"
    return f"{value.day.isoformat()} ({value.performance_ratio:.0%})"

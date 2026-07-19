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
    expected_energy_kwh: float | None = None
    generation_variance_percent: float | None = None
    estimated_value_brl: float | None = None
    no_direct_sun_hours: float | None = None


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
    expected_energy_kwh: float | None = None
    generation_variance_percent: float | None = None
    estimated_value_brl: float | None = None
    no_direct_sun_hours: float | None = None
    climate_data_days: int = 0
    performance_data_days: int = 0

    @property
    def telegram_text(self) -> str:
        best = _day_summary(self.best_day)
        worst = _day_summary(self.worst_day)
        generation_days = sum(item.energy_kwh is not None for item in self.days)
        total_days = len(self.days)
        energy = (
            _decimal(self.total_kwh, 2) + " kWh"
            if generation_days
            else "unavailable"
        )
        expected = _optional_measure(self.expected_energy_kwh, "kWh", 2)
        sunlight = _optional_measure(self.no_direct_sun_hours, "h", 1)
        lines = [
            f"☀️ Solar Report — {_month_name(self.month.month)} {self.month.year}",
            "",
            f"⚡ Energy generated: {energy}",
            f"🎯 Climate-adjusted potential: {expected}",
            _performance_summary(self.generation_variance_percent),
            f"📐 Performance comparison: {self.performance_data_days}/{total_days} days",
        ]
        if self.estimated_value_brl is not None:
            lines.append(f"💰 Estimated savings: R$ {_decimal(self.estimated_value_brl, 2)}")
        lines.extend(
            [
                f"☁️ Hours without direct sunlight: {sunlight}",
                f"🌤️ Climate data: {self.climate_data_days}/{total_days} days",
                "",
                f"🟢 Availability: {_decimal(self.availability_percent, 2)}%",
                f"⏸️ Interruptions: {_decimal(self.interruption_hours, 1)} h",
                f"🏆 Best day: {best}",
                f"📉 Worst day: {worst}",
                "",
                f"📊 Data: {generation_days}/{total_days} generation days",
                (
                    "✅ Complete coverage"
                    if self.coverage_complete
                    else f"⚠️ Partial coverage: {self.coverage_gap_count} gap(s)"
                ),
            ]
        )
        return "\n".join(lines)


def render_csv(report: MonthlyReport) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(
        [
            "date",
            "energy_kwh",
            "performance_ratio",
            "qualifying",
            "complete",
            "expected_energy_kwh",
            "generation_variance_percent",
            "estimated_value_brl",
            "no_direct_sun_hours",
        ]
    )
    for item in report.days:
        writer.writerow(
            [
                item.day.isoformat(),
                "" if item.energy_kwh is None else f"{item.energy_kwh:.3f}",
                "" if item.performance_ratio is None else f"{item.performance_ratio:.4f}",
                str(item.qualifying).lower(),
                str(item.complete).lower(),
                "" if item.expected_energy_kwh is None else f"{item.expected_energy_kwh:.3f}",
                (
                    ""
                    if item.generation_variance_percent is None
                    else f"{item.generation_variance_percent:.4f}"
                ),
                "" if item.estimated_value_brl is None else f"{item.estimated_value_brl:.2f}",
                (
                    ""
                    if item.no_direct_sun_hours is None
                    else f"{item.no_direct_sun_hours:.3f}"
                ),
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
        return "unavailable"
    return f"{value.day.isoformat()} — {_decimal(value.performance_ratio * 100, 0)}%"


_MONTH_NAMES = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _month_name(month: int) -> str:
    return _MONTH_NAMES[month]


def _decimal(value: float, places: int) -> str:
    return f"{value:.{places}f}"


def _optional_measure(value: float | None, unit: str, places: int) -> str:
    if value is None:
        return "unavailable"
    return f"{_decimal(value, places)} {unit}"


def _performance_summary(value: float | None) -> str:
    if value is None:
        return "📊 Performance: unavailable"
    if value < 0:
        return f"📉 Performance: {_decimal(abs(value), 2)}% below potential"
    if value > 0:
        return f"📈 Performance: {_decimal(value, 2)}% above potential"
    return "🎯 Performance: at expected potential"

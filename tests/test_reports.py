from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solar_monitor.dry_run import DRY_RUN_PLANT_ID, DRY_RUN_TIMEZONE, SyntheticProvider
from solar_monitor.github_state import MemoryStateStore
from solar_monitor.models import GenerationDay, Notification, WeatherDay
from solar_monitor.reports import DayReport, MonthlyReport, previous_month, render_csv
from solar_monitor.service import MonthlyReportService
from solar_monitor.state import (
    CoverageGap,
    InterruptionEvent,
    InterruptionKind,
    MonitorState,
    SourceMetadata,
    encode_state,
)

ZONE = ZoneInfo("America/Sao_Paulo")


class CompleteMonthlyProvider:
    def get_generation_day(self, day: date) -> GenerationDay:
        return GenerationDay(day, (), 10.0, True)

    def get_weather_day(self, day: date) -> WeatherDay:
        sunrise = datetime(day.year, day.month, day.day, 6, tzinfo=ZONE)
        return WeatherDay(day, sunrise, sunrise.replace(hour=18), 43200, 43200, 0, 20, True)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Notification] = []

    def send(self, notification: Notification) -> None:
        self.events.append(notification)


class SaveSpyStore(MemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.saved_payloads: list[bytes] = []

    def save(self, payload: bytes) -> None:
        self.saved_payloads.append(bytes(payload))
        super().save(payload)


def test_previous_month_handles_year_boundary() -> None:
    start, end = previous_month(datetime(2026, 1, 15, tzinfo=ZoneInfo("America/Sao_Paulo")))
    assert start.isoformat() == "2025-12-01"
    assert end.isoformat() == "2026-01-01"


def test_csv_has_fixed_sanitized_columns() -> None:
    start, _ = previous_month(datetime(2026, 7, 1, tzinfo=ZoneInfo("America/Sao_Paulo")))
    day = DayReport(start, 12.3456, 0.81234, True, True)
    report = MonthlyReport(start, 12.3456, day, day, 0, 100, True, 0, (day,))
    csv = render_csv(report).decode()
    assert csv.splitlines()[0] == "date,energy_kwh,performance_ratio,qualifying,complete"
    assert "12.346,0.8123,true,true" in csv


def test_monthly_service_accepts_only_completed_calendar_months() -> None:
    provider = SyntheticProvider()
    service = MonthlyReportService(
        provider,
        provider,
        MemoryStateStore(),
        source=SourceMetadata("dry", "report"),
        plant_id=DRY_RUN_PLANT_ID,
        timezone=DRY_RUN_TIMEZONE,
        csv_path=None,
        clock=lambda: datetime(2026, 7, 18, 12, tzinfo=DRY_RUN_TIMEZONE),
    )

    report = service.run(date(2026, 5, 1))

    assert report.month == date(2026, 5, 1)
    assert len(report.days) == 31
    with pytest.raises(ValueError, match="completed"):
        service.run(date(2026, 7, 1))
    with pytest.raises(ValueError, match="first day"):
        service.run(date(2026, 5, 2))


def test_monthly_report_notification_uses_logical_month_deduplication() -> None:
    provider = CompleteMonthlyProvider()
    store = SaveSpyStore()
    notifier = Recorder()
    service = MonthlyReportService(
        provider,
        provider,
        store,
        source=SourceMetadata("monthly-run", "Monthly Report"),
        plant_id="plant",
        timezone=ZONE,
        csv_path=None,
        notifier=notifier,
        clock=lambda: datetime(2026, 7, 1, 8, tzinfo=ZONE),
    )

    first = service.run(date(2026, 6, 1))
    duplicate = service.run(date(2026, 6, 1))

    assert first == duplicate
    assert len(notifier.events) == 1
    assert notifier.events[0].kind.value == "monthly_report"
    assert len(store.saved_payloads) == 2
    assert store.payload == store.saved_payloads[-1]


def test_monthly_availability_merges_interruptions_and_counts_only_intersecting_gaps() -> None:
    provider = CompleteMonthlyProvider()
    month = date(2026, 6, 1)
    month_start = datetime(2026, 6, 1, tzinfo=ZONE)
    interruption_start = month_start + timedelta(days=9, hours=10)
    state = MonitorState(
        generated_at=datetime(2026, 7, 1, 8, tzinfo=ZONE),
        source=SourceMetadata("prior", "Solar Monitor"),
        interruption_events=(
            InterruptionEvent(
                "telemetry-event",
                InterruptionKind.TELEMETRY,
                interruption_start,
                interruption_start + timedelta(hours=3),
            ),
            InterruptionEvent(
                "production-event",
                InterruptionKind.PRODUCTION,
                interruption_start + timedelta(hours=2),
                interruption_start + timedelta(hours=5),
            ),
        ),
        coverage_gaps=(
            CoverageGap(month_start - timedelta(days=2), month_start - timedelta(days=1), "old"),
            CoverageGap(month_start - timedelta(hours=1), month_start + timedelta(hours=1), "edge"),
            CoverageGap(month_start + timedelta(days=20), None, "open"),
            CoverageGap(
                datetime(2026, 7, 2, tzinfo=ZONE),
                datetime(2026, 7, 3, tzinfo=ZONE),
                "future",
            ),
        ),
    )
    service = MonthlyReportService(
        provider,
        provider,
        MemoryStateStore(encode_state(state)),
        source=SourceMetadata("monthly-run", "Monthly Report"),
        plant_id="plant",
        timezone=ZONE,
        csv_path=None,
        clock=lambda: datetime(2026, 7, 1, 8, tzinfo=ZONE),
    )

    report = service.run(month)

    assert report.interruption_hours == pytest.approx(5.0)
    assert report.availability_percent == pytest.approx(100 * (720 - 5) / 720)
    assert report.coverage_gap_count == 2
    assert report.coverage_complete is False

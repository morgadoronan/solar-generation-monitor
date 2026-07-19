"""Application services joining providers, policies, notifications, and state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from math import isfinite
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from solar_monitor.models import (
    DeviceCondition,
    Notification,
    NotificationKind,
)
from solar_monitor.policies import (
    CleaningEvidence,
    CleaningStatus,
    InterruptionEvidence,
    InterruptionStatus,
    LowPerformanceDay,
    SunnyDayStatus,
    evaluate_cleaning_day,
    evaluate_interruption,
    performance_ratio,
    qualify_sunny_day,
)
from solar_monitor.ports import (
    GenerationProvider,
    InvalidCredentialsError,
    NotificationProvider,
    ProviderError,
    SessionExpiredError,
    StateStore,
    WeatherProvider,
)
from solar_monitor.reports import DayReport, MonthlyReport, previous_month, write_csv_atomic
from solar_monitor.solar import solar_elevation
from solar_monitor.state import (
    EventKind,
    EventRecord,
    FailureObservation,
    InterruptionEvent,
    InterruptionKind,
    MonitorState,
    PerformanceEvidence,
    ProcessedSample,
    SourceMetadata,
    decode_or_bootstrap,
    deterministic_event_id,
    encode_state,
    prune_state,
)

DEFAULT_BASELINE_DAY = date(2026, 6, 7)
FAILURE_SEPARATION = timedelta(minutes=20)
REMINDER_DAYLIGHT_HOURS = 6.0
CLEANING_REMINDER = timedelta(days=7)


class Clock(Protocol):
    def __call__(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class CalibrationResult:
    day: date
    sample_count: int
    peak_kw: float | None
    peak_at: datetime | None
    energy_kwh: float | None
    radiation_mj_m2: float | None
    sunshine_hours: float
    generation_complete: bool
    weather_complete: bool


@dataclass(frozen=True, slots=True)
class ServiceResult:
    status: str
    notifications: tuple[str, ...] = ()
    detail: str | None = None


class CalibrationService:
    def __init__(
        self,
        generation: GenerationProvider,
        weather: WeatherProvider,
        *,
        baseline_day: date = DEFAULT_BASELINE_DAY,
    ) -> None:
        self._generation = generation
        self._weather = weather
        self._baseline_day = baseline_day

    def run(self, day: date | None = None) -> CalibrationResult:
        selected = day or self._baseline_day
        generation = self._generation.get_generation_day(selected)
        weather = self._weather.get_weather_day(selected)
        peak = max(generation.samples, key=lambda sample: sample.power_kw, default=None)
        return CalibrationResult(
            day=selected,
            sample_count=len(generation.samples),
            peak_kw=None if peak is None else peak.power_kw,
            peak_at=None if peak is None else peak.observed_at,
            energy_kwh=generation.energy_kwh,
            radiation_mj_m2=weather.shortwave_radiation_mj_m2,
            sunshine_hours=weather.sunshine_seconds / 3600,
            generation_complete=generation.complete,
            weather_complete=weather.complete,
        )


class MonitorService:
    def __init__(
        self,
        generation: GenerationProvider,
        weather: WeatherProvider,
        state_store: StateStore,
        *,
        source: SourceMetadata,
        plant_id: str,
        latitude: float,
        longitude: float,
        timezone: ZoneInfo,
        notifier: NotificationProvider | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._generation = generation
        self._weather = weather
        self._store = state_store
        self._source = source
        self._plant_id = plant_id
        self._latitude = latitude
        self._longitude = longitude
        self._timezone = timezone
        self._notifier = notifier
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(self, observed_at: datetime | None = None) -> ServiceResult:
        now = _local(self._clock() if observed_at is None else observed_at, self._timezone)
        state = decode_or_bootstrap(
            self._store.load_latest(), generated_at=now, source=self._source
        )
        state = replace(state, generated_at=now, source=self._source)
        elevation = solar_elevation(now, latitude=self._latitude, longitude=self._longitude)
        if elevation <= 10:
            self._save(state, now)
            return ServiceResult("nighttime")

        try:
            generation = self._generation.get_generation_day(now.date())
            status = self._generation.get_device_status(now)
        except (InvalidCredentialsError, SessionExpiredError):
            raise
        except ProviderError:
            return self._provider_failure(state, now, "generation")
        samples = tuple(sample for sample in generation.samples if sample.observed_at <= now)
        latest = samples[-1] if samples else None
        telemetry_bad = (
            status.condition is DeviceCondition.OFFLINE
            or latest is None
            or not timedelta(0) <= now - latest.observed_at <= timedelta(minutes=20)
        )
        notifications: list[str] = []
        if telemetry_bad:
            state, sent = self._failure_transition(
                state,
                kind=InterruptionKind.TELEMETRY,
                observed_at=status.observed_at,
                now=now,
            )
            notifications.extend(sent)
            # Production is unknown when telemetry itself is unhealthy.
            state = replace(state, production_failure=None)
        else:
            state, sent = self._recover(state, InterruptionKind.TELEMETRY, now)
            notifications.extend(sent)
            assert latest is not None
            try:
                weather = self._weather.get_snapshot(now)
            except ProviderError:
                return self._provider_failure(state, now, "weather")
            existing = state.production_failure
            decision = evaluate_interruption(
                evaluated_at=now,
                solar_elevation_degrees=elevation,
                power_sample=latest,
                weather=weather,
                device_condition=status.condition,
                evidence=None
                if existing is None
                else InterruptionEvidence(existing.first_observed_at, existing.last_observed_at),
            )
            if decision.status in (InterruptionStatus.PENDING, InterruptionStatus.INTERRUPTED):
                assert decision.evidence is not None
                state = replace(
                    state,
                    production_failure=FailureObservation(
                        decision.evidence.first_observed_at, decision.evidence.last_observed_at
                    ),
                )
                if decision.should_alert:
                    state, sent = self._activate_or_remind(
                        state, InterruptionKind.PRODUCTION, decision.evidence.first_observed_at, now
                    )
                    notifications.extend(sent)
            elif decision.status is InterruptionStatus.NORMAL:
                state = replace(state, production_failure=None)
                state, sent = self._recover(state, InterruptionKind.PRODUCTION, now)
                notifications.extend(sent)

        if latest is not None:
            state = replace(
                state,
                last_processed_sample=ProcessedSample(latest.observed_at),
            )
        state = _close_open_coverage(state, now)
        self._save(state, now)
        return ServiceResult("ok", tuple(notifications))

    def _failure_transition(
        self,
        state: MonitorState,
        *,
        kind: InterruptionKind,
        observed_at: datetime,
        now: datetime,
    ) -> tuple[MonitorState, list[str]]:
        current = state.telemetry_failure
        if current is None or observed_at < current.first_observed_at:
            failure = FailureObservation(observed_at, observed_at)
        elif observed_at > current.last_observed_at:
            failure = FailureObservation(current.first_observed_at, observed_at)
        else:
            failure = current
        state = replace(state, telemetry_failure=failure)
        if failure.last_observed_at - failure.first_observed_at < FAILURE_SEPARATION:
            return state, []
        return self._activate_or_remind(state, kind, failure.first_observed_at, now)

    def _activate_or_remind(
        self,
        state: MonitorState,
        kind: InterruptionKind,
        started_at: datetime,
        now: datetime,
    ) -> tuple[MonitorState, list[str]]:
        active = (
            state.active_telemetry_event_id
            if kind is InterruptionKind.TELEMETRY
            else state.active_production_event_id
        )
        stamps = (
            state.telemetry_timestamps
            if kind is InterruptionKind.TELEMETRY
            else state.production_timestamps
        )
        if active is None:
            event_id = deterministic_event_id(kind, started_at, self._plant_id)
            notification = Notification(
                event_id,
                NotificationKind.INTERRUPTION,
                now,
                f"Solar {kind.value} interruption",
                f"A daylight {kind.value} interruption was confirmed at {started_at.isoformat()}.",
            )
            self._deliver(notification)
            stamps = replace(stamps, last_alert_at=now)
            event = InterruptionEvent(event_id, kind, started_at)
            state = replace(
                state,
                active_telemetry_event_id=event_id
                if kind is InterruptionKind.TELEMETRY
                else state.active_telemetry_event_id,
                active_production_event_id=event_id
                if kind is InterruptionKind.PRODUCTION
                else state.active_production_event_id,
                telemetry_timestamps=stamps
                if kind is InterruptionKind.TELEMETRY
                else state.telemetry_timestamps,
                production_timestamps=stamps
                if kind is InterruptionKind.PRODUCTION
                else state.production_timestamps,
                interruption_events=(*state.interruption_events, event),
                event_history=(
                    *state.event_history,
                    EventRecord(event_id, EventKind.INTERRUPTION_ALERT, now),
                ),
            )
            return state, [event_id]
        since = stamps.last_reminder_at or stamps.last_alert_at or started_at
        if (
            _daylight_hours(
                since,
                now,
                latitude=self._latitude,
                longitude=self._longitude,
                timezone=self._timezone,
            )
            < REMINDER_DAYLIGHT_HOURS
        ):
            return state, []
        reminder_id = deterministic_event_id(f"{kind.value}_reminder", now, active)
        self._deliver(
            Notification(
                reminder_id,
                NotificationKind.INTERRUPTION,
                now,
                f"Solar {kind.value} interruption continues",
                "The confirmed daylight interruption is still active.",
            )
        )
        stamps = replace(stamps, last_reminder_at=now)
        state = replace(
            state,
            telemetry_timestamps=stamps
            if kind is InterruptionKind.TELEMETRY
            else state.telemetry_timestamps,
            production_timestamps=stamps
            if kind is InterruptionKind.PRODUCTION
            else state.production_timestamps,
            event_history=(
                *state.event_history,
                EventRecord(reminder_id, EventKind.INTERRUPTION_REMINDER, now),
            ),
        )
        return state, [reminder_id]

    def _recover(
        self, state: MonitorState, kind: InterruptionKind, now: datetime
    ) -> tuple[MonitorState, list[str]]:
        active = (
            state.active_telemetry_event_id
            if kind is InterruptionKind.TELEMETRY
            else state.active_production_event_id
        )
        if active is None:
            return state, []
        active_event = next(
            (item for item in state.interruption_events if item.event_id == active), None
        )
        # One active interruption has exactly one logical recovery. Anchor the ID to
        # persisted transition data so a retry after delivery succeeded but state
        # persistence failed reuses the same Telegram-visible identifier.
        recovery_anchor = (
            active_event.started_at if active_event is not None else state.generated_at
        )
        recovery_id = deterministic_event_id(
            f"{kind.value}_recovery", recovery_anchor, active
        )
        self._deliver(
            Notification(
                recovery_id,
                NotificationKind.RECOVERY,
                now,
                f"Solar {kind.value} recovered",
                "A healthy daylight observation closed the interruption.",
            )
        )
        events = tuple(
            replace(item, ended_at=now) if item.event_id == active else item
            for item in state.interruption_events
        )
        stamps = (
            state.telemetry_timestamps
            if kind is InterruptionKind.TELEMETRY
            else state.production_timestamps
        )
        stamps = replace(stamps, last_recovery_at=now)
        state = replace(
            state,
            active_telemetry_event_id=None
            if kind is InterruptionKind.TELEMETRY
            else state.active_telemetry_event_id,
            active_production_event_id=None
            if kind is InterruptionKind.PRODUCTION
            else state.active_production_event_id,
            telemetry_failure=None
            if kind is InterruptionKind.TELEMETRY
            else state.telemetry_failure,
            production_failure=None
            if kind is InterruptionKind.PRODUCTION
            else state.production_failure,
            telemetry_timestamps=stamps
            if kind is InterruptionKind.TELEMETRY
            else state.telemetry_timestamps,
            production_timestamps=stamps
            if kind is InterruptionKind.PRODUCTION
            else state.production_timestamps,
            interruption_events=events,
            event_history=(*state.event_history, EventRecord(recovery_id, EventKind.RECOVERY, now)),
        )
        return state, [recovery_id]

    def _provider_failure(self, state: MonitorState, now: datetime, provider: str) -> ServiceResult:
        bucket = now.replace(minute=0, second=0, microsecond=0)
        event_id = deterministic_event_id("provider_failure", bucket, provider)
        sent: tuple[str, ...] = ()
        if all(record.event_id != event_id for record in state.event_history):
            self._deliver(
                Notification(
                    event_id,
                    NotificationKind.PROVIDER_FAILURE,
                    now,
                    f"{provider.title()} provider unavailable",
                    "Monitoring could not obtain provider data; plant state is unknown.",
                )
            )
            state = replace(
                state,
                event_history=(
                    *state.event_history,
                    EventRecord(event_id, EventKind.PROVIDER_FAILURE, now),
                ),
            )
            sent = (event_id,)
        self._save(state, now)
        return ServiceResult("provider_failure", sent, provider)

    def _deliver(self, notification: Notification) -> None:
        if self._notifier is not None:
            self._notifier.send(notification)

    def _save(self, state: MonitorState, now: datetime) -> None:
        self._store.save(encode_state(prune_state(replace(state, generated_at=now), as_of=now)))


class DailyEvaluationService:
    def __init__(
        self,
        generation: GenerationProvider,
        weather: WeatherProvider,
        state_store: StateStore,
        *,
        source: SourceMetadata,
        plant_id: str,
        timezone: ZoneInfo,
        notifier: NotificationProvider | None = None,
        baseline_day: date = DEFAULT_BASELINE_DAY,
        clock: Clock | None = None,
    ) -> None:
        self._generation = generation
        self._weather = weather
        self._store = state_store
        self._source = source
        self._plant_id = plant_id
        self._timezone = timezone
        self._notifier = notifier
        self._baseline_day = baseline_day
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(self, day: date | None = None) -> ServiceResult:
        now = _local(self._clock(), self._timezone)
        selected = day or now.date() - timedelta(days=1)
        if selected >= now.date():
            raise ValueError("evaluate-day requires a completed local calendar day")
        state = decode_or_bootstrap(
            self._store.load_latest(), generated_at=now, source=self._source
        )
        try:
            generation = self._generation.get_generation_day(selected)
            weather = self._weather.get_weather_day(selected)
            baseline_generation = self._generation.get_generation_day(self._baseline_day)
            baseline_weather = self._weather.get_weather_day(self._baseline_day)
        except (InvalidCredentialsError, SessionExpiredError):
            raise
        except ProviderError:
            return ServiceResult("provider_failure", detail="daily evaluation provider")
        evidence = CleaningEvidence(
            tuple(
                LowPerformanceDay(item.day, item.performance_ratio)
                for item in state.qualifying_performance
            )
        )
        decision = evaluate_cleaning_day(
            generation=generation,
            weather=weather,
            baseline_kwh=baseline_generation.energy_kwh,
            baseline_radiation_mj_m2=baseline_weather.shortwave_radiation_mj_m2,
            evidence=evidence,
        )
        state = replace(
            state,
            generated_at=now,
            source=self._source,
            qualifying_performance=tuple(
                PerformanceEvidence(item.day, item.ratio) for item in decision.evidence.low_days
            ),
        )
        sent: list[str] = []
        if decision.status is CleaningStatus.NORMAL:
            state = replace(state, active_cleaning_event_id=None)
        elif decision.status is CleaningStatus.ALERT:
            active = state.active_cleaning_event_id
            due = (
                active is None
                or state.cleaning_timestamps.last_reminder_at is None
                or (now - state.cleaning_timestamps.last_reminder_at >= CLEANING_REMINDER)
            )
            if due:
                event_id = deterministic_event_id(
                    "cleaning" if active is None else "cleaning_reminder",
                    datetime.combine(selected, time.min, self._timezone),
                    self._plant_id if active is None else active,
                )
                if self._notifier is not None:
                    self._notifier.send(
                        Notification(
                            event_id,
                            NotificationKind.CLEANING,
                            now,
                            "Solar cleaning evidence",
                            "Three qualifying sunny days were below 80% of the calibrated "
                            "baseline.",
                        )
                    )
                stamps = replace(
                    state.cleaning_timestamps,
                    last_alert_at=now
                    if active is None
                    else state.cleaning_timestamps.last_alert_at,
                    last_reminder_at=now,
                )
                state = replace(
                    state,
                    active_cleaning_event_id=event_id if active is None else active,
                    cleaning_timestamps=stamps,
                    event_history=(
                        *state.event_history,
                        EventRecord(
                            event_id,
                            EventKind.CLEANING_ALERT
                            if active is None
                            else EventKind.CLEANING_REMINDER,
                            now,
                        ),
                    ),
                )
                sent.append(event_id)
        self._store.save(encode_state(prune_state(state, as_of=now)))
        return ServiceResult(decision.status.value, tuple(sent))


class MonthlyReportService:
    def __init__(
        self,
        generation: GenerationProvider,
        weather: WeatherProvider,
        state_store: StateStore,
        *,
        source: SourceMetadata,
        plant_id: str,
        timezone: ZoneInfo,
        csv_path: Path | None,
        notifier: NotificationProvider | None = None,
        baseline_day: date = DEFAULT_BASELINE_DAY,
        energy_rate_brl_per_kwh: float | None = None,
        force_notification: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self._generation = generation
        self._weather = weather
        self._store = state_store
        self._source = source
        self._plant_id = plant_id
        self._timezone = timezone
        self._csv_path = csv_path
        self._notifier = notifier
        self._baseline_day = baseline_day
        if energy_rate_brl_per_kwh is not None and (
            not isfinite(energy_rate_brl_per_kwh) or energy_rate_brl_per_kwh < 0
        ):
            raise ValueError("energy rate must be finite and non-negative")
        self._energy_rate_brl_per_kwh = energy_rate_brl_per_kwh
        self._force_notification = force_notification
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(self, month: date | None = None) -> MonthlyReport:
        now = _local(self._clock(), self._timezone)
        if month is None:
            start_day, end_day = previous_month(now)
        else:
            if month.day != 1:
                raise ValueError("report month must be the first day of a calendar month")
            current_month = now.date().replace(day=1)
            if month >= current_month:
                raise ValueError("report-month requires a completed local calendar month")
            start_day = month
            end_day = (
                date(month.year + 1, 1, 1)
                if month.month == 12
                else date(month.year, month.month + 1, 1)
            )
        state = decode_or_bootstrap(
            self._store.load_latest(), generated_at=now, source=self._source
        )
        try:
            baseline_generation = self._generation.get_generation_day(self._baseline_day)
        except (InvalidCredentialsError, SessionExpiredError):
            raise
        except ProviderError:
            baseline_generation = None
        try:
            baseline_weather = self._weather.get_weather_day(self._baseline_day)
        except ProviderError:
            baseline_weather = None
        days: list[DayReport] = []
        generation_failures = 0
        weather_failures = 0
        provider_failure_limit = 3
        cursor = start_day
        while cursor < end_day:
            generation = None
            if generation_failures < provider_failure_limit:
                try:
                    generation = self._generation.get_generation_day(cursor)
                except (InvalidCredentialsError, SessionExpiredError):
                    raise
                except ProviderError:
                    generation_failures += 1
                else:
                    generation_failures = 0
            weather = None
            if weather_failures < provider_failure_limit:
                try:
                    weather = self._weather.get_weather_day(cursor)
                except ProviderError:
                    weather_failures += 1
                else:
                    weather_failures = 0
            qualifying = (
                generation is not None
                and weather is not None
                and qualify_sunny_day(generation, weather).status
                is SunnyDayStatus.QUALIFYING
            )
            ratio = (
                performance_ratio(
                    daily_kwh=generation.energy_kwh,
                    daily_radiation_mj_m2=weather.shortwave_radiation_mj_m2,
                    baseline_kwh=baseline_generation.energy_kwh,
                    baseline_radiation_mj_m2=baseline_weather.shortwave_radiation_mj_m2,
                )
                if (
                    qualifying
                    and generation is not None
                    and weather is not None
                    and baseline_generation is not None
                    and baseline_weather is not None
                )
                else None
            )
            expected_energy = None
            if (
                weather is not None
                and weather.complete
                and weather.shortwave_radiation_mj_m2 is not None
                and baseline_generation is not None
                and baseline_generation.complete
                and baseline_generation.energy_kwh is not None
                and baseline_weather is not None
                and baseline_weather.complete
                and baseline_weather.shortwave_radiation_mj_m2 is not None
                and baseline_weather.shortwave_radiation_mj_m2 > 0
            ):
                expected_energy = (
                    baseline_generation.energy_kwh
                    * weather.shortwave_radiation_mj_m2
                    / baseline_weather.shortwave_radiation_mj_m2
                )
            generation_variance = (
                100 * (generation.energy_kwh - expected_energy) / expected_energy
                if generation is not None
                and generation.energy_kwh is not None
                and expected_energy is not None
                and expected_energy > 0
                else None
            )
            no_direct_sun_hours = (
                (weather.daylight_seconds - weather.sunshine_seconds) / 3600
                if weather is not None and weather.complete
                else None
            )
            days.append(
                DayReport(
                    cursor,
                    generation.energy_kwh if generation is not None else None,
                    ratio,
                    qualifying,
                    generation is not None
                    and weather is not None
                    and generation.complete
                    and weather.complete,
                    expected_energy,
                    generation_variance,
                    (
                        generation.energy_kwh * self._energy_rate_brl_per_kwh
                        if generation is not None
                        and generation.energy_kwh is not None
                        and self._energy_rate_brl_per_kwh is not None
                        else None
                    ),
                    no_direct_sun_hours,
                )
            )
            cursor += timedelta(days=1)
        qualifying_days = [
            item for item in days if item.qualifying and item.performance_ratio is not None
        ]
        start = datetime.combine(start_day, time.min, self._timezone)
        end = datetime.combine(end_day, time.min, self._timezone)
        interruption_seconds = _covered_seconds(
            ((item.started_at, item.ended_at or now) for item in state.interruption_events),
            start,
            end,
        )
        gaps = [
            item
            for item in state.coverage_gaps
            if item.started_at < end and (item.ended_at is None or item.ended_at > start)
        ]
        month_seconds = (end - start).total_seconds()
        expected_days = [item for item in days if item.expected_energy_kwh is not None]
        comparison_days = [
            item
            for item in expected_days
            if item.complete
            and item.energy_kwh is not None
            and item.expected_energy_kwh is not None
        ]
        comparison_expected_total = sum(
            item.expected_energy_kwh or 0 for item in comparison_days
        )
        comparison_generation_total = sum(
            item.energy_kwh or 0 for item in comparison_days
        )
        no_direct_sun_days = [
            item for item in days if item.no_direct_sun_hours is not None
        ]
        generation_days = [item for item in days if item.energy_kwh is not None]
        report = MonthlyReport(
            month=start_day,
            total_kwh=sum(item.energy_kwh or 0 for item in days),
            best_day=max(
                qualifying_days, key=lambda item: item.performance_ratio or 0, default=None
            ),
            worst_day=min(
                qualifying_days, key=lambda item: item.performance_ratio or 0, default=None
            ),
            interruption_hours=interruption_seconds / 3600,
            availability_percent=max(
                0.0, 100 * (month_seconds - interruption_seconds) / month_seconds
            ),
            coverage_complete=not gaps and all(item.complete for item in days),
            coverage_gap_count=len(gaps) + sum(not item.complete for item in days),
            days=tuple(days),
            expected_energy_kwh=(
                comparison_expected_total if comparison_days else None
            ),
            generation_variance_percent=(
                100
                * (comparison_generation_total - comparison_expected_total)
                / comparison_expected_total
                if comparison_expected_total > 0
                else None
            ),
            estimated_value_brl=(
                sum(item.energy_kwh or 0 for item in generation_days)
                * self._energy_rate_brl_per_kwh
                if generation_days and self._energy_rate_brl_per_kwh is not None
                else None
            ),
            no_direct_sun_hours=(
                sum(item.no_direct_sun_hours or 0 for item in no_direct_sun_days)
                if no_direct_sun_days
                else None
            ),
            climate_data_days=len(no_direct_sun_days),
            performance_data_days=len(comparison_days),
        )
        if self._csv_path is not None:
            write_csv_atomic(self._csv_path, report)
        state = replace(state, generated_at=now, source=self._source)
        event_id = deterministic_event_id("monthly_report", start, self._plant_id)
        already_recorded = any(item.event_id == event_id for item in state.event_history)
        if self._force_notification or not already_recorded:
            if self._notifier is not None:
                self._notifier.send(
                    Notification(
                        event_id,
                        NotificationKind.MONTHLY_REPORT,
                        now,
                        "Monthly solar report",
                        report.telegram_text,
                    )
                )
            if not already_recorded:
                state = replace(
                    state,
                    event_history=(
                        *state.event_history,
                        EventRecord(event_id, EventKind.MONTHLY_REPORT, now),
                    ),
                )
        # Every successful invocation must refresh the upload artifact. Notification
        # deduplication must not suppress state output on a rerun.
        self._store.save(encode_state(prune_state(state, as_of=now)))
        return report


def _local(value: datetime, timezone: ZoneInfo) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone)


def _close_open_coverage(state: MonitorState, now: datetime) -> MonitorState:
    return replace(
        state,
        coverage_gaps=tuple(
            replace(item, ended_at=now) if item.ended_at is None else item
            for item in state.coverage_gaps
        ),
    )


def _daylight_hours(
    start: datetime,
    end: datetime,
    *,
    latitude: float,
    longitude: float,
    timezone: ZoneInfo,
) -> float:
    if end <= start:
        return 0.0
    cursor = start
    seconds = 0.0
    step = timedelta(minutes=10)
    while cursor < end:
        segment_end = min(cursor + step, end)
        midpoint = cursor + (segment_end - cursor) / 2
        if (
            solar_elevation(midpoint.astimezone(timezone), latitude=latitude, longitude=longitude)
            > 10
        ):
            seconds += (segment_end - cursor).total_seconds()
        cursor = segment_end
    return seconds / 3600


def _covered_seconds(
    intervals: Iterable[tuple[datetime, datetime]], start: datetime, end: datetime
) -> float:
    clipped = sorted(
        (max(left, start), min(right, end))
        for left, right in intervals
        if left < end and right > start
    )
    merged: list[tuple[datetime, datetime]] = []
    for left, right in clipped:
        if not merged or left > merged[-1][1]:
            merged.append((left, right))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
    return sum((right - left).total_seconds() for left, right in merged)

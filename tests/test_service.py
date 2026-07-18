from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solar_monitor.github_state import MemoryStateStore
from solar_monitor.models import (
    DeviceCondition,
    DeviceStatus,
    GenerationDay,
    Notification,
    PowerSample,
    WeatherDay,
    WeatherSnapshot,
)
from solar_monitor.ports import ProviderError
from solar_monitor.service import DailyEvaluationService, MonitorService
from solar_monitor.state import SourceMetadata, decode_state

ZONE = ZoneInfo("America/Sao_Paulo")
NOON = datetime(2026, 6, 7, 12, tzinfo=ZONE)


class FakeGeneration:
    def __init__(self, samples: list[PowerSample], conditions: list[DeviceCondition]) -> None:
        self.samples = samples
        self.conditions = deque(conditions)

    def get_generation_day(self, day: date) -> GenerationDay:
        visible = tuple(sample for sample in self.samples if sample.observed_at.date() == day)
        return GenerationDay(day, visible, 1, False)

    def get_device_status(self, observed_at: datetime) -> DeviceStatus:
        return DeviceStatus(observed_at, self.conditions.popleft())


class FakeWeather:
    def get_snapshot(self, observed_at: datetime) -> WeatherSnapshot:
        return WeatherSnapshot(observed_at, 500, 0)

    def get_weather_day(self, day: date) -> WeatherDay:
        sunrise = datetime.combine(day, datetime.min.time(), ZONE).replace(hour=6)
        return WeatherDay(day, sunrise, sunrise.replace(hour=18), 40000, 43200, 0, 20, True)


class Recorder:
    def __init__(self, fail: bool = False) -> None:
        self.events: list[Notification] = []
        self.fail = fail

    def send(self, notification: Notification) -> None:
        self.events.append(notification)
        if self.fail:
            raise RuntimeError("delivery uncertain")


class FailNextSaveStore(MemoryStateStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_next = False

    def save(self, payload: bytes) -> None:
        if self.fail_next:
            self.fail_next = False
            raise OSError("sanitized persistence failure")
        super().save(payload)


class DailyGeneration:
    def __init__(self, energy_by_day: dict[date, float], *, baseline_day: date) -> None:
        self.energy_by_day = energy_by_day
        self.baseline_day = baseline_day

    def get_generation_day(self, day: date) -> GenerationDay:
        energy = 10.0 if day == self.baseline_day else self.energy_by_day[day]
        return GenerationDay(day, (), energy, True)

    def get_device_status(self, observed_at: datetime) -> DeviceStatus:
        return DeviceStatus(observed_at, DeviceCondition.ONLINE)


class FailingGeneration:
    def get_generation_day(self, day: date) -> GenerationDay:
        raise ProviderError("sanitized generation failure")

    def get_device_status(self, observed_at: datetime) -> DeviceStatus:
        raise AssertionError("status must not be requested after generation fails")


def service(
    generation: FakeGeneration, store: MemoryStateStore, notifier: Recorder
) -> MonitorService:
    return MonitorService(
        generation,
        FakeWeather(),
        store,
        source=SourceMetadata("1", "Solar Monitor"),
        plant_id="plant",
        latitude=-16.65,
        longitude=-49.49,
        timezone=ZONE,
        notifier=notifier,
    )


def test_night_suppresses_provider_calls_and_notifications() -> None:
    generation = FakeGeneration([], [])
    notifier = Recorder()

    result = service(generation, MemoryStateStore(), notifier).run(NOON.replace(hour=2))

    assert result.status == "nighttime"
    assert notifier.events == []


def test_production_requires_two_samples_then_emits_one_recovery() -> None:
    generation = FakeGeneration(
        [
            PowerSample(NOON, 0),
            PowerSample(NOON + timedelta(minutes=20), 0),
            PowerSample(NOON + timedelta(minutes=40), 1),
        ],
        [DeviceCondition.ONLINE] * 3,
    )
    store = MemoryStateStore()
    notifier = Recorder()
    monitor = service(generation, store, notifier)

    assert monitor.run(NOON).notifications == ()
    alerted = monitor.run(NOON + timedelta(minutes=20))
    recovered = monitor.run(NOON + timedelta(minutes=40))

    assert len(alerted.notifications) == 1
    assert len(recovered.notifications) == 1
    assert [item.kind.value for item in notifier.events] == ["interruption", "recovery"]
    assert store.payload is not None
    final = decode_state(store.payload)
    assert final.active_production_event_id is None
    assert final.interruption_events[0].ended_at is not None


def test_recovery_retry_after_post_acceptance_save_failure_reuses_event_id() -> None:
    generation = FakeGeneration(
        [
            PowerSample(NOON, 0),
            PowerSample(NOON + timedelta(minutes=20), 0),
            PowerSample(NOON + timedelta(minutes=40), 1),
            PowerSample(NOON + timedelta(hours=1), 1),
        ],
        [DeviceCondition.ONLINE] * 4,
    )
    store = FailNextSaveStore()
    notifier = Recorder()
    monitor = service(generation, store, notifier)

    monitor.run(NOON)
    monitor.run(NOON + timedelta(minutes=20))
    store.fail_next = True

    with pytest.raises(OSError, match="persistence failure"):
        monitor.run(NOON + timedelta(minutes=40))

    retry = monitor.run(NOON + timedelta(hours=1))

    recovery_events = [
        item for item in notifier.events if item.kind.value == "recovery"
    ]
    assert len(recovery_events) == 2
    assert recovery_events[0].event_id == recovery_events[1].event_id
    assert retry.notifications == (recovery_events[0].event_id,)
    final = decode_state(store.payload or b"")
    assert final.active_production_event_id is None
    assert [
        item.event_id for item in final.event_history if item.kind.value == "recovery"
    ] == [recovery_events[0].event_id]


def test_failed_delivery_does_not_persist_active_event_so_retry_is_at_least_once() -> None:
    generation = FakeGeneration(
        [PowerSample(NOON, 0), PowerSample(NOON + timedelta(minutes=20), 0)],
        [DeviceCondition.ONLINE] * 2,
    )
    store = MemoryStateStore()
    monitor = service(generation, store, Recorder(fail=True))
    monitor.run(NOON)
    before = store.payload

    with pytest.raises(RuntimeError, match="uncertain"):
        monitor.run(NOON + timedelta(minutes=20))

    assert store.payload == before


def test_telemetry_alert_recovers_once_after_a_fresh_online_sample() -> None:
    generation = FakeGeneration(
        [PowerSample(NOON + timedelta(minutes=40), 1)],
        [DeviceCondition.OFFLINE, DeviceCondition.OFFLINE, DeviceCondition.ONLINE],
    )
    store = MemoryStateStore()
    notifier = Recorder()
    monitor = service(generation, store, notifier)

    assert monitor.run(NOON).notifications == ()
    alert = monitor.run(NOON + timedelta(minutes=20))
    recovery = monitor.run(NOON + timedelta(minutes=40))

    assert len(alert.notifications) == 1
    assert len(recovery.notifications) == 1
    assert [item.kind.value for item in notifier.events] == ["interruption", "recovery"]
    final = decode_state(store.payload or b"")
    assert final.active_telemetry_event_id is None
    assert final.telemetry_failure is None
    assert final.interruption_events[0].ended_at == NOON + timedelta(minutes=40)


def test_active_interruption_reminds_only_after_six_accumulated_daylight_hours() -> None:
    generation = FakeGeneration(
        [],
        [DeviceCondition.OFFLINE, DeviceCondition.OFFLINE, DeviceCondition.OFFLINE],
    )
    store = MemoryStateStore()
    notifier = Recorder()
    monitor = service(generation, store, notifier)

    monitor.run(NOON.replace(hour=8))
    alert = monitor.run(NOON.replace(hour=8, minute=20))
    too_soon = monitor.run(NOON.replace(hour=12, minute=20))

    assert len(alert.notifications) == 1
    assert too_soon.notifications == ()

    # A second service instance supplies the next queued device observation.
    later_generation = FakeGeneration([], [DeviceCondition.OFFLINE])
    reminder = service(later_generation, store, notifier).run(NOON + timedelta(days=1))

    assert len(reminder.notifications) == 1
    assert [item.title for item in notifier.events] == [
        "Solar telemetry interruption",
        "Solar telemetry interruption continues",
    ]


def test_provider_failure_notification_is_deduplicated_within_the_hour() -> None:
    store = MemoryStateStore()
    notifier = Recorder()
    monitor = MonitorService(
        FailingGeneration(),
        FakeWeather(),
        store,
        source=SourceMetadata("1", "Solar Monitor"),
        plant_id="plant",
        latitude=-16.65,
        longitude=-49.49,
        timezone=ZONE,
        notifier=notifier,
    )

    first = monitor.run(NOON)
    duplicate = monitor.run(NOON + timedelta(minutes=10))
    next_hour = monitor.run(NOON + timedelta(hours=1))

    assert len(first.notifications) == 1
    assert duplicate.notifications == ()
    assert len(next_hour.notifications) == 1
    assert [item.kind.value for item in notifier.events] == [
        "provider_failure",
        "provider_failure",
    ]


def test_daily_evaluation_alerts_after_three_low_qualifying_days() -> None:
    baseline = date(2026, 6, 7)
    low_days = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]
    generation = DailyGeneration({day: 7.0 for day in low_days}, baseline_day=baseline)
    store = MemoryStateStore()
    notifier = Recorder()
    daily = DailyEvaluationService(
        generation,
        FakeWeather(),
        store,
        source=SourceMetadata("daily", "Daily Evaluation"),
        plant_id="plant",
        timezone=ZONE,
        notifier=notifier,
        baseline_day=baseline,
        clock=lambda: datetime(2026, 6, 11, 1, tzinfo=ZONE),
    )

    results = [daily.run(day) for day in low_days]

    assert [result.status for result in results] == ["low", "low", "alert"]
    assert [len(result.notifications) for result in results] == [0, 0, 1]
    state = decode_state(store.payload or b"")
    assert state.active_cleaning_event_id is not None
    assert len(state.qualifying_performance) == 3
    assert [item.kind.value for item in notifier.events] == ["cleaning"]


def test_daily_evaluation_weekly_reminder_and_normal_day_reset() -> None:
    baseline = date(2026, 6, 7)
    low_days = [date(2026, 6, 8), date(2026, 6, 9), date(2026, 6, 10)]
    reminder_day = date(2026, 6, 17)
    normal_day = date(2026, 6, 18)
    generation = DailyGeneration(
        {**{day: 7.0 for day in low_days}, reminder_day: 7.0, normal_day: 9.0},
        baseline_day=baseline,
    )
    store = MemoryStateStore()
    notifier = Recorder()

    initial = DailyEvaluationService(
        generation,
        FakeWeather(),
        store,
        source=SourceMetadata("daily-1", "Daily Evaluation"),
        plant_id="plant",
        timezone=ZONE,
        notifier=notifier,
        baseline_day=baseline,
        clock=lambda: datetime(2026, 6, 11, 1, tzinfo=ZONE),
    )
    for day in low_days:
        initial.run(day)

    reminder_service = DailyEvaluationService(
        generation,
        FakeWeather(),
        store,
        source=SourceMetadata("daily-2", "Daily Evaluation"),
        plant_id="plant",
        timezone=ZONE,
        notifier=notifier,
        baseline_day=baseline,
        clock=lambda: datetime(2026, 6, 19, 1, tzinfo=ZONE),
    )
    reminder = reminder_service.run(reminder_day)
    normal = reminder_service.run(normal_day)

    assert len(reminder.notifications) == 1
    assert normal.status == "normal"
    assert [item.kind.value for item in notifier.events] == ["cleaning", "cleaning"]
    state = decode_state(store.payload or b"")
    assert state.active_cleaning_event_id is None
    assert state.qualifying_performance == ()

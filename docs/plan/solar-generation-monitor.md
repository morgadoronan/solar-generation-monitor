# Renovigi Solar Generation Monitor

## Goal

Build an English-only, open-source-ready Python 3.12 monitor that authenticates with Renovigi/ShineMonitor, correlates plant generation with weather in Trindade, sends exceptional Telegram alerts, and sends one consolidated monthly report.

## Constraints Snapshot

- The repository is new and has no existing implementation conventions.
- The configured account and plant were verified against the Renovigi API without persisting their identifiers.
- The configurable clean-day baseline was verified with a complete set of five-minute samples; derived household telemetry remains outside tracked files.
- Credentials and household-specific values must remain outside tracked files.
- GitHub Actions is the execution environment; scheduled execution has no exact-time SLA.
- Scheduled production jobs remain inert until repository variable `ENABLE_LIVE_MONITORING=true`; manual dry runs never use production state or Telegram secrets.
- Normal nighttime zero generation must never create an alert.
- Runtime state must not be committed to Git history; ShineMonitor remains the generation system of record.

## Context Packet

- Renovigi login uses the public company key exposed by its web client and returns a short-lived secret/token pair.
- ShineMonitor supplies daily power curves, daily energy, and device status; provider failures must remain distinct from plant or telemetry failures.
- Open-Meteo supplies radiation, sunshine, cloud, precipitation, sunrise, and sunset data without an API key for the non-commercial MVP.
- No ADRs, code conventions, CI, tests, or project documentation currently exist.
- No unresolved product decisions remain: the approved policy is 30-minute checks, two consecutive daylight failures, and three sunny days below 80% for cleaning evidence.
- Escalate only if implementation proves the provider contracts, artifact persistence, or GitHub schedule model incompatible with these requirements.

## Lane Recommendation

`full-rigor` because the work introduces credentials, external APIs, persistent operational state, notification side effects, and scheduled production workflows.

## Proposed Design

- Package the application under `src/solar_monitor` with domain models and ports for generation, weather, notification, and state.
- Expose CLI commands `calibrate`, `monitor`, `evaluate-day`, and `report-month`.
- Authenticate once per process, sign requests in exact query order, reauthenticate at most once on explicit session expiry, never retry invalid credentials, and sanitize all URLs/errors.
- Compute the expected production window from local solar position; evaluate production only above 10 degrees solar elevation.
- Classify telemetry interruption, production interruption, underperformance, and provider failure separately. Base interruption evidence on provider sample timestamps across two distinct observations at least 20 minutes apart, not on workflow run counters alone.
- Treat a five-minute generation sample as fresh for 20 minutes. A production interruption requires solar elevation above 10 degrees, fresh weather no older than 90 minutes, hourly shortwave radiation at least 200 W/m2, power at or below 0.05 kW, and two qualifying observations. Missing/stale weather yields `unknown`, never a production alert.
- Qualify a sunny day only with complete generation data, at least 70% sunshine/daylight ratio, and no more than 0.5 mm precipitation.
- Compare daily `shortwave_radiation_sum` in MJ/m2 and daily kWh to the June 7 baseline. Compute `performance_ratio = (daily_kwh / daily_radiation_mj_m2) / (baseline_kwh / baseline_radiation_mj_m2)`. Missing, incomplete, or non-positive radiation/energy produces `unknown`. Alert after three qualifying days below 80%; a normal qualifying day resets evidence.
- Persist compact alert state as immutable, versioned, checksummed GitHub Actions artifacts named `solar-monitor-state-v1`. Discover candidates through paginated REST results, exclude the current/expired/dry-run artifacts, verify each candidate workflow run completed successfully on the default branch, and select the newest qualifying artifact by metadata before validation. If that newest artifact is corrupt or incompatible, bootstrap with a coverage gap; never silently roll back to older state. Serialize production workflows through one concurrency group with `cancel-in-progress: false`; stagger cron minutes and keep job timeouts below the 30-minute interval because GitHub retains only one pending run.
- State schema v1 stores generation time, source run/workflow, last processed provider sample, first/last failure observations, active deterministic event IDs, last alert/reminder/recovery timestamps, qualifying-day ratios, interruption events, and monitoring-coverage gaps. Keep policy evidence for 30 days and event/coverage records for 400 days; every monthly report intersecting a recorded gap is permanently marked incomplete. Missing, expired, corrupt, or incompatible state bootstraps conservatively: no synthetic recovery, two new observations are required for interruption, cleaning evidence restarts, and monthly availability is marked incomplete until coverage is restored.
- Telegram delivery is at-least-once. Deterministic event IDs and persisted timestamps provide best-effort deduplication; failures before and after Telegram acceptance are explicit test cases.
- Send interruption reminders every six daylight hours, cleaning reminders at most weekly, and one deterministic logical recovery event per active transition. Physical Telegram delivery remains best-effort deduplicated and may duplicate after a post-acceptance crash.
- Send only exceptional alerts, recovery notifications, and a prior-calendar-month Telegram summary; store a sanitized CSV artifact. Monthly generation is reconstructed from ShineMonitor, while interruption/availability totals come from state and explicitly report incomplete coverage after state loss.

## Implementation Plan

1. Scaffold Python 3.12 packaging, configuration, domain types, provider protocols, fixtures, linting, typing, tests, CI, and secret scanning.
2. Characterize and implement the Renovigi adapter, including authentication, deterministic signing, power, energy, device status, expiry, malformed responses, and sanitized fixtures. Centralize redaction and prove request URLs/query strings never reach logs.
3. Implement Open-Meteo and local solar-position/daylight logic, followed by pure interruption and degradation policies.
4. Implement Telegram notification, versioned/checksummed JSON artifact state, paginated predecessor discovery, orchestration services, CLI commands, and monthly CSV reporting.
5. Add staggered monitor, daily, monthly, and CI workflows with manual dry-run entry points, shared production concurrency, sub-30-minute timeouts, `actions: read`, least privilege, and the live-enable gate. Dry runs use fixtures or read-only providers, a separate concurrency group/artifact namespace, no Telegram secret, and no production-state upload.
6. Write English setup, security, operation, troubleshooting, architecture, contribution, and license documentation.
7. Run unit/integration tests, static checks, workflow validation, secret scans, live read-only calibration, artifact recovery/concurrency tests, canary-secret tests, and independent review.

## Verification

- `python -m pytest`
- `python -m ruff check .`
- `python -m mypy src`
- `python -m build`
- Workflow YAML parsing and action pinning checks.
- Secret-pattern scan over tracked files.
- Manual live read-only auth/calibration confirms complete samples, peak calculation, authoritative/fallback daily kWh, timezone, and archived radiation without exposing credentials or household telemetry.
- Tests cover delayed schedules, timestamp-based two-check interruption and recovery, telemetry lag, low radiation, nighttime suppression, stale weather, sunny-day qualification, three-day cleaning evidence, provider failures, artifact corruption/retention loss/overlap, and local-month boundaries.
- Canary credentials injected into failures never appear in logs, exceptions, reports, CSV, fixtures, or artifacts.
- Artifact tests cover pagination, branch/run conclusion filtering, dry-run exclusion, checksum/version validation, one-pending-run limitations, bootstrap coverage gaps, and deterministic event IDs.
- Notification tests cover failure before delivery and the unavoidable crash window after Telegram acceptance, proving at-least-once rather than exactly-once semantics.

## Implementation Packet

- Lane: `full-rigor`.
- Components: package foundation/domain; Renovigi provider; weather/policies; notification/state/orchestration; workflows/docs.
- Parallel slices: Renovigi provider and weather/policies may proceed after shared interfaces are fixed.
- Stop conditions: exposed secrets, unexpected external writes/notifications, inability to establish complete June 7 daily kWh/radiation calibration, provider contract contradiction, incompatible artifact behavior, or any material architecture change.
- Non-goals: external heartbeat service, database, web dashboard, panel tilt/azimuth modeling, or publishing a public repository/PR.

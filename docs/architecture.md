# Architecture

## Boundaries

The Python package separates provider adapters from pure monitoring policy and orchestration.
Renovigi/ShineMonitor remains the system of record for generation. Open-Meteo supplies weather
and radiation without an API key. Telegram is a notification side effect, and a checksummed JSON
artifact is compact continuity state rather than a generation database.

```text
GitHub Actions scheduler / manual dispatch
                 |
                 v
       CLI orchestration commands
        /       |       |       \
Renovigi   Open-Meteo  policy  artifact state
 adapter     adapter    engine       |
        \       |       /            |
         exceptional events ----------+
                 |
              Telegram
```

After `uv sync --frozen --no-editable --reinstall-package solar-generation-monitor`, the CLI entry point is
`uv run --frozen --no-sync python -m solar_monitor <command>`, where `<command>` is
`calibrate`, `monitor`, `evaluate-day`, or `report-month`. Configuration is environment-only.
Production commands write candidate state to `SOLAR_STATE_OUTPUT_PATH`; a workflow uploads it as
`solar-monitor-state-v1` only after a successful non-dry execution. Monthly reports reconstruct
generation from ShineMonitor and combine it with interruption/coverage state, then upload a
sanitized CSV.

## Policy model

Interruption evidence uses provider sample timestamps, not workflow run counts. Two qualifying
observations must be at least 20 minutes apart. Evaluation happens only above 10 degrees solar
elevation and requires generation data no older than 20 minutes and weather no older than 90
minutes. Radiation must be at least 200 W/m² and power at most 0.05 kW. Nighttime, weak radiation,
stale data, device offline status, and provider failure are separate outcomes.

Daily performance is normalized by radiation against the configured baseline date. A day
qualifies as sunny only when generation/weather data are complete, sunshine is at least 70% of
daylight, and precipitation is at most 0.5 mm. Three qualifying days below 80% create cleaning
evidence; a normal qualifying day resets it.

## State and delivery semantics

State schema v1 is versioned and checksummed. Production workflows share one concurrency group
with `cancel-in-progress: false`. A predecessor must come from a successful default-branch run;
missing, expired, corrupt, or incompatible state bootstraps conservatively and records a coverage
gap. Workflows request 90-day state retention, subject to GitHub/repository limits. The monitor
does not silently select an older artifact after the newest eligible artifact is invalid.

Telegram delivery is at-least-once. Deterministic event IDs and persisted timestamps reduce
duplicates, but a process crash after Telegram accepts a message and before state upload can
produce a duplicate. Interruption reminders are limited to every six daylight hours and cleaning
reminders to weekly. Recovery is a logical transition, not proof of exactly-once delivery.

## Known limits

- GitHub Actions schedules can be delayed or skipped and have no exact scheduler SLA. The shared
  concurrency group also means GitHub retains at most one pending production run.
- Artifact expiration, deletion, corruption, or failed runs create permanent coverage gaps for
  intersecting monthly availability reports. Artifacts are not an external heartbeat.
- Provider contract changes, rate limits, authentication outages, delayed telemetry, or weather
  outages can prevent evaluation. They must not be reclassified as confirmed plant failures.
- The model does not account for panel tilt/azimuth, shading, hardware-specific derating, or a
  remote database. The project is being prepared for future public release, not operating an SLA.

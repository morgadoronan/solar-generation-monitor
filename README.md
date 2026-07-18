# Solar Generation Monitor

Solar Generation Monitor is a Python 3.12 service that compares generation from a
Renovigi/ShineMonitor plant with local weather, sends exceptional Telegram alerts, and
produces a consolidated monthly report. It is designed to run from GitHub Actions without
committing credentials, household details, or runtime state.

The repository is being prepared for a future public release. It is not a hosted monitoring
service, and its upstream provider interfaces may change without notice. See
[the architecture](docs/architecture.md), [operations guide](docs/operations.md), and
[security policy](SECURITY.md) before enabling live runs.

## What it detects

- A daylight production interruption after two distinct qualifying samples at least 20 minutes
  apart. The policy requires solar elevation above 10 degrees, generation no greater than
  0.05 kW, radiation of at least 200 W/m², fresh generation (20 minutes), and fresh weather
  (90 minutes).
- Possible cleaning need after three complete sunny days below 80% of the
  radiation-normalized baseline. A sunny day requires at least 70% sunshine/daylight and no
  more than 0.5 mm precipitation.
- Telemetry, device, weather, and provider failures as conditions distinct from low generation.
  Normal nighttime zero generation never alerts.

## Local setup

Install [uv](https://docs.astral.sh/uv/). Synthetic dry runs supply safe defaults and do not need
an `.env` file:

```console
uv sync --frozen --no-editable --reinstall-package solar-generation-monitor
uv run --frozen --no-sync python -m solar_monitor calibrate --date 2000-01-01 --dry-run
uv run --frozen --no-sync python -m solar_monitor monitor --dry-run
uv run --frozen --no-sync python -m solar_monitor evaluate-day --date 2000-01-01 --dry-run
uv run --frozen --no-sync python -m solar_monitor report-month --month 2000-01 --dry-run
```

For local live use, copy `.env.example` to an ignored `.env`, replace every placeholder, review
the operations guide, and load it explicitly:

```console
uv run --env-file .env --frozen --no-sync python -m solar_monitor calibrate --date YYYY-MM-DD
uv run --env-file .env --frozen --no-sync python -m solar_monitor monitor
uv run --env-file .env --frozen --no-sync python -m solar_monitor evaluate-day --date YYYY-MM-DD
uv run --env-file .env --frozen --no-sync python -m solar_monitor report-month --month YYYY-MM
```

Secrets must come from the process environment; macOS Keychain is not used by the application or
GitHub Actions.

## GitHub configuration

Create a protected GitHub environment named `production` with these secrets:

| Secret | Purpose |
| --- | --- |
| `RENOVIGI_USERNAME` | Renovigi/ShineMonitor login name |
| `RENOVIGI_PASSWORD` | Renovigi/ShineMonitor password |
| `TELEGRAM_BOT_TOKEN` | Token issued by Telegram BotFather |
| `TELEGRAM_CHAT_ID` | Destination chat or channel identifier |

Set these repository variables (never put household-specific values in tracked files):

| Variable | Example shape / purpose |
| --- | --- |
| `RENOVIGI_PLANT_ID` | Provider plant identifier |
| `SOLAR_LATITUDE` | Decimal latitude |
| `SOLAR_LONGITUDE` | Decimal longitude |
| `SOLAR_TIMEZONE` | IANA name such as `Area/City` |
| `SOLAR_BASELINE_DATE` | Calibrated `YYYY-MM-DD` reference day |
| `ENABLE_LIVE_MONITORING` | Must equal the lowercase string `true` for live jobs |

Workflows derive `DEFAULT_BRANCH` from GitHub's repository default branch; there is no repository
variable to keep synchronized manually. Local live runs set it in `.env`.

Workflow dispatches default to `dry_run: true`. Dry runs use separate concurrency, do not load
the `production` environment, do not send Telegram messages, and do not upload production
state. Scheduled jobs and manual non-dry runs remain inert until `ENABLE_LIVE_MONITORING` is
`true`. Details, including Telegram setup and recovery, are in
[docs/operations.md](docs/operations.md).

## Development

```console
uv run --frozen --no-sync pytest
uv run --frozen --no-sync ruff check .
uv run --frozen --no-sync mypy src
uv build
```

Open-Meteo data is used with attribution to [Open-Meteo](https://open-meteo.com/). The free API
is intended for non-commercial use; review its current licence and pricing before commercial
deployment. This project is licensed under the [MIT License](LICENSE).

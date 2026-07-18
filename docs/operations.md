# Operations Guide

## Initial setup

Configure the exact GitHub secrets and variables listed in the README. Keep the `production`
environment protected. Workflows derive `DEFAULT_BRANCH` from GitHub repository metadata so state
is accepted only from successful runs on the current default branch.

To configure Telegram:

1. Create a dedicated bot with BotFather and store its token as `TELEGRAM_BOT_TOKEN`.
2. Start a private chat with the bot, or add it to a narrowly scoped channel.
3. Obtain the destination ID without pasting the token or full API URL into logs, and store it as
   `TELEGRAM_CHAT_ID`.
4. Leave `ENABLE_LIVE_MONITORING=false`, run each workflow manually with `dry_run=true`, and
   inspect sanitized output.
5. Protect the environment, set the enable variable to the lowercase string `true`, and use one
   controlled manual non-dry run before relying on schedules.

Dry runs do not load the production environment, use production state, upload
`solar-monitor-state-v1`, or send Telegram. Scheduled runs and manual non-dry runs are gated by
`ENABLE_LIVE_MONITORING == 'true'`.

## Schedules and expected behavior

- The monitor workflow is requested at minutes 7 and 37 from 09:00 through 21:59 UTC, a broad
  Trindade daylight window. The policy still computes solar elevation for every run; normal night
  zero production is suppressed.
- The daily workflow is requested at 03:17 UTC, just after local midnight, and evaluates the
  just-completed previous local day for sunny-day/cleaning evidence.
- The monthly workflow is requested at 10:43 UTC on the first day of each month and reports the
  previous local calendar month. It uploads a sanitized CSV as well as sending the summary. If an
  isolated provider day is unavailable after bounded retries, the report preserves the available
  days and explicitly marks coverage incomplete instead of discarding the entire month.

Cron times are UTC requests, not appointments. GitHub may delay or skip scheduled work; there is
no exact scheduler SLA. All production jobs share concurrency with cancellation disabled and
finish within 30 minutes so overlapping schedules do not create a false run-count signal.

## Artifacts and coverage

Successful live runs upload candidate state under the immutable logical name
`solar-monitor-state-v1`. The application validates source run, branch, version, and checksum.
The workflows request 90-day artifact retention, subject to repository and GitHub limits. Expired,
manually deleted, corrupt, or incompatible state restarts interruption/cleaning evidence
conservatively and records a coverage gap; it never synthesizes recovery. Any monthly period
intersecting a known gap remains marked incomplete. The sanitized monthly CSV is for reporting,
not state restoration.

Artifacts can expose household activity patterns even after field sanitization. Restrict Actions
and artifact access, use appropriate repository retention settings, and do not download them to
shared devices.

## Provider failures

Authentication failure, session expiry, rate limiting, malformed responses, missing samples,
stale telemetry, and Open-Meteo outages are provider/telemetry conditions. They are not evidence
of low production. The service reauthenticates only on explicit expiry and does not retry invalid
credentials. Transient transport failures use bounded exponential retries. Missing or stale
weather produces an unknown result rather than a production alert. Monthly reconstruction stops
calling one unavailable provider after three consecutive failed days so a prolonged outage cannot
consume the entire workflow timeout; the skipped days remain visibly incomplete.

Open-Meteo must be attributed. Its free API is intended for non-commercial use; confirm current
licensing, attribution, usage limits, and paid-plan requirements before any commercial deployment.

## Troubleshooting

1. Confirm the workflow was not merely delayed and check whether a production job is already
   running or pending.
2. Confirm the enable variable is exactly `true`, GitHub reports the expected default branch, and
   all six repository variables plus four environment secrets use the exact names documented.
3. Classify the outcome: provider error, stale generation, stale/missing weather, device offline,
   nighttime, pending evidence, or confirmed interruption. Do not bypass freshness/daylight rules.
4. Inspect logs for sanitized error categories only. Never print environment variables, signed
   URLs, raw provider responses, Telegram URLs, or artifact contents.
5. For missing/corrupt state, accept the recorded coverage gap and allow evidence to rebuild; do
   not upload a hand-edited older file.
6. After credential changes, keep live monitoring disabled, rotate affected tokens/passwords,
   update protected secrets, and complete a dry run before re-enabling it.

If alerts are absent at night, that is expected. If alerts duplicate after a crash near Telegram
delivery, that is an acknowledged at-least-once delivery limitation.

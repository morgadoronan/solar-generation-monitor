# Security Policy

## Reporting a vulnerability

Until a private security advisory channel is published with the future public repository,
contact the repository owner privately. Do not open a public issue containing credentials,
provider URLs with embedded authentication, household location, plant identifiers, chat IDs,
or exploit details. Include the affected revision, impact, and a minimal sanitized reproducer.

## Credential handling

Production credentials belong only in the protected GitHub `production` environment. Repository
variables hold non-secret runtime configuration, but plant IDs and coordinates are still private
household metadata and must not be committed. Local values belong in the ignored `.env`; the
application does not integrate with macOS Keychain.

Use a least-privilege Telegram bot dedicated to this monitor. If any Renovigi password, Telegram
token, chat ID, workflow log, artifact, fixture, or URL may have leaked:

1. Disable live monitoring by setting `ENABLE_LIVE_MONITORING` to `false`.
2. Rotate the Renovigi password and revoke/regenerate the Telegram bot token with BotFather.
3. Replace the protected environment secrets and verify the destination chat.
4. Delete exposed workflow logs/artifacts and review repository history; deleting only the latest
   file does not remove earlier Git objects.
5. Run a manual dry run before re-enabling live monitoring.

Runtime state and reports are sanitized but can reveal operational patterns. GitHub artifact
access and retention must be limited to trusted repository collaborators.

## Supported versions

Before the first public release, only the current default branch receives security fixes. No
formal compatibility or response-time commitment is offered yet.

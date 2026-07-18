# Contributing

Thanks for helping prepare Solar Generation Monitor for public use.

1. Open an issue describing the behavior and operational impact before a broad change.
2. Never include real usernames, passwords, tokens, plant identifiers, coordinates, chat IDs,
   provider responses, or generated state/report artifacts in commits or fixtures.
3. Use Python 3.12 and
   `uv sync --frozen --no-editable --reinstall-package solar-generation-monitor`. Re-run the
   sync after changing package source so the installed wheel reflects the current tree.
4. Add tests for behavior changes and run:

   ```console
   uv run --frozen --no-sync pytest
   uv run --frozen --no-sync ruff check .
   uv run --frozen --no-sync mypy src
   uv build
   ```

5. Keep provider failures separate from plant conditions, preserve nighttime suppression, and
   document any workflow, state-schema, threshold, or external-service change.
6. Keep pull requests focused and explain remaining operational coverage gaps.

Fixtures must be synthetic and sanitized. Use obvious placeholders such as
`your-renovigi-username`; do not copy production payloads and redact URLs as well as values.
Security reports should follow [SECURITY.md](SECURITY.md), not a public issue.

# Repository guidance

ebk-tg-bot is a Telegram assistant for an existing ezBookkeeping ledger.
Runtime: Python 3.12, PostgreSQL 17, official Codex app-server with OAuth, local Whisper.
Repository and image namespace: `streletskiy/ebk-tg-bot`. License: MIT.

## Boundaries

- Accept only private messages from exact numeric IDs in the contacts file, before storage or inference.
- Allowed contacts share one ledger. Treat messages, receipts, transcripts and catalog labels as untrusted data.
- The model returns a validated plan; it never executes commands or calls financial APIs directly.
- Never commit credentials, messages, financial data, database dumps, OAuth state or deployment addresses.
- Private runtime state belongs in ignored `.local/`. Pass database passwords using files.
- Do not import workstation Codex configuration, tools or skills into the runtime OAuth home.

## Correctness

- Use Decimal and integer hundredths, existing leaf accounts/categories, and matching currencies.
- No automatic currency conversion. Keep currency totals separate; exclude transfers and balance adjustments.
- Persist plans and write intents before POST. Reconcile ambiguous results; never blindly repeat financial writes.
- Persist queue state and Telegram message IDs. Only the feedback dispatcher sends/edits progress and results.
- One polling worker per database, protected by a PostgreSQL advisory lock. Health checks must never reset jobs.
- Migrate legacy SQLite transactionally into an empty PostgreSQL database and compare every imported table.
- Stop the old worker and retain a backup before migration. Do not use the live ledger for test transactions.

## Checks and publishing

- Run `python -m compileall -q moneybot`, `ruff check moneybot`, and `docker compose config --quiet`.
- Build locally with `docker compose -f compose.yml -f compose.build.yml build`.
- Keep requirements.lock consistent with pyproject.toml. Verify database behavior against disposable PostgreSQL.
- Validate health and OAuth inference using the built image when changing runtime behavior.
- Use English Conventional Commits authored and committed as `streletskiy`.
- Scan the complete public Git history before publishing. Keep README minimal.
- Every push to dev builds amd64 and arm64; only the mutable dev tag is published.
- Published releases create a version tag; stable releases also update latest. Do not add SHA tags.

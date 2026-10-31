# Runbooks

## Key rotation (TOKEN_ENCRYPTION_KEY)
Not automated yet (envelope encryption is a TODO). Procedure: put new Fernet key in env as TOKEN_ENCRYPTION_KEY_NEW, run a one-off
script that decrypts each `integration_credentials.encrypted_refresh_token`, `notification_channels.config_encrypted`,
`users.totp_secret_encrypted`, `oauth_states.code_verifier_encrypted` with the old key and re-encrypts with the new one, then swap env.

## Backup / restore
Postgres: `pg_dump -Fc -h 127.0.0.1 -U seo seo_intelligence > seo.dump`; restore `pg_restore -c -d seo_intelligence seo.dump`, then
`uv run alembic upgrade head`. ClickHouse: `BACKUP DATABASE seo TO File('...')` / `RESTORE`; global datasets can also be re-ingested
from MinIO raw provider responses. Restore test: not yet automated.

## Stuck job
`uv run seo jobs --status running` → expired leases are reclaimed automatically (120 s). Force: `seo job-cancel <id>` then
`seo job-retry <id>`. Dead-lettered: `seo jobs --status dead` → inspect `error`, fix, `seo job-retry`.

## Provider outage
Circuit breaker opens after 5 consecutive failures for 60 s; calls return `provider_unavailable`. Check `seo providers`; no fallback
blending — switch priority only by configuring another provider for the capability.

## Budget overrun
`PROVIDER_DAILY_BUDGET_USD` / `PROVIDER_MONTHLY_BUDGET_USD` stop calls before exceeding caps (log line `ALERT provider budget cap
reached`). Raise caps deliberately; review `provider_usage` by org for abuse.

## Common Crawl import
`POST /api/v1/backlinks/common-crawl/import {path: "crawl-data/CC-MAIN-…/wat/…warc.wat.gz", targets: [".tr", "example.com"]}`.
Resumable via job checkpoint (byte offset at gzip member boundary). Respect CC terms of use.

## Reprocessing
Audit: `seo crawl-reanalyze <id>` (idempotent). Opportunities: `POST /api/v1/sites/{id}/opportunities/regenerate`.
Domain strength: `POST /api/v1/backlinks/domain-strength/recompute`. Raw provider responses are in MinIO (`raw-provider/…`).

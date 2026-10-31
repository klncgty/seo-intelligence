# ADR 0004 — Postgres job system (no Temporal)
Status: accepted. `app/jobs.py`: jobs / job_dependencies / schedules tables. Claim = FOR UPDATE SKIP LOCKED ordered by
per-tenant running count (fairness) → priority → age; 120s lease extended by heartbeat; expired lease = crashed worker →
retry. Exponential backoff with jitter, max_attempts → `dead` (dead-letter, manual `seo job-retry`). Idempotency key unique.
DAG: `waiting` until all deps succeed; cancelled if a dep is dead/cancelled (fan-out/fan-in). Checkpoint JSON for resume.
Scheduler leader = pg_try_advisory_xact_lock; missed intervals collapse. Progress via pg_notify('job_events') → SSE
`/api/v1/jobs/{id}/events`. Legacy crawl_runs / sync_jobs keep their own claim logic (already tested); new modules use jobs.
Temporal would add an extra cluster with little benefit at current scale.

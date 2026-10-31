# ADR 0001 — ClickHouse for analytical / global datasets
Status: accepted (2026-10-29)
Context: keyword DB (50M+), SERP snapshots, rank tracking, backlinks (1B design) exceed comfortable Postgres OLTP use.
Decision: ClickHouse (HTTP interface, `app/infra/clickhouse.py`, SQL migrations in `backend/clickhouse/migrations`, `seo ch-migrate`).
ReplacingMergeTree(version) for idempotent re-ingest; monthly partitions; TTL on raw SERP; ngram/token bloom indexes on keyword_key.
Reads: `FINAL` for small/filtered reads, `argMax(col, version)` GROUP BY for large scans. Parameterized queries only.
Existing Postgres GSC/GA4/crawl tables stay the source of truth (no breaking change). For large tenants a later dual-write
(PG → CH facts) + `seo backfill-clickhouse` command will be added; until then CH holds only global/provider/rank data.
Optional: when CLICKHOUSE_URL is unset, dependent modules report "not available" (core profile still runs).

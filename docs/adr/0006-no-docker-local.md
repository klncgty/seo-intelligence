# ADR 0006 — Local services via brew (no Docker on dev machine)
Dev Mac has no Docker. `scripts/dev-services.sh start` runs Postgres 17, Redis, ClickHouse (HTTP 8123, TCP 9100) and MinIO
(9000/9001) bound to 127.0.0.1. Integration tests hit these real services and skip if unreachable. docker-compose.yml is
kept for server deployment (VM; not Vercel — long-running workers/crawlers) and must be validated in Phase 12.

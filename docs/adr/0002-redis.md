# ADR 0002 — Redis for shared rate limits / politeness / cache
Status: accepted. Needed because per-host politeness must be global across tenants, jobs and worker processes
(spec §9), which an in-process limiter cannot guarantee. Atomic Lua token bucket (`app/infra/ratelimit.py`), used by
the crawler HostRateLimiter and (Phase 3) provider/tenant rate limits and response cache. Postgres remains the job
queue (no Redis queue). Without REDIS_URL an in-process fallback is used (documented: not shared across processes).

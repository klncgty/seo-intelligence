# Threat model (STRIDE) — summary

| Surface | Threats | Mitigations (implemented) | Gaps |
|---|---|---|---|
| Crawlers / verifier / content fetch | SSRF to internal services & metadata, DNS rebinding, redirect tricks, alt IP encodings, resource exhaustion, abuse as DDoS | connection-time IP validation (SafeNetworkBackend), every redirect re-checked, private/loopback/link-local/reserved/multicast/NAT64/6to4/v4-mapped blocked, ports 80/443, size/time/redirect caps, gzip/xml bombs, robots.txt, global per-host Redis token bucket, competitor private-path skip, no cookies, unverified-domain limits | no egress proxy (Smokescreen) yet; no JS renderer |
| Webhooks / callbacks / NAP / migration URLs | SSRF via user URLs | same policy (`safe_post`, `_validate_base_url`), no redirect following | — |
| Provider keys / OAuth / tokens | theft, leakage in logs, cross-tenant use | Fernet encryption at rest, per-org credentials + RLS, PKCE + single-use state, log redaction filter, keys never returned | envelope encryption/key rotation command not implemented |
| Multi-tenancy | cross-tenant reads/writes | Postgres RLS (FORCE) chained via sites, ClickHouse `tenant_query` guard, pool checkin resets tenant setting, cross-tenant fuzz test | ClickHouse row policies not configured (app-level guard only) |
| Auth / API | brute force, CSRF, session theft, key abuse | Argon2id, login rate limit, TOTP, httpOnly SameSite=strict secure cookies (SERVER), CSRF header, hashed API keys w/ IP allowlist + plan rate limits, LOCAL mode loopback-only, `seo serve` refuses public bind without auth, security headers | OIDC/SAML not implemented |
| Exports / reports / share links | formula injection, XSS, link leakage | CSV/XLSX formula neutralization, HTML escaping + CSP, hashed expiring revocable share tokens, noindex | — |
| LLM / AI answers | prompt injection from third-party content | answers are string-analyzed only, never executed; no in-app LLM agent (Claude Code is the operator) | — |
| Log ingestion | PII retention, parser abuse | non-bot IPs discarded, size cap, regex line parser | — |
| Billing / credits | overspend, bypass | preflight + explicit confirmation, idempotent ledger, global provider budget caps | Stripe not implemented |

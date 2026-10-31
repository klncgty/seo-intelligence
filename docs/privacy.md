# Privacy / KVKK-GDPR notes

Data inventory
- Control plane (Postgres): organizations, users (email, Argon2id password hash, encrypted TOTP secret), memberships,
  sessions (token hashes only), API keys (SHA-256 hashes only), invites (token hashes), audit log (actor, action; IP only
  for authentication events), sites, competitors, opportunities, credit ledger.
- Tenant first-party data: GSC/GA4 aggregates per site (no personal data — aggregated by page/query/date), encrypted
  Google refresh tokens per organization.
- Global datasets (ClickHouse): keyword metrics, SERP snapshots, backlinks — contain no tenant data.
- Server logs: tokens, OAuth codes/state, API keys and query-string secrets are redacted (`app/core/logging.py`).

Isolation: Postgres RLS on every tenant table (ADR 0005); cross-tenant use only with explicit opt-in
(`organizations.cross_tenant_opt_in`) and aggregate/k-anonymous (not yet used by any model).

Rights: export `GET /api/v1/org/export` (owner); deletion `POST /api/v1/org/delete` cascades all tenant rows and leaves
global datasets untouched. Retention: raw SERP snapshots 25 months (ClickHouse TTL); job/audit retention policy TBD (Phase 12).

Subprocessors (only when configured by the operator): Google (Search Console, Analytics), external SEO data provider
(e.g. DataForSEO). No LLM API receives tenant data (Claude Code operates locally).

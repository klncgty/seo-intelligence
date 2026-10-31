# ADR 0005 — Tenant isolation (planned, Phase 2)
Postgres: tenant_id on tenant tables + Row Level Security policies using `current_setting('app.tenant_id')` set per
request/job transaction; app-layer scoping as second layer. ClickHouse: tenant tables carry tenant_id; all access through
`ClickHouse.tenant_query`, which refuses SQL without the `{tenant_id:UInt64}` filter (implemented), plus CH row policies
for the app user. Global datasets (keyword_metrics, serp_results, backlinks) have no tenant_id and never store tenant data.

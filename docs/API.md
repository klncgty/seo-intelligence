# API contract (v1)

Base URL: `http://127.0.0.1:8000/api/v1`. Consumers: the `seo` CLI and Claude Code (operator). No frontend.
All timestamps ISO-8601 UTC strings. Dates `YYYY-MM-DD`. IDs are integers.

Errors: `{"error": {"code": "not_found" | "validation_error" | "conflict" | "integration_not_configured" | "bad_request", "message": str, "details": any}}` with matching HTTP status.

Paginated lists: `{"items": T[], "total": int, "page": int, "page_size": int}`; query `page` (1-based, default 1), `page_size` (default 50, max 200).

## Health
`GET /health` → `{"status":"ok","database":"ok"|"error","version":str}`

## Sites
Site:
```
{ id, name, base_url, allowed_hosts: string[], gsc_property: string|null, ga4_property_id: string|null,
  conversion_events: { signup: string[], purchase: string[], key_events: string[] },
  crawl_settings: { max_pages:int, max_depth:int, concurrency:int, request_delay_ms:int, timeout_s:number },
  is_active: bool, created_at, updated_at,
  last_crawl: CrawlRun|null }   // last_crawl only in GET list/detail
```
- `POST /sites` body: name, base_url, allowed_hosts?(defaults to [host of base_url]), gsc_property?, ga4_property_id?, conversion_events?, crawl_settings?, is_active? → 201 Site
- `GET /sites?page&page_size&active` → Paginated<Site>
- `GET /sites/{id}` → Site ; `PATCH /sites/{id}` partial body → Site ; `DELETE /sites/{id}` → 204

## Crawls
CrawlRun:
```
{ id, site_id, status: "queued"|"running"|"completed"|"failed"|"cancelled",
  created_at, started_at|null, finished_at|null, pages_discovered, pages_crawled, pages_failed,
  config: {...crawl_settings}, error_summary: string|null, cancel_requested: bool }
```
- `POST /sites/{id}/crawls` body optional crawl_settings overrides → 202 CrawlRun (queued; 409 if a crawl is already queued/running for site)
- `GET /sites/{id}/crawls?page&page_size` → Paginated<CrawlRun>
- `GET /crawls/{id}` → CrawlRun ; `POST /crawls/{id}/cancel` → CrawlRun
- `GET /crawls/{id}/pages?page&page_size&q&status_code&has_error` → Paginated<CrawlPageSummary>
  CrawlPageSummary: `{ id, url, normalized_url, status_code|null, content_type|null, title|null, word_count|null, response_time_ms|null, depth, fetch_error|null, findings_count }`
- `GET /pages/{id}` → CrawlPage: summary fields + `{ crawl_run_id, meta_description, canonical_urls: string[], meta_robots, x_robots_tag, h1: string[], titles: string[], hreflang: [{lang, href}], content_hash, redirect_chain: [{url, status_code}], fetched_at, outlinks: [{target_url, anchor_text, rel, is_internal}], findings: Finding[] }`
- `GET /crawls/{id}/findings?page&page_size&severity&rule_id` → Paginated<Finding>
  Finding: `{ id, crawl_run_id, page_id|null, url|null, rule_id, severity: "critical"|"high"|"medium"|"low"|"info", title, description, evidence: object, remediation }`
- `GET /crawls/{id}/summary` → `{ crawl: CrawlRun, status_distribution: {"200": n, "404": n, "error": n, ...}, findings_by_severity: {critical:n,...}, findings_by_rule: [{rule_id, severity, count}], total_findings }`

## Integrations (Google)
- `GET /integrations/google/status` → `{ oauth_configured: bool, connected: bool, scopes: string[], connected_at|null, setup_instructions: string }`
- `POST /integrations/google/connect` → `{ authorization_url }` (400 `integration_not_configured` if no client id/secret)
- `GET /integrations/google/callback?code&state` → redirect to frontend `/settings?google=connected|error`
- `GET /integrations/google/properties` → `{ search_console: [{site_url, permission_level}], analytics: [{property_id, display_name}] }` (400 if not connected)
- `GET /sites/{id}/integrations/search-console` and `GET /sites/{id}/integrations/analytics` →
  `{ configured: bool, property: string|null, connected: bool, last_sync_at|null, last_success_at|null, last_status: "never"|"running"|"success"|"error", last_error|null, rows_synced: int, data_start|null, data_end|null }`
- `POST /sites/{id}/sync/search-console` and `POST /sites/{id}/sync/analytics` → 202 `{ job_id, status: "queued" }` (400 integration_not_configured when no credentials/property)

## Performance data
- `GET /sites/{id}/search-performance?start&end&dimension=date|page|query|device|country&page&page_size` →
  `{ has_data: bool, start, end, totals: {clicks, impressions, ctr, position}|null, rows: [{key, clicks, impressions, ctr, position}], total_rows }`
  (dimension=date rows ordered by date asc; position is impression-weighted average)
- `GET /sites/{id}/landing-pages?start&end&page&page_size&sort=sessions|conversions|revenue` →
  `{ has_data, start, end, items: [{landing_page, sessions, total_users, active_users|null, key_events|null, signups|null, purchases|null, revenue|null, conversion_rate|null}], total }`
- `GET /sites/{id}/conversions?start&end` →
  `{ has_data, start, end, configured_events: {...}, totals: {sessions, total_users, signups|null, purchases|null, revenue|null, key_events|null}|null, daily: [{date, sessions, signups|null, purchases|null, revenue|null}] }`
- `GET /sites/{id}/growth?start&end&compare_start&compare_end` →
  `{ site_id, current: Metrics, previous: Metrics, change_pct: {clicks|null, impressions|null, sessions|null, signups|null, purchases|null, revenue|null} }`
  Metrics: `{ clicks|null, impressions|null, ctr|null, position|null, sessions|null, users|null, signups|null, purchases|null, revenue|null }` (null = no data)
- `GET /dashboard/overview?start&end` (defaults last 28 days vs previous 28) →
  `{ start, end, compare_start, compare_end, totals: Metrics, previous_totals: Metrics, sites: [{ site: {id,name,base_url}, current: Metrics, previous: Metrics, open_opportunities: int, critical_findings: int|null, last_crawl_at|null, gsc: IntegrationStatus, ga4: IntegrationStatus }], google: {oauth_configured, connected} }`

## Opportunities
Opportunity:
```
{ id, site_id, site_name, url|null, opportunity_type: "low_ctr"|"declining_performance"|"indexing_issue_on_valuable_page"|"low_conversion"|"weak_internal_linking"|"content_intent",
  title, description, evidence: object, data_sources: string[], date_range: {start, end}|null,
  impact_estimate: {metric, value|null, unit, method}|null (null value = unknown), confidence: number(0-1), effort: "low"|"medium"|"high",
  priority_score: number, recommended_action, validation_metric, limitations,
  status: "new"|"planned"|"in_progress"|"implemented"|"dismissed", implemented_at|null, created_at, updated_at }
```
- `GET /sites/{id}/opportunities?status&type&min_confidence&sort=priority|confidence|impact|effort|created&page&page_size` → Paginated<Opportunity>
- `GET /opportunities?site_id&status&type&sort&page&page_size` → Paginated<Opportunity> (all sites, for Action Center)
- `POST /sites/{id}/opportunities/generate` → `{ created, updated, total }`
- `GET /opportunities/{id}` → Opportunity ; `PATCH /opportunities/{id}` body `{status?, notes?}` → Opportunity (implemented_at auto-set when status→implemented)

## Settings
- `GET /settings/crawler` → `{ user_agent, max_pages_limit, max_depth_limit, max_concurrency_limit, min_request_delay_ms, max_response_bytes, max_redirects, defaults: crawl_settings }` (read-only, from env)

## Competitor Intelligence
Competitor: `{ id, site_id, name, domain, base_url, allowed_hosts, notes, crawl_settings, is_active, created_at, updated_at, last_crawl }`
- `POST /sites/{id}/competitors` body `{name, domain (host or URL), notes?, extra_hosts?, crawl_settings?}` → 201 (422 for own domain / non-public targets, 409 duplicate)
- `GET /sites/{id}/competitors` · `GET|PATCH|DELETE /sites/{id}/competitors/{cid}`
- `GET /sites/{id}/competitors/suggestions` → outbound-link domains (suggestions only, never auto-added)
- `POST /sites/{id}/competitors/{cid}/crawls` → 202 CrawlRun (`competitor_id` set; stricter limits) · `GET /sites/{id}/competitors/{cid}/crawls`
- `GET /sites/{id}/competitors-comparison` → `{available, sources:[{label, source, updated_at}], technical, technical_differences, url_structure, page_types, missing_page_types, caveats}`
- `GET /sites/{id}/competitors-content-gaps?limit` → `{available, sources, own_gsc:{source:"gsc:own (first-party)",...}, gaps:[{topic, competitors, own_gsc_evidence|null, evidence_level}], total}`
- `POST /sites/{id}/competitors-opportunities/generate` → `{available, created, updated, total}` (stored as opportunities with `competitor_*` types)
- `GET /seo-data/status` → `{provider, configured, features, note}`
- `POST /sites/{id}/competitors-keyword-gap`, `POST /sites/{id}/competitors-backlinks` → `{available:false, reason}` without provider credentials; otherwise `{available:true, estimated:true, source:"provider:<name>", ...}`
- `GET /sites/{id}/external-seo-data?kind` → stored provider snapshots (estimates; never mixed with GSC)
- `POST /crawls/{id}/reanalyze` → `{findings}` (idempotent)

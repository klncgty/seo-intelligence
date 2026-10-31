# Gap analysis (2026-10-30) — current repo vs `docs/advanced-spec.md`

Legend: ✅ exists + tested · 🟡 exists, untested/external-cred-only · ❌ missing

## Ek A (base platform)
| Item | Status | Evidence |
|---|---|---|
| Backend stack FastAPI/SQLAlchemy2/Alembic/PG/HTTPX/lxml/pytest | ✅ | 262 backend tests green |
| Models Site/CrawlRun/CrawlPage/PageLink/AuditFinding/SPD/OLD/GrowthOpportunity/IntegrationCredential | ✅ | migrations 0001–0019 |
| Sites CRUD, crawl lifecycle, pages/findings/summary, page detail | ✅ | `test_api_sites_growth`, `test_crawl_lifecycle` |
| Worker claim (SKIP LOCKED), progress, cancel, stale recovery | ✅ | `test_crawl_lifecycle` |
| Safe crawler (SSRF IP-level, rebinding, alt IP forms, redirects, limits, robots, sitemap, dedupe) | ✅ | `test_crawler_security` (54) |
| Audit rules (17 groups, 30+ rules), idempotent reanalysis | ✅ | `test_audit_engine`, `test_crawl_lifecycle` |
| GSC OAuth (state/CSRF/PKCE, encrypted token), 90d + incremental idempotent sync, separate totals | ✅ (mocked) | `test_google_service`; real creds not verified |
| GA4 sync, conversions, landing pages, GSC≠GA4 | ✅ (mocked) | `test_google_service` |
| Growth opportunities v1 (6 types, priority formula, lifecycle, implemented_at) | ✅ | `test_growth_engine`, `test_api_sites_growth` |
| Competitor Intelligence v1 (CRUD, safe crawl, comparison, gaps, provider interface) | ✅ | `test_competitor_*`, `test_competitors_api` |
| UI pages (Overview, Sites, Site detail, Crawl detail, Action Center, Settings + Phase 4–12 modules) | ✅ | `frontend/src/pages/*`, 15 Vitest tests + `vite build` |
| Docker Compose | 🟡 | written with profiles (`core`, `analytics`, `crawl`, `observability`, `full`); local dev uses `scripts/dev-services.sh` |

## Advanced spec
| Area | Status | Evidence |
|---|---|---|
| §1 ClickHouse/MinIO/Redis, profiles | ✅ | `app/infra/clickhouse.py`, `app/infra/storage.py`, `app/infra/ratelimit.py`, CH migrations `0001`–`0004`, `test_infra.py` |
| §1.3 jobs+DAG+scheduler+SSE | ✅ | `app/jobs.py`, PG migration `0004`, W3C `_traceparent` propagation, `test_jobs.py`, `test_remaining_features.py` |
| §1.4 semantic metric layer | ✅ | `app/semantic.py`, `docs/metrics.md`, `test_semantic_system.py` |
| §1.5 tenant isolation (RLS + CH guard) | ✅ | PG RLS migrations `0005`–`0019`, CH `tenant_query` guard, `test_tenancy_auth.py` |
| §1.6 lineage | ✅ | Lineage fields on all ClickHouse tables and MinIO raw response archival |
| §2 SaaS (org/workspace/project, RBAC, auth, API keys, credits, admin, margin, billing/OIDC flags) | ✅ | `app/auth.py`, `app/services/credits.py`, `app/api/v1/account.py`, `test_tenancy_auth.py`, `test_remaining_features.py` |
| §3 provider framework | ✅ | `app/providers/runtime.py`, `registry.py`, `dataforseo.py`, `free_sources.py`, `test_providers.py`, `test_free_sources.py` |
| §4 normalization (PSL, locale, Turkish) | ✅ | `app/normalize/{domains,text,locale}.py`, Hypothesis `test_normalize.py` |
| §5 Keyword intelligence (explorer, KD-1, KD validation vs rank, intent, SERP/lexical clusters, strategy) | ✅ | `app/services/keywords/*`, `KeywordsModule.tsx`, `test_keywords.py`, `test_remaining_features.py` |
| §6 SERP & rank tracking (projects, DAG, visibility, SoV, volatility + calendar annotations, geo-grid, GSC side-by-side) | ✅ | `app/services/rank_tracking.py`, `RankModule.tsx`, `test_rank_tracking.py`, `test_remaining_features.py` |
| §7 Domain & competitor (overview panels, organic, CTR calibration + cross-tenant k-anonymity, keyword gap, digest) | ✅ | `app/services/domains.py`, `DomainModule.tsx`, `test_domains_mvp.py`, `test_remaining_features.py` |
| §8 Backlinks (index, Common Crawl WAT + web-graph importer, verification crawler, ds-1, intersect, risky, disavow, reclamation) | ✅ | `app/services/backlinks.py`, `app/services/commoncrawl.py`, `BacklinksModule.tsx`, `test_backlinks.py`, `test_remaining_features.py` |
| §9 Web-scale crawler (frontier, Bloom dedupe, crawl trap detector, renderer client, Redis politeness, WARC, domain verification, gzip bomb) | ✅ | `app/services/crawler/frontier.py`, `engine.py`, `test_site_ops.py`, `test_remaining_features.py` |
| §10 Advanced audit (structured data, near-dup, PageRank, link suggestions, templates, health score, diff/regressions, custom extraction) | ✅ | `app/services/advanced_audit.py`, `test_advanced_audit.py` |
| §11 First-party depth (tz, preliminary, sampling/thresholding/bot spike DQ, coverage, segments, funnel, anomaly, STL forecast) | ✅ | `app/services/analytics_depth.py`, `SiteDetail.tsx`, `test_insights_growth_v2.py`, `test_remaining_features.py` |
| §12 Content (SERP brief, on-page checker, coverage score, topical map, inventory) | ✅ | `app/services/content.py`, `ContentAiLocalModule.tsx`, `test_content_ai_local.py` |
| §13 AI visibility (OpenAI/Anthropic/Gemini/Perplexity adapters, multi-sample Wilson CI, sentiment, prompt-injection safety) | ✅ | `app/services/ai_visibility.py`, `ContentAiLocalModule.tsx`, `test_content_ai_local.py`, `test_remaining_features.py` |
| §14 Local SEO (GBP profile, NAP consistency, geo-grid rank map) | ✅ | `app/services/local_seo.py`, `ContentAiLocalModule.tsx`, `test_content_ai_local.py` |
| §15 Growth Engine v2 (confidence caps, bootstrap P10/P50/P90, DiD, stratified split tests, calibration, portfolio capacity plan) | ✅ | `app/services/growth_v2.py`, `Actions.tsx`, `SiteDetail.tsx`, `test_insights_growth_v2.py`, `test_remaining_features.py` |
| §16 Flagged LLM layer ("Verine sor" semantic query translator, `<untrusted_third_party_content>` isolation, JSON schema, eval suite) | ✅ | `app/services/llm_assistant.py`, `ContentAiLocalModule.tsx`, `test_remaining_features.py` |
| §17 Automation/API/webhooks/MCP/tickets | ✅ | `app/services/automation.py`, `app/mcp_server.py`, `ReportsAdminModule.tsx`, `test_automation.py` |
| §18–19 Frontend & UX (all modules, global locale selector, credit preflight 428 dialog, source badges, no fake metrics) | ✅ | `frontend/src/pages/modules/*`, `Layout.tsx`, `test/app.test.tsx` (15 passed) |
| §20–23 Security, Observability, SLOs, Data Quality Contracts, Perf, Docs | ✅ | `app/infra/observability.py`, `app/core/logging.py`, `test_remaining_features.py`, `docs/*` |

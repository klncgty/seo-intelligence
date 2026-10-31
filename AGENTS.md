# AGENTS.md — persistent instructions for coding & operating agents

## Project Overview
SEO Growth Intelligence is a local-first, terminal-based multi-site SEO platform operated entirely via CLI and HTTP API.
- **Backend:** FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL, Python 3.12+, managed with `uv`.
- **Pure Terminal / Agent Driven:** There is NO frontend. The system is operated by AI agents and operators via the root `./seo` CLI wrapper (or `uv run --project backend seo ...`), which outputs pure, deterministic JSON.
- **Root CLI Wrapper:** `./seo <command>` (located at project root).

## Common Agent CLI Commands
Every CLI command produces JSON for fast agent parsing:

```bash
# 1. Health & Status
./seo health                                # Check DB and system health
./seo ready                                 # Check status of all storage and integration dependencies

# 2. Site Management
./seo sites                                 # List all registered sites
./seo site <site_id>                        # Inspect site configuration
./seo site-add <Name> <URL> --hosts <h1...> # Register new site (e.g. ./seo site-add Oleart https://www.oleart.co/ --hosts oleart.co www.oleart.co)
./seo site-update <site_id> --set key=val   # Update site configuration
./seo site-delete <site_id>                 # Remove site

# 3. Crawl & Audit (Inline execution for immediate agent feedback)
./seo crawl --inline <site_id>              # Run an end-to-end inline crawl and audit
./seo crawls <site_id>                      # List crawl history for a site
./seo crawl-status <crawl_id>               # Check progress/status of a crawl
./seo crawl-summary <crawl_id>              # View audit health score (0-100) and findings summary
./seo crawl-reanalyze <crawl_id>            # Re-evaluate all deterministic SEO audit rules
./seo findings <crawl_id>                   # List detailed findings for a crawl
./seo findings <crawl_id> --severity high   # Filter findings by severity (critical, high, medium, low, info)
./seo pages <crawl_id>                      # List crawled pages and status codes
./seo page <page_id>                        # Inspect single crawled page, meta, and issues

# 4. Opportunities & Competitors
./seo opps-generate <site_id>               # Generate evidence-based SEO growth opportunities
./seo opps --site-id <site_id>              # List actionable opportunities
./seo comps <site_id>                       # List competitors
./seo comp-add <site_id> <name> <domain>    # Add competitor
./seo comp-crawl <site_id> <comp_id> --inline # Crawl and compare competitor
./seo comp-compare <site_id>                # Technical, structural, and page-type comparison

# 5. Testing & Verification
cd backend && uv run pytest -q              # Run full backend test suite
```

## Hard Rules for Operating Agents
1. **Never fabricate SEO data:** All metrics carry source classes (`FIRST_PARTY`, `OBSERVED`, `PROVIDER`, `MODELED`). Never invent metrics.
2. **Deterministic Rules & Safe Crawling:** The crawler respects SSRF checks, private network policies, and politeness delays.
3. **Pure Terminal Interface:** Do not reintroduce frontend folders. Output should always be parseable JSON or clean terminal logs.
4. **Token Efficiency:** When querying findings or pages, use filters (`--severity`, `--page-size`) to inspect relevant items without context bloat.

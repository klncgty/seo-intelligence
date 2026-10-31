<div align="center">

```
  ██████╗ ██████╗  ██████╗ ██╗    ██╗████████╗██╗  ██╗
 ██╔════╝ ██╔══██╗██╔═══██╗██║    ██║╚══██╔══╝██║  ██║
 ██║  ███╗██████╔╝██║   ██║██║ █╗ ██║   ██║   ███████║
 ██║   ██║██╔══██╗██║   ██║██║███╗██║   ██║   ██╔══██║
 ╚██████╔╝██║  ██║╚██████╔╝╚███╔███╔╝   ██║   ██║  ██║
  ╚═════╝ ╚═╝  ╚═╝ ╚═════╝  ╚══╝╚══╝    ╚═╝   ╚═╝  ╚═╝
 ██╗███╗   ██╗████████╗███████╗██╗     ██╗     ██╗ ██████╗ ███████╗███╗   ██╗ ██████╗███████╗
 ██║████╗  ██║╚══██╔══╝██╔════╝██║     ██║     ██║██╔════╝ ██╔════╝████╗  ██║██╔════╝██╔════╝
 ██║██╔██╗ ██║   ██║   █████╗  ██║     ██║     ██║██║  ███╗█████╗  ██╔██╗ ██║██║     █████╗  
 ██║██║╚██╗██║   ██║   ██╔══╝  ██║     ██║     ██║██║   ██║██╔══╝  ██║╚██╗██║██║     ██╔══╝  
 ██║██║ ╚████║   ██║   ███████╗███████╗███████╗██║╚██████╔╝███████╗██║ ╚████║╚██████╗███████╗
 ╚═╝╚═╝  ╚═══╝   ╚═╝   ╚══════╝╚══════╝╚══════╝╚═╝ ╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚══════╝
```

### ⚡ Enterprise Autonomous SEO & Growth Intelligence Engine

*Local-first, self-hostable technical crawler, SEMrush-parity audit engine, first-party Google data warehouse, and agent-native terminal platform.*

[![Python](https://img.shields.io/badge/python-3.13+-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-17-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://postgresql.org)
[![ClickHouse](https://img.shields.io/badge/ClickHouse-Latest-FFCC01?style=for-the-badge&logo=clickhouse&logoColor=black)](https://clickhouse.com)
[![uv](https://img.shields.io/badge/uv-Fast%20Packaging-DE5FE9?style=for-the-badge)](https://github.com/astral-sh/uv)
[![MCP](https://img.shields.io/badge/MCP-Native%20Server-4A154B?style=for-the-badge&logo=anthropic&logoColor=white)](https://modelcontextprotocol.io)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

[Architecture](#-architecture) • [Features](#-core-capabilities) • [CLI Manual](#-cli-manual-seo) • [Quickstart](#-quickstart) • [Security](#-ssrf--crawler-safety) • [MCP Server](#-mcp-server-for-agents)

---

</div>

## 🌟 Why Growth Intelligence?

Traditional SEO tools are monolithic, browser-centric web dashboards with opaque scoring algorithms and high monthly SaaS fees. **Growth Intelligence** is designed from the ground up for modern engineering teams and autonomous AI agents:

* 🤖 **Agent-First Terminal Architecture**: Pure CLI (`./seo`) with deterministic JSON outputs and a built-in read-only **Model Context Protocol (MCP)** server.
* 🛡️ **SEMrush-Parity Audit Rules**: 100+ audit rules detecting syntax/schema bugs, invalid JSON-LD entities, unminified inline styles/scripts, internal nofollow leaks, and canonical loops.
* ⚡ **Ultra-Safe Async Crawler**: High-throughput crawler with connection-time IP validation (anti-SSRF), redirect re-checks, bomb limits, robots.txt compliance, and domain politeness.
* 📊 **Verified Provenance**: Every metric carries an explicit source class (`FIRST_PARTY`, `OBSERVED`, `PROVIDER`, `MODELED`, `AI`). Zero invented numbers.
* 🌐 **Self-Hostable & Local-First**: Runs entirely on your local machine with PostgreSQL, ClickHouse, Redis, and MinIO without requiring cloud dependencies.

---

## 🏛 Architecture

```
                       ┌──────────────────────────────────────────────┐
                       │     AI Agents / Operators / Automation       │
                       │  (Claude Code, Cursor, Cron Jobs, Terminal)  │
                       └──────────────────────┬───────────────────────┘
                                              │
                      ┌───────────────────────┴───────────────────────┐
                      ▼                                               ▼
         ┌─────────────────────────┐                     ┌─────────────────────────┐
         │   ./seo CLI (In-Proc)   │                     │  MCP Server (stdio JSON)│
         └────────────┬────────────┘                     └────────────┬────────────┘
                      │                                               │
                      └───────────────────────┬───────────────────────┘
                                              │ In-Process / HTTP
                                              ▼
 ┌────────────────────────────────────────────────────────────────────────────────────────┐
 │                              FastAPI Core Engine & API v1                              │
 │  ┌───────────────────────┬──────────────────────────┬───────────────────────────────┐  │
 │  │      Auth & SaaS      │    Crawler & Audits      │      Integrations & AI        │  │
 │  │  • LOCAL_SINGLE_USER  │  • Connection-time SSRF  │  • Google Search Console      │  │
 │  │  • SERVER (RLS, RBAC) │  • 100+ SEMrush Parity   │  • GA4 / Bing Webmaster       │  │
 │  │  • Fernet Key Secret  │  • Schema.org Validator  │  • DataForSEO / Common Crawl  │  │
 │  └───────────────────────┴──────────────────────────┴───────────────────────────────┘  │
 └─────────────┬──────────────────────────┬───────────────────────┬───────────────────────┘
               │                          │                       │
               ▼                          ▼                       ▼
 ┌──────────────────────────┐ ┌──────────────────────┐ ┌──────────────────────┐
 │       PostgreSQL 17      │ │      ClickHouse      │ │     Redis & MinIO    │
 │ • Tenant-isolated DB     │ │ • Keyword timeseries │ │ • Global rate limit  │
 │ • FOR UPDATE SKIP LOCKED │ │ • SERP rankings      │ │ • Raw WARC snapshots │
 │ • Directed DAG jobs      │ │ • Historical metrics │ │ • Blob cache storage │
 └──────────────────────────┘ └──────────────────────┘ └──────────────────────┘
```

---

## 🚀 Core Capabilities

### 1. 🔍 High-Performance SSRF-Safe Crawler
- Async HTTP client with strict DNS & socket-level IP binding to prevent SSRF against loopback (`127.0.0.1`) and cloud metadata (`169.254.169.254`).
- Streamed body decoding with hard size limits (anti-decompression bomb protection).
- Canonical URL normalization (RFC 3986, Public Suffix List multi-level TLDs, lowercase hosts, sorted tracking-param stripping).

### 2. 📋 SEMrush-Parity Audit Engine
Over 100 deterministic technical rules executed across crawls:
- **Structured Data Validator**: Validates Schema.org objects (`Product`, `Offer`, `Organization`, `LocalBusiness`, `FAQPage`, etc.). Enforces schema contracts (e.g., `shippingRate` must be `MonetaryAmount`, seller entity requirements, and ISO dates).
- **Asset Minification**: Detects unminified inline JavaScript and CSS files by measuring comment ratio, line count, and whitespace overhead.
- **Link Quality**: Detects internal links leaking `rel="nofollow"`, broken redirects, orphan pages, and internal PageRank flow.
- **On-Page SEO**: Canonical consistency, identical `<title>` vs `<h1>` duplicate tags, image missing alt attributes, and heading hierarchy.

### 3. 🎯 Growth Opportunity Engine
- Deterministic opportunity scoring based on GSC query opportunities, keyword cannibalization, content gaps against competitors, and CTR underperformance.
- Estimated monthly revenue impact and ranking difficulty scores calculated from live data.

### 4. 🤖 Agent-Native MCP Server
- Native Model Context Protocol (MCP) server over `stdio` allowing AI assistants like **Claude Code**, **Cursor**, or custom LLM loops to query site health, initiate crawls, inspect audit findings, and execute automatic code fixes.

---

## 💻 CLI Manual (`./seo`)

The root `./seo` wrapper executes all commands in-process with zero overhead and pristine JSON output:

```bash
# Check health of database and storage infrastructure
./seo health

# List monitored sites
./seo sites

# Crawl and audit a site immediately (Inline Worker)
./seo crawl --inline <site_id>

# Fetch structured audit findings from a crawl
./seo findings <crawl_id>

# Inspect crawled page inventory and metrics
./seo pages <crawl_id>

# Run audit summary with health score
./seo crawl-summary <crawl_id>

# Generate evidence-backed growth opportunities
./seo opps-generate <site_id>

# Benchmark against competitor domains
./seo comp-compare <site_id> <competitor_id>
```

---

## 🛠 Quickstart

### Prerequisites
* **macOS** or **Linux**
* **Python 3.13+**
* [uv](https://github.com/astral-sh/uv) (Extremely fast Python package installer)
* PostgreSQL 17, ClickHouse, Redis (or Homebrew / Docker)

### 1. Clone & Set Up Development Services
```bash
git clone https://github.com/klncgty/seo-intelligence.git
cd seo-intelligence

# Start local services (Homebrew: Postgres 17, Redis, ClickHouse, MinIO)
scripts/dev-services.sh start
```

### 2. Configure Environment
```bash
cp .env.example .env

# Generate secure token encryption key
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Paste generated key into TOKEN_ENCRYPTION_KEY in .env
```

### 3. Run Migrations & Dependencies
```bash
cd backend
uv sync
uv run alembic upgrade head
CLICKHOUSE_URL=http://127.0.0.1:8123 uv run seo ch-migrate
cd ..
```

### 4. Run Your First Crawl
```bash
# Add a new site
./seo site-add "My Site" https://example.com/

# Trigger instant crawl
./seo crawl --inline 1

# View health report
./seo crawl-summary 1
```

---

## 🛡 SSRF & Crawler Safety

Any platform crawling arbitrary user-provided URLs faces severe SSRF risks. Growth Intelligence eliminates this through multi-stage security:

```
URL String
   │
   ▼
[ 1. Syntax & Host Parsing ] ──▶ Reject invalid formats / userinfo (@)
   │
   ▼
[ 2. Pre-Resolution DNS ] ────▶ Disallow loopback, RFC 1918, link-local, multicast
   │
   ▼
[ 3. Connection Socket Pin ] ─▶ Re-verify IP directly before TCP handshake
   │
   ▼
[ 4. Redirect Validation ] ───▶ Re-run full pipeline on every 3xx redirect hop
```

---

## 🤖 MCP Server for Agents

Integrate Growth Intelligence directly with **Claude Code** or Cursor:

```bash
claude mcp add growth-intelligence -- uv --directory /path/to/seo-intelligence/backend run seo mcp
```

### Supported MCP Tools:
* `get_site_health`: Returns overall health score and severity breakdown.
* `list_findings`: Returns filtered SEO audit findings with root cause and remediation.
* `trigger_crawl`: Initiates a new crawl run.
* `get_growth_opportunities`: Returns ranked growth suggestions.

---

## 🧪 Test Suite

Run the full end-to-end and unit test suite:

```bash
cd backend
uv run pytest tests/test_advanced_audit.py tests/test_audit_engine.py -v
```

---

<div align="center">

Crafted with precision for AI Agents & Modern SEO Engineers • **Growth Intelligence**

</div>

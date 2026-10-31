"""`seo` — operator CLI (used by Claude Code as a tool). Every command prints JSON.

Calls the same FastAPI routes in-process (same validation as HTTP), or a running server with
--api http://127.0.0.1:8000. `--inline` runs crawl/sync jobs in this process instead of the worker.
"""

import argparse
import asyncio
import json
import sys

import httpx


def _client(api: str | None):
    if api:
        return httpx.Client(base_url=api, timeout=120)
    from fastapi.testclient import TestClient

    from app.main import app
    return TestClient(app, raise_server_exceptions=False)


def _kv(pairs: list[str] | None) -> dict:
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        try:
            out[k] = json.loads(v)
        except json.JSONDecodeError:
            out[k] = v
    return out


def _params(a, *names) -> dict:
    return {n: getattr(a, n) for n in names if getattr(a, n, None) is not None}


def _inline_crawl(crawl_id: int) -> None:
    from app.db.session import SessionLocal
    from app.models import CrawlRun
    from app.services import crawl_service

    with SessionLocal() as db:
        run = db.get(CrawlRun, crawl_id, with_for_update=True)
        if run.status != "queued":
            db.rollback()
            return
        run.status, run.started_at, run.heartbeat_at, run.worker_id = "running", crawl_service.now(), crawl_service.now(), "cli"
        db.commit()
    asyncio.run(crawl_service.execute_crawl(crawl_id))


def _inline_sync(job_id: int) -> None:
    from app.db.session import SessionLocal
    from app.models import SyncJob
    from app.services import google_service

    with SessionLocal() as db:
        job = db.get(SyncJob, job_id, with_for_update=True)
        if job.status != "queued":
            db.rollback()
            return
        job.status, job.worker_id = "running", "cli"
        db.commit()
        asyncio.run(google_service.run_sync_job(db, job))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="seo", description=__doc__)
    p.add_argument("--api", help="use a running API instead of in-process calls")
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name, help_, *args):
        sp = sub.add_parser(name, help=help_)
        for a in args:
            flags, kw = (a[0], a[1]) if isinstance(a, tuple) else (a, {})
            sp.add_argument(*([flags] if isinstance(flags, str) else flags), **kw)
        return sp

    D = [("--start", {}), ("--end", {})]
    PG = [("--page", {"type": int}), ("--page-size", {"type": int, "dest": "page_size"})]
    cmd("health", "API/DB health")
    cmd("overview", "multi-site dashboard", *D)
    cmd("sites", "list sites", *PG)
    cmd("site", "show site", ("site_id", {"type": int}))
    cmd("site-add", "add site", "name", "base_url", ("--hosts", {"nargs": "*"}), ("--gsc", {}), ("--ga4", {}),
        ("--set", {"nargs": "*", "help": "extra fields key=json"}))
    cmd("site-update", "patch site: --set key=json ...", ("site_id", {"type": int}), ("--set", {"nargs": "+"}))
    cmd("site-delete", "delete site", ("site_id", {"type": int}))
    cmd("crawl", "start crawl", ("site_id", {"type": int}), ("--inline", {"action": "store_true"}),
        ("--set", {"nargs": "*", "help": "crawl_settings overrides key=value"}))
    cmd("crawls", "list crawls of a site", ("site_id", {"type": int}), *PG)
    cmd("crawl-status", "crawl status", ("crawl_id", {"type": int}))
    cmd("crawl-cancel", "cancel crawl", ("crawl_id", {"type": int}))
    cmd("crawl-summary", "audit summary", ("crawl_id", {"type": int}))
    cmd("crawl-reanalyze", "rerun audit rules", ("crawl_id", {"type": int}))
    cmd("findings", "list findings", ("crawl_id", {"type": int}), ("--severity", {}), ("--rule-id", {"dest": "rule_id"}), *PG)
    cmd("pages", "list crawled pages", ("crawl_id", {"type": int}), ("--q", {}), ("--status-code", {"type": int, "dest": "status_code"}), *PG)
    cmd("page", "page detail", ("page_id", {"type": int}))
    cmd("google-status", "Google OAuth status")
    cmd("google-connect", "print Google authorization URL")
    cmd("google-properties", "list GSC/GA4 properties")
    cmd("google-disconnect", "delete stored Google credentials")
    cmd("sync", "sync GSC or GA4", ("source", {"choices": ["gsc", "ga4"]}), ("site_id", {"type": int}),
        ("--inline", {"action": "store_true"}))
    cmd("integration", "per-site integration status", ("source", {"choices": ["gsc", "ga4"]}), ("site_id", {"type": int}))
    cmd("performance", "GSC performance", ("site_id", {"type": int}), ("--dimension", {}), *D, *PG)
    cmd("landing", "GA4 organic landing pages", ("site_id", {"type": int}), ("--sort", {}), *D, *PG)
    cmd("conversions", "GA4 conversions", ("site_id", {"type": int}), *D)
    cmd("growth", "period comparison", ("site_id", {"type": int}), *D)
    cmd("opps-generate", "generate growth opportunities", ("site_id", {"type": int}), *D)
    cmd("opps", "list opportunities", ("--site-id", {"type": int, "dest": "site_id"}), ("--status", {}), ("--type", {}),
        ("--min-confidence", {"type": float, "dest": "min_confidence"}), ("--sort", {}), *PG)
    cmd("opp", "show opportunity", ("opp_id", {"type": int}))
    cmd("opp-set", "update opportunity", ("opp_id", {"type": int}), ("--status", {}), ("--notes", {}))
    cmd("comp-add", "add competitor", ("site_id", {"type": int}), "name", "domain", ("--notes", {}))
    cmd("comps", "list competitors", ("site_id", {"type": int}))
    cmd("comp-update", "patch competitor", ("site_id", {"type": int}), ("comp_id", {"type": int}), ("--set", {"nargs": "+"}))
    cmd("comp-delete", "delete competitor", ("site_id", {"type": int}), ("comp_id", {"type": int}))
    cmd("comp-crawl", "crawl competitor", ("site_id", {"type": int}), ("comp_id", {"type": int}), ("--inline", {"action": "store_true"}))
    cmd("comp-suggest", "competitor suggestions (not auto-added)", ("site_id", {"type": int}))
    cmd("comp-compare", "technical/structure/page-type comparison", ("site_id", {"type": int}))
    cmd("comp-gaps", "content gaps vs own GSC", ("site_id", {"type": int}), ("--limit", {"type": int}))
    cmd("comp-opps", "generate competitor opportunities", ("site_id", {"type": int}))
    cmd("comp-keyword-gap", "keyword gap via external provider", ("site_id", {"type": int}))
    cmd("comp-backlinks", "backlink comparison via external provider", ("site_id", {"type": int}))
    cmd("tag-intel", "reverse engineer competitor tags and get winning rank #1 blueprint",
        ("--keyword", {"required": True}),
        ("--url", {}),
        ("--comp", {"action": "append", "dest": "competitor_urls"}),
        ("--site-id", {"type": int, "dest": "site_id"}),
        ("--lang", {"default": "tr"}))
    cmd("seo-data-status", "external SEO data provider status")
    cmd("crawler-settings", "crawler safety limits")
    cmd("worker", "run the background worker")
    cmd("ready", "readiness of all dependencies")
    cmd("jobs", "list generic jobs", ("--status", {}), ("--kind", {}), *PG)
    cmd("job", "show job", ("job_id", {"type": int}))
    cmd("job-cancel", "cancel job", ("job_id", {"type": int}))
    cmd("job-retry", "retry dead/cancelled job", ("job_id", {"type": int}))
    cmd("metrics", "semantic metric catalog")
    cmd("metrics-doc", "regenerate docs/metrics.md")
    cmd("ch-migrate", "apply ClickHouse migrations")
    cmd("mcp", "run the read-only MCP server on stdio (for Claude)")
    cmd("rotate-keys", "re-encrypt all stored secrets with the primary TOKEN_ENCRYPTION_KEYS key")
    cmd("psl-update", "refresh the Public Suffix List snapshot (review + commit the diff)")
    cmd("providers", "provider status + budget")
    cmd("org-create", "create organization + owner (SERVER mode bootstrap)", "name", "email", ("--plan", {"default": "starter"}),
        ("--platform-admin", {"action": "store_true"}))
    cmd("serve", "run the API (refuses public bind unless APP_MODE=SERVER)", ("--host", {"default": "127.0.0.1"}),
        ("--port", {"type": int, "default": 8000}))
    return p


def route(a) -> tuple[str, str, dict | None, dict | None]:
    """Map parsed args to (method, path, params, json)."""
    c, sid = a.cmd, getattr(a, "site_id", None)
    pg = _params(a, "page", "page_size")
    dr = _params(a, "start", "end")
    src = {"gsc": "search-console", "ga4": "analytics"}
    table = {
        "health": ("GET", "/health", None, None),
        "overview": ("GET", "/api/v1/dashboard/overview", dr, None),
        "sites": ("GET", "/api/v1/sites", pg, None),
        "site": ("GET", f"/api/v1/sites/{sid}", None, None),
        "site-delete": ("DELETE", f"/api/v1/sites/{sid}", None, None),
        "crawls": ("GET", f"/api/v1/sites/{sid}/crawls", pg, None),
        "google-status": ("GET", "/api/v1/integrations/google/status", None, None),
        "google-connect": ("POST", "/api/v1/integrations/google/connect", None, None),
        "google-properties": ("GET", "/api/v1/integrations/google/properties", None, None),
        "google-disconnect": ("POST", "/api/v1/integrations/google/disconnect", None, None),
        "performance": ("GET", f"/api/v1/sites/{sid}/search-performance", {**dr, **pg, **_params(a, "dimension")}, None),
        "landing": ("GET", f"/api/v1/sites/{sid}/landing-pages", {**dr, **pg, **_params(a, "sort")}, None),
        "conversions": ("GET", f"/api/v1/sites/{sid}/conversions", dr, None),
        "growth": ("GET", f"/api/v1/sites/{sid}/growth", dr, None),
        "opps-generate": ("POST", f"/api/v1/sites/{sid}/opportunities/generate", dr, None),
        "opps": ("GET", "/api/v1/opportunities", {**pg, **_params(a, "site_id", "status", "type", "min_confidence", "sort")}, None),
        "comps": ("GET", f"/api/v1/sites/{sid}/competitors", None, None),
        "comp-suggest": ("GET", f"/api/v1/sites/{sid}/competitors/suggestions", None, None),
        "comp-compare": ("GET", f"/api/v1/sites/{sid}/competitors-comparison", None, None),
        "comp-gaps": ("GET", f"/api/v1/sites/{sid}/competitors-content-gaps", _params(a, "limit"), None),
        "comp-opps": ("POST", f"/api/v1/sites/{sid}/competitors-opportunities/generate", None, None),
        "comp-keyword-gap": ("POST", f"/api/v1/sites/{sid}/competitors-keyword-gap", None, None),
        "comp-backlinks": ("POST", f"/api/v1/sites/{sid}/competitors-backlinks", None, None),
        "seo-data-status": ("GET", "/api/v1/seo-data/status", None, None),
        "crawler-settings": ("GET", "/api/v1/settings/crawler", None, None),
        "ready": ("GET", "/ready", None, None),
        "jobs": ("GET", "/api/v1/jobs", {**pg, **_params(a, "status", "kind")}, None),
        "metrics": ("GET", "/api/v1/metrics/catalog", None, None),
        "providers": ("GET", "/api/v1/providers/status", None, None),
    }
    if c in table:
        return table[c]
    if c == "site-add":
        body = {"name": a.name, "base_url": a.base_url, **_kv(a.set)}
        if a.hosts:
            body["allowed_hosts"] = a.hosts
        if a.gsc:
            body["gsc_property"] = a.gsc
        if a.ga4:
            body["ga4_property_id"] = a.ga4
        return "POST", "/api/v1/sites", None, body
    if c == "site-update":
        return "PATCH", f"/api/v1/sites/{sid}", None, _kv(a.set)
    if c == "crawl":
        return "POST", f"/api/v1/sites/{sid}/crawls", None, {"crawl_settings": _kv(a.set)}
    if c.startswith("crawl-"):
        action = {"crawl-status": ("GET", ""), "crawl-cancel": ("POST", "/cancel"), "crawl-summary": ("GET", "/summary"),
                  "crawl-reanalyze": ("POST", "/reanalyze")}[c]
        return action[0], f"/api/v1/crawls/{a.crawl_id}{action[1]}", None, None
    if c == "findings":
        return "GET", f"/api/v1/crawls/{a.crawl_id}/findings", {**pg, **_params(a, "severity", "rule_id")}, None
    if c == "pages":
        return "GET", f"/api/v1/crawls/{a.crawl_id}/pages", {**pg, **_params(a, "q", "status_code")}, None
    if c == "page":
        return "GET", f"/api/v1/pages/{a.page_id}", None, None
    if c == "sync":
        return "POST", f"/api/v1/sites/{sid}/sync/{src[a.source]}", None, None
    if c == "integration":
        return "GET", f"/api/v1/sites/{sid}/integrations/{src[a.source]}", None, None
    if c.startswith("job"):
        suffix = {"job": "", "job-cancel": "/cancel", "job-retry": "/retry"}[c]
        return ("GET" if c == "job" else "POST"), f"/api/v1/jobs/{a.job_id}{suffix}", None, None
    if c == "opp":
        return "GET", f"/api/v1/opportunities/{a.opp_id}", None, None
    if c == "opp-set":
        return "PATCH", f"/api/v1/opportunities/{a.opp_id}", None, _params(a, "status", "notes")
    if c == "comp-add":
        return "POST", f"/api/v1/sites/{sid}/competitors", None, {"name": a.name, "domain": a.domain, **_params(a, "notes")}
    if c == "comp-update":
        return "PATCH", f"/api/v1/sites/{sid}/competitors/{a.comp_id}", None, _kv(a.set)
    if c == "comp-delete":
        return "DELETE", f"/api/v1/sites/{sid}/competitors/{a.comp_id}", None, None
    if c == "comp-crawl":
        return "POST", f"/api/v1/sites/{sid}/competitors/{a.comp_id}/crawls", None, None
    if c == "tag-intel":
        body = {
            "keyword": a.keyword,
            "url": getattr(a, "url", None),
            "competitor_urls": getattr(a, "competitor_urls", None) or [],
            "site_id": getattr(a, "site_id", None),
            "lang": getattr(a, "lang", "tr") or "tr",
        }
        return "POST", "/api/v1/content/tag-intelligence", None, body
    raise SystemExit(f"unknown command {c}")


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "metrics-doc":
        import pathlib

        from app.semantic import render_markdown
        out = pathlib.Path(__file__).resolve().parents[2] / "docs" / "metrics.md"
        out.write_text(render_markdown())
        print(json.dumps({"written": str(out)}))
        return 0
    if a.cmd == "rotate-keys":
        from app.core.crypto import rotate_all
        from app.db.session import SessionLocal
        with SessionLocal() as db:
            print(json.dumps({"rotated": rotate_all(db)}))
        return 0
    if a.cmd == "mcp":
        from app.mcp_server import main as mcp_main
        mcp_main()
        return 0
    if a.cmd == "psl-update":
        from app.normalize.domains import update_psl
        print(json.dumps(update_psl()))
        return 0
    if a.cmd == "org-create":
        import getpass
        import os

        from app.api.v1.account import bootstrap_org
        from app.db.session import SessionLocal
        pw = os.environ.get("SEO_OWNER_PASSWORD") or getpass.getpass("Owner password (min 12 chars): ")
        with SessionLocal() as db:
            org, user = bootstrap_org(db, a.name, a.email, pw, plan=a.plan, platform_admin=a.platform_admin)
        print(json.dumps({"org_id": org.id, "user_id": user.id, "plan": org.plan}))
        return 0
    if a.cmd == "serve":
        import ipaddress

        import uvicorn

        from app.core.config import get_settings
        try:
            loopback = ipaddress.ip_address(a.host).is_loopback
        except ValueError:
            loopback = a.host == "localhost"
        if not loopback and get_settings().app_mode != "SERVER":
            print(json.dumps({"error": "refusing to bind a non-loopback address without APP_MODE=SERVER (auth)"}))
            return 2
        uvicorn.run("app.main:app", host=a.host, port=a.port)
        return 0
    if a.cmd == "ch-migrate":
        from app.infra.clickhouse import ClickHouse
        print(json.dumps({"applied": ClickHouse().migrate()}))
        return 0
    if a.cmd == "worker":
        from app.worker import main as worker_main
        asyncio.run(worker_main())
        return 0
    method, path, params, body = route(a)
    with _client(a.api) as client:
        r = client.request(method, path, params=params, json=body)
        data = r.json() if r.content else {"status": r.status_code}
        if getattr(a, "inline", False) and r.status_code == 202 and not a.api:
            if "job_id" in data:
                _inline_sync(data["job_id"])
                data = client.get(path.replace("/sync/", "/integrations/")).json()
            else:
                _inline_crawl(data["id"])
                data = client.get(f"/api/v1/crawls/{data['id']}/summary").json()
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    return 0 if r.status_code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())

"""Automation & reporting (spec §17): notification channels, alert rules, weekly digest, white-label reports,
exports with credit cost, signed share links, issue-tracker tickets.

Webhook signature: header `X-SGI-Signature: t=<unix>,v1=<hex HMAC-SHA256(secret, f"{t}.{body}")>`; receivers should reject
timestamps older than 5 minutes. Every user-supplied URL (webhook/Slack/Teams) goes through the SSRF policy.
"""

import csv
import hashlib
import hmac
import html as htmlmod
import io
import json
import smtplib
import time
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import crypto
from app.core.config import get_settings
from app.core.security import new_token, sha256
from app.models import (
    AlertEvent,
    AlertRule,
    CrawlRun,
    GrowthOpportunity,
    NotificationChannel,
    RankAlert,
    ShareLink,
    Site,
    TicketLink,
)
from app.services import metrics
from app.services.crawler.fetcher import safe_post

SLACK_HOSTS = ("hooks.slack.com",)
TEAMS_SUFFIX = (".webhook.office.com", ".logic.azure.com")


# ---------------------------------------------------------------- channels & delivery

def channel_config(ch: NotificationChannel) -> dict:
    return json.loads(crypto.decrypt(ch.config_encrypted))


def validate_channel(kind: str, cfg: dict) -> dict:
    from app.schemas import _validate_base_url
    if kind in ("webhook", "slack", "teams"):
        url = _validate_base_url(cfg.get("url", ""))
        host = httpx.URL(url).host or ""
        if kind == "slack" and host not in SLACK_HOSTS:
            raise ValueError("Slack channels must use an incoming-webhook URL on hooks.slack.com")
        if kind == "teams" and not host.endswith(TEAMS_SUFFIX):
            raise ValueError("Teams channels must use a Teams/Power Automate webhook URL")
        out = {"url": url}
        if kind == "webhook":
            out["secret"] = cfg.get("secret") or new_token(24)[0]
        return out
    if kind == "email":
        to = str(cfg.get("to", ""))
        if "@" not in to or len(to) > 320:
            raise ValueError("email channel needs a 'to' address")
        return {"to": to}
    raise ValueError("kind must be webhook, slack, teams or email")


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_signature(secret: str, body: bytes, header: str, tolerance_s: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    if abs(int(time.time()) - int(parts.get("t", 0))) > tolerance_s:
        return False
    expected = sign(secret, body, int(parts["t"])).split("v1=")[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


async def deliver(ch: NotificationChannel, title: str, payload: dict, transport=None) -> dict:
    cfg = channel_config(ch)
    s = get_settings()
    if ch.kind == "email":
        if not s.smtp_host:
            return {"channel_id": ch.id, "ok": False, "error": "SMTP not configured"}
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = title, s.smtp_from or s.smtp_user, cfg["to"]
        msg.set_content(json.dumps(payload, indent=2, ensure_ascii=False, default=str))

        def send():
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=20) as smtp:
                smtp.starttls()
                if s.smtp_user:
                    smtp.login(s.smtp_user, s.smtp_password)
                smtp.send_message(msg)
        import asyncio
        try:
            await asyncio.to_thread(send)
            return {"channel_id": ch.id, "ok": True}
        except Exception as exc:
            return {"channel_id": ch.id, "ok": False, "error": type(exc).__name__}
    if ch.kind == "slack":
        body = json.dumps({"text": f"*{title}*\n```{json.dumps(payload, ensure_ascii=False, default=str)[:2500]}```"}).encode()
        headers = {}
    elif ch.kind == "teams":
        body = json.dumps({"text": f"**{title}**\n\n{json.dumps(payload, ensure_ascii=False, default=str)[:2500]}"}).encode()
        headers = {}
    else:
        body = json.dumps({"title": title, "payload": payload, "sent_at": datetime.now(UTC).isoformat()}, default=str).encode()
        headers = {"X-SGI-Signature": sign(cfg["secret"], body)}
    status, err = await safe_post(cfg["url"], body, headers, allow_private=s.crawler_allow_private_networks, transport=transport)
    return {"channel_id": ch.id, "ok": err is None, "status": status, "error": err}


# ---------------------------------------------------------------- alert evaluation

def _candidates(db: Session, rule: AlertRule) -> list[tuple[str, dict]]:
    """Return (subject_key, payload) pairs that currently violate the rule."""
    out = []
    site = db.get(Site, rule.site_id) if rule.site_id else None
    cfg = rule.config or {}
    if rule.rule_type == "metric_change" and site:
        metric = cfg.get("metric", "clicks")
        days = int(cfg.get("window_days", 7))
        end = date.today() - timedelta(days=3)
        start = end - timedelta(days=days - 1)
        pend = start - timedelta(days=1)
        cur = metrics.site_metrics(db, site.id, start, end).get(metric)
        prev = metrics.site_metrics(db, site.id, pend - timedelta(days=days - 1), pend).get(metric)
        if cur is None or prev is None or prev < max(rule.min_volume, 1):
            return []
        change = (cur - prev) / prev * 100
        thr = float(cfg.get("threshold_pct", -20))
        if (thr < 0 and change <= thr) or (thr > 0 and change >= thr):
            out.append((f"{metric}:{end}", {"metric": metric, "current": cur, "previous": prev, "change_pct": round(change, 1),
                                            "window": [start.isoformat(), end.isoformat()], "site": site.base_url}))
    elif rule.rule_type == "anomaly" and site:
        from app.services.analytics_depth import anomalies
        for a in anomalies(db, site, cfg.get("metric", "gsc_clicks"), days=7)["anomalies"]:
            if a["baseline"] >= rule.min_volume:
                out.append((f"anomaly:{a['date']}", {**a, "site": site.base_url}))
    elif rule.rule_type == "rank_alerts":
        since = rule.last_fired_at or datetime.now(UTC) - timedelta(days=1)
        q = select(RankAlert).where(RankAlert.created_at > since)
        if cfg.get("rank_project_id"):
            q = q.where(RankAlert.rank_project_id == int(cfg["rank_project_id"]))
        for a in db.scalars(q.order_by(RankAlert.id)):
            out.append((f"rank:{a.id}", {"type": a.alert_type, "keyword": a.keyword, "details": a.details}))
    elif rule.rule_type == "crawl_regression" and site:
        from app.services.advanced_audit import crawl_diff
        last = db.scalar(select(CrawlRun).where(CrawlRun.site_id == site.id, CrawlRun.competitor_id.is_(None),
                                                CrawlRun.status == "completed").order_by(CrawlRun.id.desc()).limit(1))
        if last:
            d = crawl_diff(db, last.id)
            for a in d.get("regression_alerts", []):
                out.append((f"crawl:{last.id}:{a['type']}:{a.get('url')}", a))
    return out


async def evaluate_rules(db: Session, transport=None) -> dict:
    now = datetime.now(UTC)
    fired = suppressed = 0
    for rule in db.scalars(select(AlertRule).where(AlertRule.enabled.is_(True)).order_by(AlertRule.id)).all():
        if rule.snoozed_until and rule.snoozed_until > now:
            continue
        for subject, payload in _candidates(db, rule):
            dk = f"rule:{rule.id}:{subject}"
            if db.scalar(select(AlertEvent.id).where(AlertEvent.dedupe_key == dk)):
                continue  # dedupe
            if rule.last_fired_at and now - rule.last_fired_at < timedelta(hours=rule.cooldown_hours) and rule.rule_type != "rank_alerts":
                suppressed += 1
                continue  # cooldown
            ev = AlertEvent(org_id=rule.org_id, rule_id=rule.id, dedupe_key=dk, severity=rule.severity, payload=payload, deliveries=[])
            db.add(ev)
            db.flush()
            chans = db.scalars(select(NotificationChannel).where(NotificationChannel.id.in_(rule.channel_ids or [0]),
                                                                 NotificationChannel.enabled.is_(True))).all()
            ev.deliveries = [await deliver(c, f"[{rule.severity}] {rule.name}", payload, transport) for c in chans]
            rule.last_fired_at = now
            fired += 1
            db.commit()
    return {"fired": fired, "suppressed_by_cooldown": suppressed}


# ---------------------------------------------------------------- digest / reports

def weekly_digest(db: Session, site: Site) -> dict:
    end = date.today() - timedelta(days=3)
    start = end - timedelta(days=6)
    pend = start - timedelta(days=1)
    pstart = pend - timedelta(days=6)
    cur, prev = metrics.site_metrics(db, site.id, start, end), metrics.site_metrics(db, site.id, pstart, pend)
    opps = db.scalars(select(GrowthOpportunity).where(GrowthOpportunity.site_id == site.id,
                                                      GrowthOpportunity.status.in_(("new", "planned", "in_progress")))
                      .order_by(GrowthOpportunity.priority_score.desc()).limit(10)).all()
    from app.services.analytics_depth import anomalies
    an = anomalies(db, site, "gsc_clicks", days=7)["anomalies"]
    last = db.scalar(select(CrawlRun).where(CrawlRun.site_id == site.id, CrawlRun.competitor_id.is_(None), CrawlRun.status == "completed")
                     .order_by(CrawlRun.id.desc()).limit(1))
    return {"site": {"id": site.id, "name": site.name, "base_url": site.base_url}, "period": [start.isoformat(), end.isoformat()],
            "compare": [pstart.isoformat(), pend.isoformat()], "metrics": {"current": cur, "previous": prev,
                                                                              "change_pct": metrics.change_pct(cur, prev)},
            "source_classes": {"clicks": "FIRST_PARTY (GSC)", "sessions": "FIRST_PARTY (GA4)"},
            "top_opportunities": [{"id": o.id, "title": o.title, "priority": o.priority_score, "confidence": o.confidence} for o in opps],
            "anomalies": an, "health_score": last.health_score if last else None}


def render_report_html(digest: dict, branding: dict | None) -> str:
    b = branding or {}
    name = htmlmod.escape(str(b.get("name") or "Growth Intelligence"))
    color = b.get("color") if isinstance(b.get("color"), str) and len(b.get("color", "")) <= 9 and b.get("color", "").startswith("#") else "#1f6feb"
    def fmt(v):
        return "—" if v is None else (f"{v:,.0f}" if isinstance(v, (int, float)) else htmlmod.escape(str(v)))
    rows = "".join(f"<tr><td>{htmlmod.escape(k)}</td><td>{fmt(digest['metrics']['current'].get(k))}</td>"
                   f"<td>{fmt(digest['metrics']['previous'].get(k))}</td><td>{fmt(digest['metrics']['change_pct'].get(k))}</td></tr>"
                   for k in ("clicks", "impressions", "sessions", "signups", "purchases", "revenue"))
    opps = "".join(f"<li>{htmlmod.escape(o['title'])} <small>(priority {o['priority']})</small></li>" for o in digest["top_opportunities"])
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{name} — {htmlmod.escape(digest['site']['name'])}</title>"
            f"<style>body{{font-family:system-ui;margin:2rem}}h1{{color:{color}}}td,th{{padding:4px 10px;border-bottom:1px solid #ddd}}</style>"
            f"</head><body><h1>{name}</h1><h2>{htmlmod.escape(digest['site']['name'])} · {digest['period'][0]} – {digest['period'][1]}</h2>"
            f"<table><tr><th>Metric</th><th>Current</th><th>Previous</th><th>Δ %</th></tr>{rows}</table>"
            f"<p><small>Clicks/impressions: Google Search Console (first-party). Sessions/conversions: GA4 (first-party). "
            f"“—” means no data.</small></p><h3>Top opportunities</h3><ol>{opps or '<li>None</li>'}</ol>"
            f"<p>Site health score: {fmt(digest['health_score'])} (quality indicator, not a ranking score)</p></body></html>")


# ---------------------------------------------------------------- exports

EXPORTS = {
    "opportunities": ["id", "site_id", "opportunity_type", "title", "status", "priority_score", "confidence", "effort", "url",
                      "data_sources", "impact_estimate"],
}


def export_rows(db: Session, kind: str, site_id: int | None) -> list[dict]:
    if kind != "opportunities":
        raise ValueError("unsupported export")
    q = select(GrowthOpportunity).order_by(GrowthOpportunity.priority_score.desc())
    if site_id:
        q = q.where(GrowthOpportunity.site_id == site_id)
    from app.services.growth_v2 import source_class_of
    out = []
    for o in db.scalars(q):
        out.append({"id": o.id, "site_id": o.site_id, "opportunity_type": o.opportunity_type, "title": o.title, "status": o.status,
                    "priority_score": o.priority_score, "confidence": o.confidence, "effort": o.effort, "url": o.url,
                    "data_sources": ";".join(o.data_sources or []),
                    "source_classes": ";".join(sorted({source_class_of(s) for s in o.data_sources or []})),
                    "impact_estimate": json.dumps(o.impact_estimate, ensure_ascii=False) if o.impact_estimate else ""})
    return out


def to_csv(rows: list[dict]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0]))
    w.writeheader()
    for r in rows:  # neutralize spreadsheet formula injection
        w.writerow({k: (f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v) for k, v in r.items()})
    return buf.getvalue().encode("utf-8-sig")


def to_xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    if rows:
        ws.append(list(rows[0]))
        for r in rows:
            ws.append([(f"'{v}" if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v) for v in r.values()])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- share links

def create_share(db: Session, org_id: int, resource: str, ttl_hours: int) -> tuple[str, ShareLink]:
    token, th = new_token(32)
    link = ShareLink(org_id=org_id, token_hash=th, resource=resource, expires_at=datetime.now(UTC) + timedelta(hours=ttl_hours))
    db.add(link)
    db.commit()
    return token, link


def resolve_share(db: Session, token: str) -> ShareLink | None:
    link = db.scalar(select(ShareLink).where(ShareLink.token_hash == sha256(token)))
    if link is None or link.revoked_at or link.expires_at < datetime.now(UTC):
        return None
    return link


# ---------------------------------------------------------------- issue trackers

async def create_ticket(db: Session, o: GrowthOpportunity, tracker: str, client: httpx.AsyncClient | None = None) -> TicketLink:
    s = get_settings()
    title = f"[SEO] {o.title}"[:250]
    body = (f"{o.description}\n\nRecommended action: {o.recommended_action}\nValidation: {o.validation_metric}\n"
            f"Evidence: {json.dumps(o.evidence, ensure_ascii=False, default=str)[:3000]}\nLimitations: {o.limitations}")
    own = client is None
    c = client or httpx.AsyncClient(timeout=30, trust_env=False)
    try:
        if tracker == "github":
            if not (s.github_token and s.github_repo):
                raise ValueError("GitHub not configured (GITHUB_TOKEN, GITHUB_REPO)")
            r = await c.post(f"https://api.github.com/repos/{s.github_repo}/issues", json={"title": title, "body": body, "labels": ["seo"]},
                             headers={"Authorization": f"Bearer {s.github_token}", "Accept": "application/vnd.github+json"})
            r.raise_for_status()
            d = r.json()
            ext, url = str(d["number"]), d["html_url"]
        elif tracker == "jira":
            if not (s.jira_base_url and s.jira_email and s.jira_api_token and s.jira_project_key):
                raise ValueError("Jira not configured")
            r = await c.post(f"{s.jira_base_url.rstrip('/')}/rest/api/3/issue", auth=(s.jira_email, s.jira_api_token), json={
                "fields": {"project": {"key": s.jira_project_key}, "summary": title, "issuetype": {"name": "Task"},
                           "description": {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": body[:30000]}]}]}}})
            r.raise_for_status()
            d = r.json()
            ext, url = d["key"], f"{s.jira_base_url.rstrip('/')}/browse/{d['key']}"
        elif tracker == "linear":
            if not (s.linear_api_key and s.linear_team_id):
                raise ValueError("Linear not configured")
            r = await c.post("https://api.linear.app/graphql", headers={"Authorization": s.linear_api_key}, json={
                "query": "mutation($i: IssueCreateInput!){issueCreate(input:$i){issue{id identifier url}}}",
                "variables": {"i": {"teamId": s.linear_team_id, "title": title, "description": body}}})
            r.raise_for_status()
            issue = r.json()["data"]["issueCreate"]["issue"]
            ext, url = issue["id"], issue["url"]
        else:
            raise ValueError("tracker must be github, jira or linear")
    finally:
        if own:
            await c.aclose()
    site = db.get(Site, o.site_id)
    link = TicketLink(org_id=site.org_id, opportunity_id=o.id, tracker=tracker, external_id=ext, url=url, state="open",
                      synced_at=datetime.now(UTC))
    db.add(link)
    if o.status == "new":
        o.status = "planned"
    db.commit()
    return link


async def sync_github(db: Session, link: TicketLink, client: httpx.AsyncClient | None = None) -> TicketLink:
    s = get_settings()
    own = client is None
    c = client or httpx.AsyncClient(timeout=30, trust_env=False)
    try:
        r = await c.get(f"https://api.github.com/repos/{s.github_repo}/issues/{link.external_id}",
                        headers={"Authorization": f"Bearer {s.github_token}", "Accept": "application/vnd.github+json"})
        r.raise_for_status()
        d = r.json()
    finally:
        if own:
            await c.aclose()
    link.state, link.synced_at = d["state"], datetime.now(UTC)
    o = db.get(GrowthOpportunity, link.opportunity_id)
    if d["state"] == "closed" and o.status in ("planned", "in_progress", "new"):
        o.status = "implemented"
        o.implemented_at = datetime.fromisoformat(d["closed_at"].replace("Z", "+00:00")) if d.get("closed_at") else datetime.now(UTC)
    db.commit()
    return link



from app import jobs as _jobs  # noqa: E402


@_jobs.handler("alerts_evaluate")
async def _alerts_job(ctx, payload):
    from app.db.session import SessionLocal
    with SessionLocal() as db:  # system context: evaluates every tenant's rules
        return await evaluate_rules(db)


@_jobs.handler("weekly_digest")
async def _digest_job(ctx, payload):
    from app.db.session import SessionLocal
    with SessionLocal() as db:
        site = db.get(Site, payload["site_id"])
        d = weekly_digest(db, site)
        chans = db.scalars(select(NotificationChannel).where(NotificationChannel.id.in_(payload.get("channel_ids") or [0]))).all()
        return {"deliveries": [await deliver(c, f"Weekly digest — {site.name}", d) for c in chans]}


@_jobs.handler("async_wrapper")
async def _async_wrapper(ctx, payload):
    import asyncio

    from app.auth import scoped_session
    from app.db.session import SessionLocal
    from app.infra.clickhouse import ClickHouse
    kind, params, org_id = payload["kind"], payload.get("params") or {}, payload["org_id"]
    if kind == "opportunities_regenerate":
        from app.services import growth_v2
        with scoped_session(org_id) as db:
            site = db.get(Site, int(params["site_id"]))
            ch = ClickHouse()
            result = growth_v2.regenerate(db, site, ch if ch.configured else None)
    elif kind == "domain_strength":
        from app.services.backlinks import compute_domain_strength
        result = await asyncio.to_thread(compute_domain_strength, ClickHouse())
    else:
        from app.services.backlinks import verify_links
        result = await verify_links(ClickHouse(), params["domain"], int(params.get("limit", 200)))
    if payload.get("callback_url"):
        with SessionLocal() as db:
            ch = db.scalar(select(NotificationChannel).where(NotificationChannel.org_id == org_id, NotificationChannel.kind == "webhook")
                           .order_by(NotificationChannel.id).limit(1))
            secret = channel_config(ch)["secret"] if ch else None
        body = json.dumps({"job_kind": kind, "result": result}, default=str).encode()
        headers = {"X-SGI-Signature": sign(secret, body)} if secret else {}
        status, err = await safe_post(payload["callback_url"], body, headers, allow_private=get_settings().crawler_allow_private_networks)
        result = {"result": result, "callback": {"status": status, "error": err}}
    return json.loads(json.dumps(result, default=str))

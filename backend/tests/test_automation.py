import json
from datetime import UTC, date, datetime, timedelta

import respx
from sqlalchemy import select, text

from app.models import AlertEvent, GrowthOpportunity, RankAlert, RankProject, SearchTotalsDaily, TicketLink
from app.services import automation as auto


def site(client, name="Ex"):
    return client.post("/api/v1/sites", json={"name": name, "base_url": "https://example.com.tr"}).json()


def test_channel_validation_and_signed_webhook(client, settings, fixture_site):
    assert client.post("/api/v1/channels", json={"kind": "slack", "name": "s", "config": {"url": "https://evil.example/hook"}}).status_code == 422
    assert client.post("/api/v1/channels", json={"kind": "webhook", "name": "w", "config": {"url": "http://169.254.169.254/x"}}).status_code == 422
    settings(crawler_allow_private_networks=True)
    rx = fixture_site({})
    c = client.post("/api/v1/channels", json={"kind": "webhook", "name": "w", "config": {"url": f"{rx.base}/hook"}}).json()
    secret = c["signing_secret"]
    assert "signing_secret" not in client.get("/api/v1/channels").json()["items"][0]  # never returned again
    r = client.post(f"/api/v1/channels/{c['id']}/test").json()
    assert r["ok"] and rx.posts[0]["path"] == "/hook"
    sig = rx.posts[0]["headers"]["x-sgi-signature"]
    assert auto.verify_signature(secret, rx.posts[0]["body"], sig) and not auto.verify_signature("wrong", rx.posts[0]["body"], sig)


async def test_safe_post_blocks_internal_targets():
    from app.services.crawler.fetcher import safe_post
    for url in ["http://127.0.0.1:9/x", "http://169.254.169.254/latest", "http://[::1]/", "http://localhost/"]:
        status, err = await safe_post(url, b"{}", {})
        assert status is None and err.startswith("blocked")


def test_alert_rules_fire_dedupe_cooldown_snooze(client, db, settings, fixture_site):
    settings(crawler_allow_private_networks=True)
    rx = fixture_site({})
    s = site(client)
    end = date.today() - timedelta(days=3)
    for i in range(14):
        d = end - timedelta(days=i)
        db.add(SearchTotalsDaily(site_id=s["id"], date=d, device="MOBILE", clicks=50 if i < 7 else 100, impressions=1000, ctr=0.1, position=5))
    db.commit()
    ch = client.post("/api/v1/channels", json={"kind": "webhook", "name": "w", "config": {"url": f"{rx.base}/a"}}).json()
    rule = client.post("/api/v1/alert-rules", json={"name": "Clicks drop", "rule_type": "metric_change", "site_id": s["id"],
                                                    "config": {"metric": "clicks", "window_days": 7, "threshold_pct": -30},
                                                    "min_volume": 50, "channel_ids": [ch["id"]], "severity": "high"}).json()
    r1 = client.post("/api/v1/alerts/evaluate").json()
    assert r1["fired"] == 1 and json.loads(rx.posts[0]["body"])["payload"]["change_pct"] == -50.0
    assert client.post("/api/v1/alerts/evaluate").json()["fired"] == 0  # dedupe (same subject)
    ev = client.get("/api/v1/alert-events").json()["items"][0]
    assert ev["deliveries"][0]["ok"] and ev["severity"] == "high"
    # rank alerts are forwarded; snooze suppresses
    rp = RankProject(org_id=1, project_id=1, name="r", own_domain="example.com.tr", database="tr-TR:google.com.tr", devices=[], locations=[])
    db.add(rp); db.flush()
    db.add(RankAlert(org_id=1, rank_project_id=rp.id, alert_type="rank_drop", keyword="kahve", severity="high", details={}, dedupe_key="x1"))
    db.commit()
    r2 = client.post("/api/v1/alert-rules", json={"name": "ranks", "rule_type": "rank_alerts", "channel_ids": [ch["id"]]}).json()
    client.post(f"/api/v1/alert-rules/{r2['id']}/snooze", json={"hours": 2})
    assert client.post("/api/v1/alerts/evaluate").json()["fired"] == 0
    client.post(f"/api/v1/alert-rules/{r2['id']}/snooze", json={"hours": 0})
    assert client.post("/api/v1/alerts/evaluate").json()["fired"] == 1


def test_digest_report_xss_export_share(client, db):
    s = client.post("/api/v1/sites", json={"name": "<script>alert(1)</script>", "base_url": "https://x.example"}).json()
    db.add(GrowthOpportunity(site_id=s["id"], key="k", opportunity_type="low_ctr", title="=HYPERLINK(\"http://evil\")", description="d",
                             evidence={}, data_sources=["gsc"], confidence=0.5, effort="low", priority_score=5, recommended_action="a",
                             validation_metric="v", limitations="l"))
    db.commit()
    client.patch("/api/v1/org/branding", json={"name": "Ajans X", "color": "#ff0000"})
    html = client.get(f"/api/v1/sites/{s['id']}/report.html").text
    assert "<script>alert" not in html and "&lt;script&gt;" in html and "Ajans X" in html and "—" in html
    assert client.get("/api/v1/exports/opportunities").status_code == 428
    csv_body = client.get("/api/v1/exports/opportunities", params={"confirm": True}).content.decode("utf-8-sig")
    assert "'=HYPERLINK" in csv_body and "source_classes" in csv_body.splitlines()[0]
    xlsx = client.get("/api/v1/exports/opportunities", params={"confirm": True, "fmt": "xlsx"})
    assert xlsx.content[:2] == b"PK"
    sh = client.post(f"/api/v1/sites/{s['id']}/share", json={"ttl_hours": 1}).json()
    pub = client.get(sh["url_path"])
    assert pub.status_code == 200 and pub.headers["x-robots-tag"] == "noindex" and "&lt;script&gt;" in pub.text
    client.delete(f"/api/v1/share-links/{sh['id']}")
    assert client.get(sh["url_path"]).status_code == 404
    assert client.get("/share/not-a-token").status_code == 404


@respx.mock
def test_tickets_github_jira_linear(client, db, settings):
    s = site(client)
    o = GrowthOpportunity(site_id=s["id"], key="t", opportunity_type="low_ctr", title="Fix title", description="d", evidence={"a": 1},
                          data_sources=["gsc"], confidence=0.5, effort="low", priority_score=5, recommended_action="a",
                          validation_metric="v", limitations="l")
    db.add(o); db.commit()
    assert client.post(f"/api/v1/opportunities/{o.id}/ticket", json={"tracker": "github"}).status_code == 400
    settings(github_token="ghp_x", github_repo="acme/seo")
    respx.post("https://api.github.com/repos/acme/seo/issues").respond(json={"number": 7, "html_url": "https://github.com/acme/seo/issues/7"})
    t = client.post(f"/api/v1/opportunities/{o.id}/ticket", json={"tracker": "github"}).json()
    assert t["external_id"] == "7" and t["opportunity_status"] == "planned"
    respx.get("https://api.github.com/repos/acme/seo/issues/7").respond(json={"state": "closed", "closed_at": "2026-10-01T10:00:00Z"})
    tid = db.scalar(select(TicketLink.id))
    assert client.post(f"/api/v1/tickets/{tid}/sync").json() == {"state": "closed", "opportunity_status": "implemented"}
    settings(jira_base_url="https://acme.atlassian.net", jira_email="a@b.c", jira_api_token="t", jira_project_key="SEO",
             linear_api_key="lin", linear_team_id="team")
    o2 = GrowthOpportunity(site_id=s["id"], key="t2", opportunity_type="low_ctr", title="J", description="d", evidence={}, data_sources=[],
                           confidence=0.5, effort="low", priority_score=5, recommended_action="a", validation_metric="v", limitations="l")
    db.add(o2); db.commit()
    respx.post("https://acme.atlassian.net/rest/api/3/issue").respond(json={"key": "SEO-12"})
    assert client.post(f"/api/v1/opportunities/{o2.id}/ticket", json={"tracker": "jira"}).json()["url"].endswith("/browse/SEO-12")
    respx.post("https://api.linear.app/graphql").respond(json={"data": {"issueCreate": {"issue": {"id": "L1", "identifier": "SEO-1", "url": "https://linear.app/x"}}}})
    assert client.post(f"/api/v1/opportunities/{o2.id}/ticket", json={"tracker": "linear"}).json()["external_id"] == "L1"


def test_api_rate_limit_idempotency_cursor(client, db, settings):
    from fastapi.testclient import TestClient

    from app import auth
    from app.api.v1.account import bootstrap_org
    from app.main import app
    org, _ = bootstrap_org(db, "Paid", "p@example.com", "correct horse battery staple", plan="starter")
    from app.core.security import new_api_key
    from app.models import ApiKey
    full, prefix, h = new_api_key()
    db.add(ApiKey(org_id=org.id, name="k", prefix=prefix, key_hash=h, role="editor"))
    db.commit()
    auth._rate_bucket = None
    settings(app_mode="SERVER", redis_url="")
    with TestClient(app, base_url="https://t") as c:
        hdr = {"Authorization": f"Bearer {full}"}
        codes = [c.get("/api/v1/sites", headers=hdr).status_code for _ in range(14)]
        assert codes.count(200) >= 10 and 429 in codes
    auth._rate_bucket = None
    settings(app_mode="LOCAL_SINGLE_USER")
    body = {"name": "Idem", "base_url": "https://idem.example"}
    r1 = client.post("/api/v1/sites", json=body, headers={"Idempotency-Key": "abc"})
    r2 = client.post("/api/v1/sites", json=body, headers={"Idempotency-Key": "abc"})
    assert r1.status_code == 201 and r2.status_code == 201 and r2.headers["idempotent-replay"] == "true" and r1.json()["id"] == r2.json()["id"]
    assert client.post("/api/v1/sites", json=body).status_code == 409  # without key → real conflict
    sid = r1.json()["id"]
    for i in range(5):
        db.add(GrowthOpportunity(site_id=sid, key=f"c{i}", opportunity_type="low_ctr", title=f"t{i}", description="d", evidence={},
                                 data_sources=[], confidence=0.5, effort="low", priority_score=i, recommended_action="a",
                                 validation_metric="v", limitations="l"))
    db.commit()
    seen, cur = [], ""
    while True:
        page = client.get("/api/v1/opportunities", params={"cursor": cur, "page_size": 2}).json()
        seen += [o["id"] for o in page["items"]]
        if not page["next_cursor"]:
            break
        cur = page["next_cursor"]
    assert len(seen) == 5 and seen == sorted(seen)
    assert client.get("/api/v1/opportunities", params={"cursor": "!!"}).status_code == 422


def test_async_job_with_callback_and_mcp(client, db, settings, fixture_site):
    import asyncio

    from app.worker import run_once
    settings(crawler_allow_private_networks=True)
    rx = fixture_site({})
    client.post("/api/v1/channels", json={"kind": "webhook", "name": "w", "config": {"url": f"{rx.base}/cb", "secret": "s3cret"}})
    s = site(client)
    j = client.post("/api/v1/async-jobs", json={"kind": "opportunities_regenerate", "params": {"site_id": s["id"]},
                                                "callback_url": f"{rx.base}/cb"}).json()
    asyncio.run(run_once("w"))
    st = client.get(f"/api/v1/jobs/{j['job_id']}").json()
    assert st["status"] == "succeeded" and st["result"]["callback"]["status"] == 200
    assert auto.verify_signature("s3cret", rx.posts[-1]["body"], rx.posts[-1]["headers"]["x-sgi-signature"])
    from app.mcp_server import Server
    srv = Server(client=client)
    assert srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})["result"]["serverInfo"]["name"] == "growth-intelligence"
    names = {t["name"] for t in srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]}
    assert {"overview", "opportunities", "keyword_explorer"} <= names
    res = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "opportunities", "arguments": {"site_id": s["id"]}}})
    assert res["result"]["isError"] is False and "items" in json.loads(res["result"]["content"][0]["text"])
    assert srv.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "crawl_summary", "arguments": {}}})["result"]["isError"]
    assert srv.handle({"jsonrpc": "2.0", "id": 5, "method": "delete_everything"})["error"]["code"] == -32601

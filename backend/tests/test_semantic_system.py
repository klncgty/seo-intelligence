import pytest

from app import jobs, semantic


def test_semantic_blocks_invalid_combinations():
    semantic.validate(["gsc_clicks", "gsc_impressions"], ["date"])
    with pytest.raises(semantic.IncompatibleQuery):
        semantic.validate(["gsc_clicks", "gsc_query_clicks"])  # property totals vs page/query grain
    with pytest.raises(semantic.IncompatibleQuery):
        semantic.validate(["gsc_clicks"], ["page"])  # totals have no page dimension
    with pytest.raises(semantic.IncompatibleQuery):
        semantic.validate(["gsc_position"], aggregation_over=["date"])  # never sum average position
    with pytest.raises(semantic.IncompatibleQuery):
        semantic.validate(["gsc_clicks", "estimated_traffic"])  # FIRST_PARTY + MODELED
    with pytest.raises(semantic.IncompatibleQuery):
        semantic.validate(["gsc_position", "tracked_rank"])
    with pytest.raises(semantic.IncompatibleQuery):
        semantic.validate(["nope"])


def test_metric_doc_matches_registry():
    md = semantic.render_markdown()
    assert all(f"`{name}`" in md for name in semantic.REGISTRY)
    assert all(m.source_class in semantic.SOURCE_CLASSES for m in semantic.REGISTRY.values())


def test_catalog_ready_and_jobs_api(client, db):
    cat = client.get("/api/v1/metrics/catalog").json()["metrics"]
    assert {m["name"] for m in cat} == set(semantic.REGISTRY)
    r = client.get("/ready")
    body = r.json()
    assert body["checks"]["postgres"]["ok"] is True and body["checks"]["migrations"]["ok"] is True
    j = jobs.enqueue(db, "noop", {"x": 1})
    assert client.get(f"/api/v1/jobs/{j.id}").json()["status"] == "queued"
    assert client.post(f"/api/v1/jobs/{j.id}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/v1/jobs/{j.id}/retry").json()["status"] == "queued"
    assert client.get("/api/v1/jobs", params={"status": "queued"}).json()["total"] == 1
    assert client.get("/api/v1/jobs/999").status_code == 404


def test_sse_stream_reports_terminal_state(client, db):
    j = jobs.enqueue(db, "noop", {})
    jobs.cancel(db, j.id)
    with client.stream("GET", f"/api/v1/jobs/{j.id}/events") as r:
        body = "".join(r.iter_text())
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "event: state" in body and '"cancelled"' in body

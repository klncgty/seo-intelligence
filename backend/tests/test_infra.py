"""Integration tests against real local services (scripts/dev-services.sh). Skipped if unreachable."""

import asyncio
import time
import uuid

import pytest

from app.infra.clickhouse import ClickHouse, ClickHouseUnavailable
from app.infra.ratelimit import TokenBucket
from app.infra.storage import ObjectStore, StorageUnavailable, content_key


@pytest.fixture()
def ch():
    c = ClickHouse(url="http://127.0.0.1:8123", database="seo_test")
    if not c.ping():
        pytest.skip("ClickHouse not running")
    c.command("DROP DATABASE IF EXISTS seo_test", database="default")
    c.migrate()
    return c


def test_clickhouse_migrations_idempotent_and_dedupe(ch):
    from app.infra.clickhouse import MIGRATIONS_DIR
    assert ch.migrate() == [] and ch.head() == sorted(MIGRATIONS_DIR.glob("*.sql"))[-1].stem
    row = {"keyword": "Kahve Makinesi", "keyword_key": "kahve makinesi", "locale": "tr-TR:google.com.tr",
           "month": "2026-09-01", "search_volume": 1000, "volume_precision": "exact", "cpc": 1.2, "cpc_currency": "TRY",
           "intents": ["commercial"], "serp_features": [], "word_count": 2, "is_question": 0, "cluster_id": "",
           "source_class": "PROVIDER", "provider": "mock", "provider_request_id": "r1", "raw_ref": "", "cost_units": 0,
           "fetched_at": "2026-10-01 00:00:00", "kd_model_version": "", "version": 1}
    ch.insert("keyword_metrics", [row])
    ch.insert("keyword_metrics", [{**row, "search_volume": 1200, "version": 2}])  # re-ingest same key
    rows = ch.query("SELECT search_volume FROM keyword_metrics FINAL WHERE keyword_key = {k:String}", {"k": "kahve makinesi"})
    assert rows == [{"search_volume": "1200"}] or rows == [{"search_volume": 1200}]


def test_clickhouse_params_prevent_injection_and_tenant_guard(ch):
    evil = "x' OR 1=1 --"
    assert ch.query("SELECT count() AS c FROM keyword_metrics WHERE keyword_key = {k:String}", {"k": evil})[0]["c"] in (0, "0")
    with pytest.raises(ValueError):
        ch.tenant_query("SELECT * FROM rank_tracking_results", tenant_id=1)
    assert ch.tenant_query("SELECT count() AS c FROM rank_tracking_results WHERE tenant_id = {tenant_id:UInt64}", 1)


def test_clickhouse_unconfigured():
    with pytest.raises(ClickHouseUnavailable):
        ClickHouse(url="").query("SELECT 1")


def test_storage_keys_are_content_addressed_and_traversal_safe():
    k = content_key("raw-provider", b"abc", "json")
    assert k.startswith("raw-provider/ba/") and k.endswith(".json") and ".." not in k
    with pytest.raises(ValueError):
        content_key("../../etc", b"x")
    with pytest.raises(ValueError):
        content_key("html", b"x", "sh")
    store = ObjectStore(client=None)
    with pytest.raises(StorageUnavailable):
        store.put("html", b"x", "html")


def test_storage_roundtrip_minio():
    from minio import Minio
    client = Minio("127.0.0.1:9000", access_key="seo-minio", secret_key="seo-minio-dev-secret", secure=False)
    store = ObjectStore(client=client, bucket="seo-test")
    if not store.ping():
        pytest.skip("MinIO not running")
    data = uuid.uuid4().bytes * 10
    key = store.put("raw-provider", data, "json")
    assert store.put("raw-provider", data, "json") == key  # idempotent
    assert store.get(key) == data
    for bad in ["../secret", "raw-provider/../../x", "raw-provider/ab/" + "g" * 64]:
        with pytest.raises(ValueError):
            store.get(bad)


def test_token_bucket_is_shared_across_workers():
    import redis
    r = redis.Redis.from_url("redis://127.0.0.1:6379/15")
    try:
        r.ping()
    except Exception:
        pytest.skip("Redis not running")
    key = f"host:test-{uuid.uuid4().hex}"
    w1, w2 = TokenBucket(r), TokenBucket(r)  # two "workers" / tenants
    assert w1.try_acquire(key, rate=2, burst=1) == 0
    assert w2.try_acquire(key, rate=2, burst=1) > 0  # second worker must wait: shared bucket
    start = time.monotonic()

    async def go():
        await asyncio.gather(*(TokenBucket(r).acquire(key, rate=10, burst=1) for _ in range(5)))
    asyncio.run(go())
    assert time.monotonic() - start >= 0.35  # 5 requests at 10/s with burst 1 cannot finish instantly


def test_token_bucket_local_fallback():
    b = TokenBucket(redis_client=None)
    if b.shared:
        pytest.skip("REDIS_URL configured")
    assert b.try_acquire("k", rate=1, burst=1) == 0 and b.try_acquire("k", rate=1, burst=1) > 0


def test_clickhouse_array_params_escaped(ch):
    tricky = ["kahve", "o'reilly", "back\\slash", "x') OR 1=1 --"]
    rows = ch.query("SELECT arrayJoin({a:Array(String)}) AS v", {"a": tricky})
    assert [r["v"] for r in rows] == tricky


def test_warc_record_format_and_minio_archive(settings, fixture_site, client, db):
    from app.infra.warc import parse_record, response_record
    rec = parse_record(response_record("https://e.com/a", 200, {"Content-Type": "text/html", "Set-Cookie": "s=1"}, b"<html>x</html>"))
    assert rec["version"] == "WARC/1.1" and rec["fields"]["WARC-Type"] == "response"
    assert rec["fields"]["WARC-Target-URI"] == "https://e.com/a" and rec["payload"].endswith(b"<html>x</html>")
    assert b"Set-Cookie" not in rec["payload"]  # never archive cookies
    from minio import Minio
    if not ObjectStore(client=Minio("127.0.0.1:9000", access_key="seo-minio", secret_key="seo-minio-dev-secret", secure=False),
                       bucket="seo-test").ping():
        pytest.skip("MinIO not running")
    settings(crawler_allow_private_networks=True, crawler_store_warc=True, minio_endpoint="127.0.0.1:9000",
             minio_access_key="seo-minio", minio_secret_key="seo-minio-dev-secret", minio_bucket="seo-test")
    from app.models import CrawlPage, Site
    from app.worker import run_once
    from tests.conftest import html
    fx = fixture_site({"/": html("Home", "<p>hello</p>")})
    s = Site(org_id=1, project_id=1, name="w", base_url=fx.base + "/", allowed_hosts=["127.0.0.1"], conversion_events={}, crawl_settings={})
    db.add(s); db.commit()
    client.post(f"/api/v1/sites/{s.id}/crawls")
    asyncio.run(run_once("w"))
    page = db.scalar(__import__("sqlalchemy").select(CrawlPage).where(CrawlPage.normalized_url == fx.base + "/"))
    assert page.warc_ref and page.warc_ref.startswith("warc/")
    stored = parse_record(ObjectStore().get(page.warc_ref))
    assert b"hello" in stored["payload"]

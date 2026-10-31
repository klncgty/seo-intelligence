"""Keyword explorer load test on N synthetic rows (separate DB `seo_perf`, is_synthetic=1).

Usage: CLICKHOUSE_URL=http://127.0.0.1:8123 uv run python scripts/perf_keywords.py [rows]
"""
import statistics
import sys
import time

from app.infra.clickhouse import ClickHouse
from app.services.keywords import store

N = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000
ch = ClickHouse(database="seo_perf")
ch.command("DROP DATABASE IF EXISTS seo_perf", database="default")
ch.migrate()
words = ["kahve", "makinesi", "fiyat", "en", "iyi", "espresso", "çay", "demlik", "öğütücü", "kapsül", "filtre", "türk",
         "nasıl", "yapılır", "yorum", "ucuz", "online", "satın", "al", "otomatik", "mini", "paslanmaz", "çelik", "seti"]
arr = "[" + ",".join(f"'{w}'" for w in words) + "]"
t0 = time.time()
ch.command(f"""
INSERT INTO keyword_metrics (keyword, keyword_key, locale, month, search_volume, volume_precision, cpc, cpc_currency,
  ads_competition, kd, kd_model_version, intents, serp_features, word_count, is_question, cluster_id, source_class, provider,
  provider_request_id, raw_ref, cost_units, fetched_at, is_synthetic, version)
SELECT kw, kw, 'tr-TR:google.com.tr', toDate('2026-09-01'), toUInt64(rand() % 50000), 'exact', (rand() % 500) / 100.0, 'TRY',
  (rand() % 100) / 100.0, toFloat32(rand() % 100), 'kd-1',
  [arrayElement(['informational','commercial','transactional','navigational'], (number % 4) + 1)], [], 3 + number % 3,
  toUInt8(number % 7 = 0), '', 'DEMO', 'synthetic', '', '', 0, now(), 1, 1
FROM (SELECT number, concat(arrayElement({arr}, (number % 24) + 1), ' ', arrayElement({arr}, (intDiv(number, 24) % 24) + 1), ' ',
             arrayElement({arr}, (intDiv(number, 576) % 24) + 1), ' ', toString(number)) AS kw FROM numbers({N}))
""")
from app.services.keywords.store import LATEST_COLS
cols = ", ".join(LATEST_COLS)
ch.command(f"INSERT INTO keyword_latest ({cols}) SELECT {cols} FROM keyword_metrics")  # same dual-write as the app
print(f"inserted {N:,} rows (+latest) in {time.time() - t0:.1f}s")
ch.command("OPTIMIZE TABLE keyword_latest FINAL")
cases = [dict(q="kahve makinesi"), dict(q="espresso", min_volume=1000, max_kd=40), dict(q="çay demlik", match="phrase"),
         dict(intent="transactional", min_volume=40000), dict(q="kapsül", question=True), dict(include=["fiyat"], exclude=["ucuz"], min_cpc=3)]
lat = []
for _ in range(5):
    for c in cases:
        t = time.perf_counter()
        store.explorer(ch, "tr-TR:google.com.tr", limit=50, **c)
        lat.append(time.perf_counter() - t)
lat.sort()
p95 = lat[int(len(lat) * 0.95) - 1]
print(f"queries={len(lat)} p50={statistics.median(lat)*1000:.0f}ms p95={p95*1000:.0f}ms max={lat[-1]*1000:.0f}ms")

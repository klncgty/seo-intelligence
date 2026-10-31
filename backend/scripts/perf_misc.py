"""Rank-tracking ingest/compute (100k keywords/day) and backlink referring-domain queries on synthetic data (seo_perf DB)."""
import statistics
import sys
import time

from app.infra.clickhouse import ClickHouse
from app.services import backlinks as bl

BL_ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 10_000_000
ch = ClickHouse(database="seo_perf")
ch.command("DROP DATABASE IF EXISTS seo_perf", database="default")
ch.migrate()
t = time.time()
ch.command("""INSERT INTO rank_tracking_results (tenant_id, project_id, tracked_keyword_id, keyword_key, locale, device, location, check_date,
  domain, position, landing_url, serp_features_owned, source_class, provider, fetched_at, is_synthetic, version)
SELECT 1, 1, number % 100000, concat('kw', toString(number % 100000)), 'tr-TR:google.com.tr', 'desktop', '', today(),
  arrayElement(['me.com.tr','r1.com','r2.com'], intDiv(number, 100000) + 1), if(rand() % 5 = 0, NULL, 1 + rand() % 100), '', [],
  'DEMO', 'synthetic', now(), 1, 1 FROM numbers(300000)""")
ingest = time.time() - t
t = time.time()
vis = ch.query("SELECT domain, sum(multiIf(position = 1, 0.28, position = 2, 0.15, position = 3, 0.10, position <= 10, 0.03, position <= 20, 0.01, 0)) "
               "/ (count() * 0.28) * 100 AS visibility, countIf(position <= 10) AS top10 FROM rank_tracking_results FINAL "
               "WHERE tenant_id = 1 AND project_id = 1 AND check_date = today() GROUP BY domain")
compute = time.time() - t
print(f"rank: 100k keywords x 3 domains ingest {ingest:.2f}s, visibility+distribution compute {compute:.2f}s -> {len(vis)} domains")
t = time.time()
ch.command(f"""INSERT INTO backlinks (source_url, source_domain, target_url, target_domain, anchor, rel, link_position, is_image, first_seen,
  last_seen, last_checked, status, source, source_class, fetched_at, is_synthetic, version)
SELECT concat('https://s', toString(number % 2000000), '.com/p', toString(number)), concat('s', toString(number % 2000000), '.com'),
  concat('https://t', toString(number % 50000), '.com.tr/'), concat('t', toString(number % 50000), '.com.tr'), 'anchor', '', 'content', 0,
  today() - number % 700, today(), today(), 'live', 'common_crawl', 'OBSERVED', now(), 1, 1 FROM numbers({BL_ROWS})""")
print(f"backlinks: inserted {BL_ROWS:,} rows in {time.time() - t:.1f}s")
ch.command("OPTIMIZE TABLE backlinks FINAL")
lat = []
for i in range(20):
    s = time.perf_counter()
    bl.referring_domains(ch, f"t{i * 97 % 50000}.com.tr", 200)
    lat.append(time.perf_counter() - s)
lat.sort()
print(f"referring_domains: p50={statistics.median(lat)*1000:.0f}ms p95={lat[int(len(lat)*0.95)-1]*1000:.0f}ms")
ch.command("DROP DATABASE IF EXISTS seo_perf", database="default")

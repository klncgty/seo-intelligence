# Performance (measured locally — MacBook, brew ClickHouse 26.10, single node)

| Target (spec §21) | Design | Measured locally | Status |
|---|---|---|---|
| Keyword explorer p95 < 1 s @ 50M | `keyword_latest` (ReplacingMergeTree, ORDER BY (locale, keyword_key), token/ngram bloom + minmax indexes), no GROUP BY at read time | **10M synthetic rows: p50 129 ms, p95 230 ms, max 241 ms** (30 queries, 6 filter shapes) | met at 10M; 50M not measured |
| — before optimization | GROUP BY keyword_key + argMax over monthly history | 10M: p50 318 ms, **p95 4469 ms** | replaced |
| Rank tracking 100k kw/day < 1 h (excl. provider latency) | CH rank_tracking_results, ReplacingMergeTree | **100k kw × 3 domains: ingest 0.09 s, visibility/distribution 0.01 s** | met (provider SERP latency not included) |
| Backlinks RD p95 < 2 s @ 50M | backlinks ORDER BY (target_domain, …) + bloom index | **10M synthetic rows: p50 7 ms, p95 15 ms** (20 queries) | met at 10M; 50M not measured |
| Cached API reads p95 < 300 ms | — | not measured | open |
| Audit crawler 50k pages < 1 GB | — | not measured | open |

Reproduce: `CLICKHOUSE_URL=http://127.0.0.1:8123 uv run python backend/scripts/perf_keywords.py 10000000`
(synthetic rows, is_synthetic=1, separate database `seo_perf`, dropped afterwards). Insert of 10M rows: ~7 s.
Note: queries read `keyword_latest FINAL` after OPTIMIZE; under heavy concurrent ingest, FINAL cost rises until merges run.

Reproduce rank/backlink numbers: `CLICKHOUSE_URL=http://127.0.0.1:8123 uv run python backend/scripts/perf_misc.py 10000000`.

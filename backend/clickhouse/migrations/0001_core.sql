-- Global (shared) and tenant-scoped analytics tables. Idempotent writes via ReplacingMergeTree(version).
-- Read latest state with FINAL or argMax(..., version); see docs/adr/0001-clickhouse.md.

CREATE TABLE IF NOT EXISTS keyword_metrics (
    keyword String,
    keyword_key String,               -- Turkish/diacritic-insensitive match key
    locale LowCardinality(String),    -- e.g. tr-TR:google.com.tr
    month Date,                       -- first day of month for the volume point
    search_volume Nullable(UInt64),
    volume_precision LowCardinality(String),  -- exact | range | rounded (as provided)
    cpc Nullable(Float64),
    cpc_currency LowCardinality(String),
    ads_competition Nullable(Float32),
    kd Nullable(Float32),
    kd_model_version LowCardinality(String),
    intents Array(LowCardinality(String)),
    serp_features Array(LowCardinality(String)),
    word_count UInt8,
    is_question UInt8,
    cluster_id String,
    source_class LowCardinality(String),
    provider LowCardinality(String),
    provider_request_id String,
    raw_ref String,
    cost_units Float32,
    fetched_at DateTime,
    is_synthetic UInt8 DEFAULT 0,
    version UInt64,
    INDEX kw_ngram keyword_key TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 4,
    INDEX kw_token keyword_key TYPE tokenbf_v1(65536, 3, 0) GRANULARITY 4
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(month)
ORDER BY (locale, keyword_key, month);

CREATE TABLE IF NOT EXISTS serp_results (
    keyword_key String,
    keyword String,
    locale LowCardinality(String),
    device LowCardinality(String),
    location String,
    snapshot_date Date,
    rank_group UInt16,
    rank_absolute UInt16,
    result_type LowCardinality(String),   -- organic | featured_snippet | paa | local_pack | video | image | ad | ai_overview ...
    url String,
    domain String,
    registrable_domain String,
    title String,
    serp_features Array(LowCardinality(String)),
    ai_overview_cited UInt8,
    source_class LowCardinality(String),
    provider LowCardinality(String),
    provider_request_id String,
    raw_ref String,
    fetched_at DateTime,
    is_synthetic UInt8 DEFAULT 0,
    version UInt64
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(snapshot_date)
ORDER BY (locale, device, location, keyword_key, snapshot_date, rank_absolute)
TTL snapshot_date + INTERVAL 25 MONTH;

CREATE TABLE IF NOT EXISTS domain_keyword_rankings (
    registrable_domain String,
    locale LowCardinality(String),
    device LowCardinality(String),
    keyword_key String,
    snapshot_date Date,
    best_rank UInt16,
    url String,
    version UInt64
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(snapshot_date)
ORDER BY (registrable_domain, locale, device, keyword_key, snapshot_date);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_domain_keyword_rankings TO domain_keyword_rankings AS
SELECT registrable_domain, locale, device, keyword_key, snapshot_date,
       min(rank_group) AS best_rank, argMin(url, rank_group) AS url, max(version) AS version
FROM serp_results WHERE result_type = 'organic'
GROUP BY registrable_domain, locale, device, keyword_key, snapshot_date;

CREATE TABLE IF NOT EXISTS rank_tracking_results (
    tenant_id UInt64,
    project_id UInt64,
    tracked_keyword_id UInt64,
    keyword_key String,
    locale LowCardinality(String),
    device LowCardinality(String),
    location String,
    check_date Date,
    domain String,
    position Nullable(UInt16),        -- NULL = not in top-N
    landing_url String,
    serp_features_owned Array(LowCardinality(String)),
    source_class LowCardinality(String),
    provider LowCardinality(String),
    fetched_at DateTime,
    is_synthetic UInt8 DEFAULT 0,
    version UInt64
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(check_date)
ORDER BY (tenant_id, project_id, tracked_keyword_id, domain, check_date);

CREATE TABLE IF NOT EXISTS backlinks (
    source_url String,
    source_domain String,
    target_url String,
    target_domain String,
    anchor String,
    rel LowCardinality(String),
    link_position LowCardinality(String),  -- content | nav | footer | unknown
    is_image UInt8,
    first_seen Date,
    last_seen Date,
    last_checked Date,
    status LowCardinality(String),         -- live | lost | redirect
    source LowCardinality(String),         -- own_crawl | common_crawl | provider:<name>
    source_class LowCardinality(String),
    fetched_at DateTime,
    is_synthetic UInt8 DEFAULT 0,
    version UInt64,
    INDEX td target_domain TYPE bloom_filter GRANULARITY 4
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(first_seen)
ORDER BY (target_domain, source_domain, target_url, source_url);

CREATE TABLE IF NOT EXISTS ai_visibility_results (
    tenant_id UInt64,
    project_id UInt64,
    prompt_id UInt64,
    engine LowCardinality(String),
    model_version String,
    locale LowCardinality(String),
    sample_index UInt8,
    run_date Date,
    brand_mentioned UInt8,
    brand_cited UInt8,
    mention_position Nullable(UInt16),
    cited_urls Array(String),
    competitor_mentions Array(String),
    source_class LowCardinality(String),
    cost_units Float32,
    fetched_at DateTime,
    version UInt64
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(run_date)
ORDER BY (tenant_id, project_id, prompt_id, engine, run_date, sample_index)

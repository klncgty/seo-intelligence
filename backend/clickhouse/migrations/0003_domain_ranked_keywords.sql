-- Provider "ranked keywords" per domain (PROVIDER). Kept separate from our own SERP snapshot universe (OBSERVED via
-- serp_results / domain_keyword_rankings); the two are shown side by side and never blended.
CREATE TABLE IF NOT EXISTS domain_ranked_keywords (
    registrable_domain String,
    locale LowCardinality(String),
    keyword String,
    keyword_key String,
    position UInt16,
    url String,
    search_volume Nullable(UInt64),
    snapshot_date Date,
    source_class LowCardinality(String),
    provider LowCardinality(String),
    provider_request_id String,
    fetched_at DateTime,
    is_synthetic UInt8 DEFAULT 0,
    version UInt64
) ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(snapshot_date)
ORDER BY (registrable_domain, locale, keyword_key, snapshot_date)
TTL snapshot_date + INTERVAL 25 MONTH

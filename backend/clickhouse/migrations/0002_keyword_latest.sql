-- Latest state per (locale, keyword): serves the explorer without GROUP BY over the monthly history.
-- Dual-written by app/services/keywords/store.py; ReplacingMergeTree(version) keeps the newest row.
CREATE TABLE IF NOT EXISTS keyword_latest (
    keyword String,
    keyword_key String,
    locale LowCardinality(String),
    month Date,
    search_volume Nullable(UInt64),
    volume_precision LowCardinality(String),
    cpc Nullable(Float64),
    cpc_currency LowCardinality(String),
    ads_competition Nullable(Float32),
    kd Nullable(Float32),
    kd_model_version LowCardinality(String),
    intents Array(LowCardinality(String)),
    serp_features Array(LowCardinality(String)),
    word_count UInt8,
    is_question UInt8,
    source_class LowCardinality(String),
    provider LowCardinality(String),
    fetched_at DateTime,
    is_synthetic UInt8 DEFAULT 0,
    version UInt64,
    INDEX kl_token keyword_key TYPE tokenbf_v1(65536, 3, 0) GRANULARITY 2,
    INDEX kl_ngram keyword_key TYPE ngrambf_v1(3, 65536, 3, 0) GRANULARITY 2,
    INDEX kl_vol search_volume TYPE minmax GRANULARITY 2
) ENGINE = ReplacingMergeTree(version)
ORDER BY (locale, keyword_key)

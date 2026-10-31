-- Own domain-strength metric (model card docs/models/domain-strength.md). Recomputed from the backlink index.
CREATE TABLE IF NOT EXISTS domain_strength (
    registrable_domain String,
    score Float32,
    raw_pagerank Float64,
    referring_domains UInt32,
    spam_penalty Float32,
    model_version LowCardinality(String),
    computed_at DateTime,
    version UInt64
) ENGINE = ReplacingMergeTree(version)
ORDER BY registrable_domain

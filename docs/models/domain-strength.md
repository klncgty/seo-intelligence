# Model card — Domain Strength (`ds-1`)
Own 0–100 metric on our backlink index (`backlinks`, sources own_crawl / common_crawl / provider:*).
- Graph: one edge per (source registrable domain → target registrable domain) among live links; weight 1 for followed links,
  0.1 if every link of the pair is nofollow/ugc/sponsored.
- PageRank, damping 0.85, 30 iterations, dangling mass redistributed uniformly.
- Spam penalty (≤ 0.5): for domains with ≤ 20 referring domains, share of referrers with ≥ 50 distinct outbound target domains
  (link-farm pattern) × 0.5.
- Score = 100 · ln(1 + 999·PR/PR_max) / ln(1000) · (1 − penalty).
Limitations: coverage-bound — a domain absent from our index scores low regardless of reality; scores are relative to the
current index and shift when it grows. Not validated against an external ground truth yet. Feeds KD-1 `authority`.

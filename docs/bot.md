# Crawler identity

User-Agent: configured via `CRAWLER_USER_AGENT` (default `SEOGrowthIntelligenceBot/0.1 (+private; local crawler)`). For public
deployments set it to `SEOGrowthBot/1.0 (+https://<your-domain>/bot)` and publish this page there.

- Purpose: technical SEO audits of sites registered and (for deep audits) verified by their owners; bounded, shallow crawls of
  competitor public pages; re-verification of known backlinks.
- Politeness: robots.txt (incl. Disallow, unreachable 5xx robots = disallow all), per-host delay shared across all customers,
  low concurrency (competitors ≤ 2, ≥ 1 s delay), no login/account/cart/checkout/admin areas, no cookies, no JavaScript execution.
- Opt-out: `User-agent: SEOGrowthIntelligenceBot` + `Disallow: /` in robots.txt is honored on the next fetch.
- Never scraped: Google search results / autocomplete, consumer AI chat interfaces.
- IP ranges: publish your egress IPs here when deployed.

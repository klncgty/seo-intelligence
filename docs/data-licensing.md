# Data licensing & provider terms

| Source | Use | Display | Redistribution | Retention | Attribution | Status |
|---|---|---|---|---|---|---|
| Google Search Console API | tenant's own property data | to that tenant only | no | per tenant retention | n/a | per Google API ToS |
| Google Analytics Data API | tenant's own property data | to that tenant only | no | per tenant retention | n/a | per Google API ToS |
| DataForSEO (SERP, Keywords Data, Labs, Backlinks) | global datasets (SERP snapshots, volumes, ranked keywords) | to platform users | **to verify**: whether reselling/displaying raw data to third-party tenants is permitted under the plan | cache TTL default 7 days, raw responses in MinIO | **to verify** | adapter implemented, not verified with live account |
| Public Suffix List | registrable domain | n/a | MPL-2.0 (file kept unmodified) | repo snapshot | MPL notice in file | ok |
| Common Crawl (planned Phase 8) | backlink bootstrap | aggregated | per CC Terms of Use | **to verify** | CC attribution | not started |
| CrUX / PSI (planned Phase 7) | CWV field/lab | yes with attribution | per Google API ToS | **to verify** | "Chrome UX Report" | not started |

Rules: Google SERP/autocomplete are never scraped; SERP data only from licensed providers. Mock providers only for tests and DEMO organizations (is_synthetic, DEMO badge). Different providers' data is never blended into one value; every record keeps its provider in lineage.

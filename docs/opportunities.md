# Growth opportunities (v2)

Every opportunity carries: site/URL, type, data_sources (→ source classes), date_range, raw evidence, impact estimate
(P10/P50/P90 or explicit unknown), confidence, effort (low/medium/high ≈ 1–4 / 4–16 / 16–60 h), priority, action, validation
metric, limitations. Regeneration is idempotent on a stable `key`; operator status/notes are preserved.

## Lifecycle
- Evidence missing on regeneration → `resolution_suggested_at` set (operator decides).
- `implemented` and evidence reappears ≥ 28 days later → reopened (`status=new`, `reopened_count+1`).
- Confidence cap = weight of the weakest source class: FIRST_PARTY 1.0 > PROVIDER 0.85 > MODELED 0.7 > OBSERVED 0.6 > AI 0.4.

## Types
| Type | Trigger (defaults) | Impact | Source |
|---|---|---|---|
| low_ctr | (page,query) ≥200 impr, pos ≤20, CTR < 0.5 × site's own median CTR in position bucket (≥5 comparable rows) | (expected−actual CTR)×impr; P10/P50/P90 by bootstrap over days | GSC |
| declining_performance | page clicks −30% vs previous equal period, prev ≥50 clicks | lost clicks | GSC |
| indexing_issue_on_valuable_page | blocking audit issue on page with ≥10 clicks / ≥20 sessions / conversions | unknown | crawl + GSC/GA4 |
| low_conversion | ≥100 sessions and conv. rate < 0.5 × site median | sessions × (median − rate) | GA4 |
| weak_internal_linking | valuable page with ≤1 inlink or depth ≥4 | unknown | crawl + GSC/GA4 |
| content_intent | cannibalization (≥2 pages ≥20% impr) or striking distance (pos 8–20) | unknown / modeled | GSC |
| competitor_content_gap / page_type_gap / technical_gap | competitor crawl observation (+ own GSC demand for content gaps) | unknown | crawl (+GSC) |
| keyword_gap | competitors rank, you don't; commercial/transactional intent; KD ≤ 60; provider volume | volume × CTR(pos 5) × reach 0.3 (uncalibrated) → P10/P50/P90 = ×0.3/×1/×2.5 | SERP + provider + model |
| broken_backlink_reclamation | live backlinks to a URL returning 404/410 in latest crawl | referring domains recovered | backlink index + crawl |

## Priority
v1: `impact_norm × confidence × effort_factor × 100` (see backend/app/services/growth/engine.py `PRIORITY_FORMULA`);
keyword gap: `min(100, √P50) × confidence × effort_factor`; reclamation: `min(100, 20 + 5·RD) × 0.7`.

## Measurement loop
Implemented page-level opportunities: difference-in-differences, treated = page clicks, control = site total − page
(scaled to the page's pre level), 28 days pre/post (post must be ≥3 days old), 95% bootstrap CI → positive / negative /
inconclusive. `GET /opportunities-calibration` compares predicted P50 with measured effects.

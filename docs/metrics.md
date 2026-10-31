# Metric catalog

_Generated from `backend/app/semantic.py` — do not edit by hand._

| Metric | Source | Unit | Aggregation | Dimensions | Dataset | TZ | Formula | Notes |
|---|---|---|---|---|---|---|---|---|
| `gsc_clicks` Google clicks (site total) | FIRST_PARTY | clicks | sum | date, device | gsc_totals | America/Los_Angeles | SUM(clicks) over search_totals_daily | Property-level totals incl. anonymized queries. Last ~3 days are preliminary. |
| `gsc_impressions` Google impressions (site total) | FIRST_PARTY | impressions | sum | date, device | gsc_totals | America/Los_Angeles | SUM(impressions) over search_totals_daily |  |
| `gsc_ctr` Google CTR (site total) | FIRST_PARTY | ratio | ratio | date, device | gsc_totals | America/Los_Angeles | SUM(clicks)/SUM(impressions) | Never average daily CTRs; recompute from sums. |
| `gsc_position` Average position (GSC) | FIRST_PARTY | position | weighted_avg | date, device | gsc_totals | America/Los_Angeles | SUM(position*impressions)/SUM(impressions) | Impression-weighted. Never summed. Not comparable to tracked rank. |
| `gsc_query_clicks` Google clicks (query/page breakdown) | FIRST_PARTY | clicks | sum | date, page, query, device, country | gsc_detail | America/Los_Angeles | SUM(clicks) over search_performance_daily | Excludes anonymized queries → sums below site total. Do not mix with gsc_clicks. |
| `gsc_query_impressions` Google impressions (query/page breakdown) | FIRST_PARTY | impressions | sum | date, page, query, device, country | gsc_detail | America/Los_Angeles | SUM(impressions) over search_performance_daily |  |
| `gsc_query_position` Average position (query/page breakdown) | FIRST_PARTY | position | weighted_avg | date, page, query, device, country | gsc_detail | America/Los_Angeles | SUM(position*impressions)/SUM(impressions) |  |
| `ga4_sessions` Organic sessions (GA4) | FIRST_PARTY | sessions | sum | date, landing_page, channel_group, source_medium | ga4 | property | SUM(sessions) where channel_group='Organic Search' | GA4 property timezone. Not equal to Google clicks. |
| `ga4_users` Users (GA4, summed daily) | FIRST_PARTY | users | sum | date, landing_page, channel_group, source_medium | ga4 | property | SUM(total_users) | Summed across days/pages → not de-duplicated users. |
| `ga4_signups` Signups (configured events) | FIRST_PARTY | events | sum | date, landing_page, channel_group, source_medium | ga4 | property | SUM(signups) | NULL when no signup events configured. |
| `ga4_purchases` Purchases (configured events) | FIRST_PARTY | events | sum | date, landing_page, channel_group, source_medium | ga4 | property | SUM(purchases) |  |
| `ga4_revenue` Purchase revenue | FIRST_PARTY | currency | sum | date, landing_page, channel_group, source_medium | ga4 | property | SUM(revenue) |  |
| `tracked_rank` Tracked rank (SERP snapshot) | PROVIDER | position | latest | date, keyword, device, location, domain | rank_tracking | UTC | rank_group of best organic result | From licensed SERP provider. NULL = not in top-N. Not comparable to GSC average position. |
| `visibility_index` Visibility index | MODELED | percent | ratio | date, device, domain, tag | rank_tracking | UTC | SUM(CTR(rank_i))/(N*CTR(1))*100 over tracked keywords | CTR curve versioned (model_version). |
| `estimated_traffic` Estimated organic traffic | MODELED | visits | sum | date, domain, keyword, page | traffic_model | UTC | SUM(volume*CTR(position, device, features)) | Model estimate with error band. Never added to GSC clicks. |
| `search_volume` Search volume | PROVIDER | searches/month | latest | keyword, locale, month | keyword_metrics | UTC | provider monthly volume | As provided; precision field indicates rounding. |
| `referring_domains` Referring domains (in our index) | OBSERVED | domains | count_distinct | target_domain, date, source | backlinks | UTC | COUNT(DISTINCT source_domain) live links | Coverage-limited; real number may be higher. |

Rules: metrics from different datasets or source classes are never combined; weighted averages and ratios are recomputed from base sums, never summed; GSC property totals (`gsc_totals`) and page/query breakdowns (`gsc_detail`) are separate grains.

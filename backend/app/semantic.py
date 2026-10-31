"""Semantic metric layer: the single definition point for metrics (spec §1.4).

Every consumer (API, CLI, reports, alerts, Claude as operator) must resolve metrics here.
``validate`` rejects invalid combinations, e.g. summing average position, mixing GSC
property-level totals with page/query breakdowns, or combining different source classes.
Run ``uv run seo metrics-doc`` to regenerate docs/metrics.md from this registry.
"""

from dataclasses import asdict, dataclass, field

SOURCE_CLASSES = ("FIRST_PARTY", "OBSERVED", "PROVIDER", "MODELED", "AI")


class IncompatibleQuery(ValueError):
    pass


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    source_class: str
    unit: str
    formula: str
    aggregation: str  # sum | ratio | weighted_avg | count_distinct | latest | none
    dimensions: tuple[str, ...]
    dataset: str  # logical dataset; metrics from different datasets never combine in one query
    timezone: str = "UTC"
    notes: str = ""
    allowed_aggregations: tuple[str, ...] = field(default=())

    def __post_init__(self):
        if self.source_class not in SOURCE_CLASSES:
            raise ValueError(f"{self.name}: unknown source class {self.source_class}")


GSC_TOTAL_DIMS = ("date", "device")
GSC_DETAIL_DIMS = ("date", "page", "query", "device", "country")
GA4_DIMS = ("date", "landing_page", "channel_group", "source_medium")

REGISTRY: dict[str, Metric] = {m.name: m for m in [
    Metric("gsc_clicks", "Google clicks (site total)", "FIRST_PARTY", "clicks", "SUM(clicks) over search_totals_daily",
           "sum", GSC_TOTAL_DIMS, "gsc_totals", "America/Los_Angeles",
           "Property-level totals incl. anonymized queries. Last ~3 days are preliminary."),
    Metric("gsc_impressions", "Google impressions (site total)", "FIRST_PARTY", "impressions",
           "SUM(impressions) over search_totals_daily", "sum", GSC_TOTAL_DIMS, "gsc_totals", "America/Los_Angeles"),
    Metric("gsc_ctr", "Google CTR (site total)", "FIRST_PARTY", "ratio", "SUM(clicks)/SUM(impressions)", "ratio",
           GSC_TOTAL_DIMS, "gsc_totals", "America/Los_Angeles", "Never average daily CTRs; recompute from sums."),
    Metric("gsc_position", "Average position (GSC)", "FIRST_PARTY", "position",
           "SUM(position*impressions)/SUM(impressions)", "weighted_avg", GSC_TOTAL_DIMS, "gsc_totals",
           "America/Los_Angeles", "Impression-weighted. Never summed. Not comparable to tracked rank."),
    Metric("gsc_query_clicks", "Google clicks (query/page breakdown)", "FIRST_PARTY", "clicks",
           "SUM(clicks) over search_performance_daily", "sum", GSC_DETAIL_DIMS, "gsc_detail", "America/Los_Angeles",
           "Excludes anonymized queries → sums below site total. Do not mix with gsc_clicks."),
    Metric("gsc_query_impressions", "Google impressions (query/page breakdown)", "FIRST_PARTY", "impressions",
           "SUM(impressions) over search_performance_daily", "sum", GSC_DETAIL_DIMS, "gsc_detail", "America/Los_Angeles"),
    Metric("gsc_query_position", "Average position (query/page breakdown)", "FIRST_PARTY", "position",
           "SUM(position*impressions)/SUM(impressions)", "weighted_avg", GSC_DETAIL_DIMS, "gsc_detail", "America/Los_Angeles"),
    Metric("ga4_sessions", "Organic sessions (GA4)", "FIRST_PARTY", "sessions",
           "SUM(sessions) where channel_group='Organic Search'", "sum", GA4_DIMS, "ga4", "property",
           "GA4 property timezone. Not equal to Google clicks."),
    Metric("ga4_users", "Users (GA4, summed daily)", "FIRST_PARTY", "users", "SUM(total_users)", "sum", GA4_DIMS, "ga4",
           "property", "Summed across days/pages → not de-duplicated users."),
    Metric("ga4_signups", "Signups (configured events)", "FIRST_PARTY", "events", "SUM(signups)", "sum", GA4_DIMS, "ga4",
           "property", "NULL when no signup events configured."),
    Metric("ga4_purchases", "Purchases (configured events)", "FIRST_PARTY", "events", "SUM(purchases)", "sum", GA4_DIMS,
           "ga4", "property"),
    Metric("ga4_revenue", "Purchase revenue", "FIRST_PARTY", "currency", "SUM(revenue)", "sum", GA4_DIMS, "ga4", "property"),
    Metric("tracked_rank", "Tracked rank (SERP snapshot)", "PROVIDER", "position", "rank_group of best organic result",
           "latest", ("date", "keyword", "device", "location", "domain"), "rank_tracking", "UTC",
           "From licensed SERP provider. NULL = not in top-N. Not comparable to GSC average position."),
    Metric("visibility_index", "Visibility index", "MODELED", "percent",
           "SUM(CTR(rank_i))/(N*CTR(1))*100 over tracked keywords", "ratio", ("date", "device", "domain", "tag"),
           "rank_tracking", "UTC", "CTR curve versioned (model_version)."),
    Metric("estimated_traffic", "Estimated organic traffic", "MODELED", "visits",
           "SUM(volume*CTR(position, device, features))", "sum", ("date", "domain", "keyword", "page"), "traffic_model",
           "UTC", "Model estimate with error band. Never added to GSC clicks."),
    Metric("search_volume", "Search volume", "PROVIDER", "searches/month", "provider monthly volume", "latest",
           ("keyword", "locale", "month"), "keyword_metrics", "UTC", "As provided; precision field indicates rounding."),
    Metric("referring_domains", "Referring domains (in our index)", "OBSERVED", "domains",
           "COUNT(DISTINCT source_domain) live links", "count_distinct", ("target_domain", "date", "source"), "backlinks",
           "UTC", "Coverage-limited; real number may be higher."),
]}


def get(name: str) -> Metric:
    try:
        return REGISTRY[name]
    except KeyError as exc:
        raise IncompatibleQuery(f"unknown metric: {name}") from exc


def validate(metrics: list[str], dimensions: list[str] | None = None, aggregation_over: list[str] | None = None) -> list[Metric]:
    """Raise IncompatibleQuery for invalid metric/dimension/aggregation combinations."""
    ms = [get(m) for m in metrics]
    if len({m.dataset for m in ms}) > 1:
        raise IncompatibleQuery("metrics from different datasets/grains cannot be combined in one query: "
                                + ", ".join(f"{m.name}({m.dataset})" for m in ms))
    if len({m.source_class for m in ms}) > 1:
        raise IncompatibleQuery("metrics with different source classes cannot be combined")
    for m in ms:
        for d in dimensions or []:
            if d not in m.dimensions:
                raise IncompatibleQuery(f"dimension {d!r} not valid for {m.name} (valid: {', '.join(m.dimensions)})")
        if aggregation_over and m.aggregation in ("weighted_avg", "ratio", "latest"):
            raise IncompatibleQuery(f"{m.name} is a {m.aggregation} metric and cannot be summed across "
                                    f"{', '.join(aggregation_over)}; recompute from base metrics")
    return ms


def catalog() -> list[dict]:
    return [{**asdict(m), "dimensions": list(m.dimensions)} for m in REGISTRY.values()]


def render_markdown() -> str:
    lines = ["# Metric catalog", "", "_Generated from `backend/app/semantic.py` — do not edit by hand._", "",
             "| Metric | Source | Unit | Aggregation | Dimensions | Dataset | TZ | Formula | Notes |", "|---|---|---|---|---|---|---|---|---|"]
    for m in REGISTRY.values():
        lines.append(f"| `{m.name}` {m.label} | {m.source_class} | {m.unit} | {m.aggregation} | {', '.join(m.dimensions)} | "
                     f"{m.dataset} | {m.timezone} | {m.formula} | {m.notes} |")
    lines += ["", "Rules: metrics from different datasets or source classes are never combined; weighted averages and "
              "ratios are recomputed from base sums, never summed; GSC property totals (`gsc_totals`) and page/query "
              "breakdowns (`gsc_detail`) are separate grains."]
    return "\n".join(lines) + "\n"

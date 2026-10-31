"""Dependency-free Prometheus metrics + W3C trace-context propagation.

* Metrics are exposed in the Prometheus text exposition format (v0.0.4) at ``/metrics``.
  Label values are sanitized and label cardinality is bounded (route templates, never raw paths/IDs).
* Tracing: a ``traceparent`` header (W3C Trace Context) is accepted or generated per request and stored in a
  contextvar, so logs, jobs and provider calls can carry the same ``trace_id`` (api → job → provider → ClickHouse).
  When the optional ``opentelemetry`` SDK is installed and ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, spans are also
  exported; otherwise the IDs are propagated only (no exporter, no network).
"""

from __future__ import annotations

import contextvars
import re
import secrets
import threading
import time
from collections.abc import Iterable

_LOCK = threading.Lock()
_MAX_SERIES_PER_METRIC = 2000
DEFAULT_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
_LABEL_SAFE = re.compile(r"[^A-Za-z0-9_:/{}.\-]")


def _esc(v: str) -> str:
    v = _LABEL_SAFE.sub("_", str(v))[:120]
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
    parts = [f'{n}="{_esc(v)}"' for n, v in zip(names, values, strict=True)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, doc: str, labels: Iterable[str] = ()):
        self.name, self.doc, self.label_names = name, doc, tuple(labels)
        self._series: dict[tuple[str, ...], object] = {}
        REGISTRY.register(self)

    def _key(self, labels: dict[str, str] | None) -> tuple[str, ...] | None:
        labels = labels or {}
        key = tuple(str(labels.get(n, "")) for n in self.label_names)
        if key not in self._series and len(self._series) >= _MAX_SERIES_PER_METRIC:
            return None  # cardinality guard: drop new series instead of exhausting memory
        return key


class Counter(_Metric):
    kind = "counter"

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        if amount < 0:
            raise ValueError("counter can only increase")
        with _LOCK:
            k = self._key(labels)
            if k is not None:
                self._series[k] = float(self._series.get(k, 0.0)) + amount  # type: ignore[arg-type]

    def value(self, **labels: str) -> float:
        return float(self._series.get(tuple(str(labels.get(n, "")) for n in self.label_names), 0.0))  # type: ignore[arg-type]

    def render(self) -> list[str]:
        return [f"{self.name}{_labels(self.label_names, k)} {v}" for k, v in self._series.items()]


class Gauge(Counter):
    kind = "gauge"

    def set(self, value: float, **labels: str) -> None:
        with _LOCK:
            k = self._key(labels)
            if k is not None:
                self._series[k] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:  # gauges may go down
        with _LOCK:
            k = self._key(labels)
            if k is not None:
                self._series[k] = float(self._series.get(k, 0.0)) + amount  # type: ignore[arg-type]


class Histogram(_Metric):
    kind = "histogram"

    def __init__(self, name: str, doc: str, labels: Iterable[str] = (), buckets: tuple[float, ...] = DEFAULT_BUCKETS):
        self.buckets = tuple(sorted(buckets))
        super().__init__(name, doc, labels)

    def observe(self, value: float, **labels: str) -> None:
        with _LOCK:
            k = self._key(labels)
            if k is None:
                return
            s = self._series.get(k)
            if s is None:
                s = {"b": [0] * len(self.buckets), "sum": 0.0, "count": 0}
                self._series[k] = s
            for i, b in enumerate(self.buckets):  # type: ignore[union-attr]
                if value <= b:
                    s["b"][i] += 1  # type: ignore[index]
            s["sum"] += value  # type: ignore[index]
            s["count"] += 1  # type: ignore[index]

    def render(self) -> list[str]:
        out = []
        for k, s in self._series.items():
            for b, c in zip(self.buckets, s["b"], strict=True):  # type: ignore[index]
                out.append(f"{self.name}_bucket{_labels(self.label_names, k, f'le=\"{b}\"')} {c}")
            out.append(f"{self.name}_bucket{_labels(self.label_names, k, 'le=\"+Inf\"')} {s['count']}")  # type: ignore[index]
            out.append(f"{self.name}_sum{_labels(self.label_names, k)} {s['sum']}")  # type: ignore[index]
            out.append(f"{self.name}_count{_labels(self.label_names, k)} {s['count']}")  # type: ignore[index]
        return out

    def quantile(self, q: float = 0.95) -> float | None:
        """Upper-bound bucket estimate of quantile ``q`` across all series."""
        with _LOCK:
            total = sum(s["count"] for s in self._series.values())  # type: ignore[index]
            if not total:
                return None
            target = total * q
            for i, b in enumerate(self.buckets):
                cum = sum(s["b"][i] for s in self._series.values())  # type: ignore[index]
                if cum >= target:
                    return b
            return self.buckets[-1]


class Registry:
    def __init__(self) -> None:
        self.metrics: dict[str, _Metric] = {}

    def register(self, m: _Metric) -> None:
        if m.name in self.metrics:
            raise ValueError(f"duplicate metric {m.name}")
        self.metrics[m.name] = m

    def render(self) -> str:
        lines: list[str] = []
        with _LOCK:
            for m in self.metrics.values():
                lines += [f"# HELP {m.name} {m.doc}", f"# TYPE {m.name} {m.kind}", *m.render()]  # type: ignore[attr-defined]
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

HTTP_REQUESTS = Counter("sgi_http_requests_total", "HTTP requests by route template, method and status class",
                        ("route", "method", "status"))
HTTP_LATENCY = Histogram("sgi_http_request_duration_seconds", "HTTP request latency", ("route", "method"))
JOBS_PROCESSED = Counter("sgi_jobs_processed_total", "Jobs finished by kind and outcome", ("kind", "outcome"))
JOB_DURATION = Histogram("sgi_job_duration_seconds", "Job handler duration", ("kind",),
                         buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900, 3600))
QUEUE_DEPTH = Gauge("sgi_job_queue_depth", "Jobs by status (sampled at scrape time)", ("status",))
PROVIDER_CALLS = Counter("sgi_provider_calls_total", "Provider calls by provider, capability and outcome",
                         ("provider", "capability", "outcome"))
PROVIDER_LATENCY = Histogram("sgi_provider_call_duration_seconds", "Provider call latency", ("provider", "capability"))
PROVIDER_COST = Counter("sgi_provider_cost_usd_total", "Provider spend in USD", ("provider",))
CRAWL_PAGES = Counter("sgi_crawl_pages_total", "Pages fetched by crawlers", ("role", "outcome"))
DATA_FRESHNESS = Gauge("sgi_data_freshness_seconds", "Age of the newest record per dataset (sampled)", ("dataset",))


# ---------------------------------------------------------------- tracing
_TRACE: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("sgi_trace", default=None)
_TP = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def parse_traceparent(header: str | None) -> tuple[str, str] | None:
    if not header:
        return None
    m = _TP.match(header.strip().lower())
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None
    return m.group(1), m.group(2)


def start_trace(traceparent: str | None = None) -> tuple[str, str]:
    """Continue an incoming trace (new span id) or start a new one; returns (trace_id, span_id)."""
    parent = parse_traceparent(traceparent)
    ctx = (parent[0] if parent else secrets.token_hex(16), secrets.token_hex(8))
    _TRACE.set(ctx)
    return ctx


def current_trace() -> tuple[str, str] | None:
    return _TRACE.get()


def traceparent() -> str | None:
    """Header value for outbound calls / job payloads, continuing the current trace."""
    ctx = _TRACE.get()
    return f"00-{ctx[0]}-{secrets.token_hex(8)}-01" if ctx else None


class timed:
    """Context manager: ``with timed(HIST, kind="x"): ...`` observes elapsed seconds."""

    def __init__(self, hist: Histogram, **labels: str):
        self.hist, self.labels = hist, labels

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.hist.observe(time.perf_counter() - self.t0, **self.labels)
        return False


def sample_gauges() -> None:
    """Refresh scrape-time gauges (queue depth & data freshness). Swallows errors so /metrics never 500s."""
    try:
        from datetime import UTC, datetime
        from sqlalchemy import text

        from app.db.session import SessionLocal
        now = datetime.now(UTC)
        with SessionLocal() as db:
            for status, n in db.execute(text("SELECT status, count(*) FROM jobs GROUP BY status")).all():
                QUEUE_DEPTH.set(float(n), status=str(status))
            for name, sql in (
                ("crawl_runs", "SELECT max(finished_at) FROM crawl_runs WHERE status = 'completed'"),
                ("sync_jobs", "SELECT max(finished_at) FROM sync_jobs WHERE status = 'completed'"),
                ("provider_cache", "SELECT max(fetched_at) FROM provider_cache"),
            ):
                ts = db.scalar(text(sql))
                if ts is not None:
                    DATA_FRESHNESS.set(max(0.0, (now - ts).total_seconds()), dataset=name)
    except Exception:  # noqa: BLE001  (observability must never break the app)
        pass


def evaluate_slos(db) -> dict:
    """Evaluate service-level objectives (§20): API p95, rank tracking daily completion, sync freshness."""
    from datetime import UTC, date, datetime, timedelta
    from sqlalchemy import func, select
    from app.models import RankProject, Site, SyncJob

    p95 = HTTP_LATENCY.quantile(0.95)
    api_slo = {"target_seconds": 0.3, "observed_p95_seconds": p95, "ok": p95 is None or p95 <= 0.3}

    active_projects = db.scalar(select(func.count()).select_from(RankProject).where(RankProject.is_active.is_(True))) or 0
    checked_today = db.scalar(
        select(func.count()).select_from(RankProject).where(
            RankProject.is_active.is_(True), RankProject.last_check_date >= date.today() - timedelta(days=1)
        )
    ) or 0
    rank_rate = (checked_today / active_projects) if active_projects else 1.0
    rank_slo = {"target_rate": 0.95, "active_projects": active_projects, "checked_within_24h": checked_today,
                "observed_rate": round(rank_rate, 3), "ok": rank_rate >= 0.95}

    cutoff = datetime.now(UTC) - timedelta(hours=36)
    connected_sites = db.scalar(select(func.count()).select_from(Site).where(Site.gsc_property.is_not(None))) or 0
    fresh_sites = db.scalar(
        select(func.count(func.distinct(SyncJob.site_id))).where(
            SyncJob.status == "completed", SyncJob.finished_at >= cutoff
        )
    ) or 0
    sync_ok = connected_sites == 0 or fresh_sites >= connected_sites
    sync_slo = {"target_max_age_hours": 36, "connected_sites": connected_sites, "fresh_sites": fresh_sites, "ok": sync_ok}

    alerts = [name for name, s in (("api_p95", api_slo), ("rank_daily_completion", rank_slo), ("sync_freshness", sync_slo)) if not s["ok"]]
    return {"all_ok": not alerts, "breached": alerts, "slos": {"api_p95": api_slo, "rank_daily_completion": rank_slo, "sync_freshness": sync_slo}}


def check_data_quality_contracts(db, ch=None) -> dict:
    """Data quality contracts (§20): value bounds, null rate, row-count drift, provider schema/error rate."""
    import statistics
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import func, select
    from app.models import ProviderUsage, SearchPerformanceDaily as SP

    contracts = []
    # 1. Value bounds on GSC rows
    invalid_bounds = db.scalar(
        select(func.count()).select_from(SP).where(
            (SP.clicks < 0) | (SP.impressions < 0) | (SP.ctr < 0.0) | (SP.ctr > 1.0) | (SP.position < 1.0)
        )
    ) or 0
    contracts.append({"name": "gsc_value_bounds", "ok": invalid_bounds == 0, "invalid_rows": int(invalid_bounds),
                      "rule": "clicks>=0, impressions>=0, 0<=ctr<=1, position>=1"})

    # 2. Row-count drift across the last 8 finalized days
    by_day = db.execute(select(SP.date, func.count()).group_by(SP.date).order_by(SP.date.desc()).limit(8)).all()
    drift_ok = True
    drift_detail = {"days_checked": len(by_day)}
    if len(by_day) >= 5:
        latest_cnt = by_day[0][1]
        med = statistics.median(c for _, c in by_day[1:])
        ratio = (latest_cnt / med) if med > 0 else 1.0
        drift_ok = 0.2 <= ratio <= 5.0
        drift_detail = {"latest_date": by_day[0][0].isoformat(), "latest_rows": latest_cnt, "median_rows": med, "ratio": round(ratio, 2)}
    contracts.append({"name": "gsc_row_count_drift", "ok": drift_ok, **drift_detail, "rule": "0.2 <= latest/median_7d <= 5.0"})

    # 3. Provider schema / call error rate in last 24h
    since = datetime.now(UTC) - timedelta(hours=24)
    total_calls = db.scalar(select(func.count()).select_from(ProviderUsage).where(ProviderUsage.created_at >= since)) or 0
    failed_calls = db.scalar(
        select(func.count()).select_from(ProviderUsage).where(ProviderUsage.created_at >= since, ProviderUsage.ok.is_(False))
    ) or 0
    err_rate = (failed_calls / total_calls) if total_calls else 0.0
    contracts.append({"name": "provider_error_rate_24h", "ok": err_rate <= 0.25, "total_calls": int(total_calls),
                      "failed_calls": int(failed_calls), "error_rate": round(err_rate, 3), "rule": "error_rate <= 0.25"})

    # 4. ClickHouse keyword_latest bounds if ClickHouse is available
    if ch is not None and getattr(ch, "configured", False) and ch.ping():
        try:
            bad_ch = ch.query("SELECT count() AS c FROM keyword_latest FINAL WHERE kd < 0 OR kd > 100")[0]["c"]
            contracts.append({"name": "clickhouse_kd_bounds", "ok": int(bad_ch) == 0, "invalid_rows": int(bad_ch), "rule": "0 <= kd <= 100"})
        except Exception:
            pass

    suspect = any(not c["ok"] for c in contracts)
    return {
        "healthy": not suspect,
        "suspect": suspect,
        "warning": "Veri kalitesi kontratı ihlali tespit edildi — veriler şüpheli (suspect) olarak işaretlendi." if suspect else None,
        "contracts": contracts,
    }

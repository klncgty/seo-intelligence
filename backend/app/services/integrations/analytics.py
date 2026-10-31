"""Google Analytics 4 Admin + Data API client."""
from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from app.services.integrations.retry import GoogleApiError, request_with_retry

ADMIN_BASE = "https://analyticsadmin.googleapis.com/v1beta"
DATA_BASE = "https://analyticsdata.googleapis.com/v1beta"
CATEGORIES = ("signups", "purchases")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


async def list_properties(client: httpx.AsyncClient, access_token: str) -> list[dict]:
    out: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"pageSize": 200}
        if page_token:
            params["pageToken"] = page_token
        resp = await request_with_retry(
            client, "GET", f"{ADMIN_BASE}/accountSummaries", params=params, headers=_headers(access_token)
        )
        data = resp.json()
        for acc in data.get("accountSummaries", []):
            for p in acc.get("propertySummaries", []):
                out.append(
                    {
                        "property_id": p["property"],
                        "display_name": p.get("displayName"),
                        "account": acc.get("account"),
                    }
                )
        page_token = data.get("nextPageToken")
        if not page_token:
            return out


def _num(value: str) -> int | float | str:
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


async def run_report(
    client: httpx.AsyncClient,
    access_token: str,
    property_id: str,
    start: date,
    end: date,
    dimensions: list[str],
    metrics: list[str],
    dimension_filter: dict | None = None,
    limit: int = 100000,
    meta: dict | None = None,
) -> list[dict]:
    """Run a GA4 report, paginating by offset; metric values become int/float.
    If `meta` is given, GA4 data-quality metadata (sampling, thresholding, "(other)" row loss) and property quota are
    accumulated into it."""
    url = f"{DATA_BASE}/{property_id}:runReport"
    rows: list[dict] = []
    offset = 0
    while True:
        body: dict[str, Any] = {
            "dateRanges": [{"startDate": start.isoformat(), "endDate": end.isoformat()}],
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": m} for m in metrics],
            "limit": limit,
            "offset": offset,
            "returnPropertyQuota": True,
        }
        if dimension_filter:
            body["dimensionFilter"] = dimension_filter
        resp = await request_with_retry(client, "POST", url, json=body, headers=_headers(access_token))
        data = resp.json()
        if meta is not None:
            md = data.get("metadata") or {}
            meta["subject_to_thresholding"] = meta.get("subject_to_thresholding", False) or bool(md.get("subjectToThresholding"))
            meta["data_loss_from_other_row"] = meta.get("data_loss_from_other_row", False) or bool(md.get("dataLossFromOtherRow"))
            if md.get("samplingMetadatas"):
                meta.setdefault("sampling", []).extend(md["samplingMetadatas"])
            if data.get("propertyQuota"):
                meta["property_quota"] = data["propertyQuota"]
        dim_names = [h["name"] for h in data.get("dimensionHeaders", [])] or dimensions
        met_names = [h["name"] for h in data.get("metricHeaders", [])] or metrics
        page = data.get("rows", [])
        for r in page:
            row: dict[str, Any] = {}
            for n, v in zip(dim_names, r.get("dimensionValues", [])):
                row[n] = v.get("value")
            for n, v in zip(met_names, r.get("metricValues", [])):
                row[n] = _num(v.get("value", "0"))
            rows.append(row)
        offset += len(page)
        total = int(data.get("rowCount", offset))
        if not page or len(page) < limit or offset >= total:
            return rows


def _fmt_date(value: str) -> str:
    return f"{value[:4]}-{value[4:6]}-{value[6:8]}" if len(value) == 8 and value.isdigit() else value


async def fetch_landing_daily(
    client: httpx.AsyncClient,
    token: str,
    property_id: str,
    start: date,
    end: date,
    conversion_events: dict[str, list[str]],
    meta: dict | None = None,
) -> list[dict]:
    """Daily landing-page metrics keyed by (date, landing_page, channel_group, source_medium)."""
    dims = ["date", "landingPagePlusQueryString", "sessionDefaultChannelGroup", "sessionSourceMedium"]
    metrics = ["sessions", "totalUsers", "activeUsers", "keyEvents", "purchaseRevenue"]
    optional = ("keyEvents", "purchaseRevenue")
    try:
        base = await run_report(client, token, property_id, start, end, dims, metrics, meta=meta)
    except GoogleApiError as exc:
        text = f"{exc.message}".lower()
        if exc.status != 400 or not any(m.lower() in text for m in optional):
            raise
        base = await run_report(
            client, token, property_id, start, end, dims, [m for m in metrics if m not in optional], meta=meta
        )

    merged: dict[tuple, dict] = {}
    for r in base:
        key = (_fmt_date(r["date"]), r["landingPagePlusQueryString"], r["sessionDefaultChannelGroup"], r["sessionSourceMedium"])
        merged[key] = {
            "date": key[0],
            "landing_page": key[1],
            "channel_group": key[2],
            "source_medium": key[3],
            "sessions": r.get("sessions"),
            "total_users": r.get("totalUsers"),
            "active_users": r.get("activeUsers"),
            "key_events": r.get("keyEvents"),
            "purchase_revenue": r.get("purchaseRevenue"),
            "signups": None,
            "purchases": None,
        }

    for cat in CATEGORIES:
        names = conversion_events.get(cat) or []
        if not names:
            continue
        ev_rows = await run_report(
            client,
            token,
            property_id,
            start,
            end,
            dims + ["eventName"],
            ["eventCount"],
            dimension_filter={"filter": {"fieldName": "eventName", "inListFilter": {"values": list(names)}}}, meta=meta,
        )
        for row in merged.values():
            row[cat] = 0
        for r in ev_rows:
            if r.get("eventName") not in names:  # defensive: never trust the server filter alone
                continue
            key = (_fmt_date(r["date"]), r["landingPagePlusQueryString"], r["sessionDefaultChannelGroup"], r["sessionSourceMedium"])
            if key not in merged:
                merged[key] = {
                    "date": key[0], "landing_page": key[1], "channel_group": key[2], "source_medium": key[3],
                    "sessions": None, "total_users": None, "active_users": None,
                    "key_events": None, "purchase_revenue": None,
                    "signups": None if cat != "signups" else 0,
                    "purchases": None if cat != "purchases" else 0,
                }
                other = "purchases" if cat == "signups" else "signups"
                merged[key][other] = 0 if conversion_events.get(other) else None
            merged[key][cat] += int(r.get("eventCount", 0))
    return [merged[k] for k in sorted(merged)]

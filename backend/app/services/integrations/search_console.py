"""Google Search Console API client."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from urllib.parse import quote

import httpx

from app.services.integrations.retry import request_with_retry

BASE = "https://www.googleapis.com/webmasters/v3"
DEFAULT_DIMENSIONS = ["date", "page", "query", "device", "country"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


async def list_sites(client: httpx.AsyncClient, access_token: str) -> list[dict]:
    resp = await request_with_retry(client, "GET", f"{BASE}/sites", headers=_headers(access_token))
    return [
        {"site_url": s["siteUrl"], "permission_level": s.get("permissionLevel")}
        for s in resp.json().get("siteEntry", [])
    ]


async def iter_search_analytics(
    client: httpx.AsyncClient,
    access_token: str,
    site_url: str,
    start: date,
    end: date,
    dimensions: list[str] | None = None,
    row_limit: int = 25000,
    data_state: str = "final",
    max_rows: int = 5_000_000,
) -> AsyncIterator[dict]:
    """Yield search analytics rows, paginating by startRow until a short page or max_rows."""
    dims = list(dimensions or DEFAULT_DIMENSIONS)
    url = f"{BASE}/sites/{quote(site_url, safe='')}/searchAnalytics/query"
    start_row = 0
    yielded = 0
    while yielded < max_rows:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": dims,
            "rowLimit": row_limit,
            "startRow": start_row,
            "dataState": data_state,
        }
        resp = await request_with_retry(client, "POST", url, json=body, headers=_headers(access_token))
        rows = resp.json().get("rows", [])
        for r in rows:
            keys = r.get("keys", [])
            out: dict = {d: None for d in DEFAULT_DIMENSIONS}
            out.update(zip(dims, keys))
            out["clicks"] = int(r.get("clicks", 0))
            out["impressions"] = int(r.get("impressions", 0))
            out["ctr"] = float(r.get("ctr", 0.0))
            out["position"] = float(r.get("position", 0.0))
            yield out
            yielded += 1
            if yielded >= max_rows:
                return
        if len(rows) < row_limit:
            return
        start_row += len(rows)

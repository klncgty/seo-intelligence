"""Import Google Search Console exported ZIP or CSV into seo-intelligence."""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import (
    SearchPerformanceDaily,
    SearchTotalsDaily,
    Site,
)

log = logging.getLogger(__name__)


def _parse_float(val: Any) -> float:
    if val is None or val == "":
        return 0.0
    s = str(val).replace("%", "").strip()
    try:
        f = float(s)
        if "%" in str(val):
            return f / 100.0
        return f
    except ValueError:
        return 0.0


def _parse_int(val: Any) -> int:
    if val is None or val == "":
        return 0
    try:
        return int(float(str(val).replace(",", "").strip()))
    except ValueError:
        return 0


def import_gsc_data(db: Session, site_id: int, file_path: str | Path, import_date: date | None = None) -> dict[str, Any]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    site = db.get(Site, site_id)
    if not site:
        raise ValueError(f"Site {site_id} not found")

    target_date = import_date or datetime.now(UTC).date()
    queries_imported = 0
    pages_imported = 0
    total_clicks = 0
    total_impressions = 0

    def process_queries_csv(f):
        nonlocal queries_imported, total_clicks, total_impressions
        reader = csv.reader(f)
        header = None
        for row in reader:
            if not row or not any(row):
                continue
            if header is None:
                header = [c.lower().strip() for c in row]
                continue

            query = row[0].strip()
            if not query or query.lower() in ("top queries", "queries"):
                continue

            clicks = _parse_int(row[1]) if len(row) > 1 else 0
            impressions = _parse_int(row[2]) if len(row) > 2 else 0
            ctr = _parse_float(row[3]) if len(row) > 3 else 0.0
            position = _parse_float(row[4]) if len(row) > 4 else 0.0

            total_clicks += clicks
            total_impressions += impressions

            stmt = insert(SearchPerformanceDaily).values(
                site_id=site_id,
                date=target_date,
                page=site.base_url,
                query=query,
                device="all",
                country="all",
                clicks=clicks,
                impressions=impressions,
                ctr=ctr,
                position=position,
            ).on_conflict_do_update(
                index_elements=["site_id", "date", "page", "query", "device", "country"],
                set_={
                    "clicks": clicks,
                    "impressions": impressions,
                    "ctr": ctr,
                    "position": position,
                },
            )
            db.execute(stmt)
            queries_imported += 1

    def process_pages_csv(f):
        nonlocal pages_imported
        reader = csv.reader(f)
        header = None
        for row in reader:
            if not row or not any(row):
                continue
            if header is None:
                header = [c.lower().strip() for c in row]
                continue

            page_url = row[0].strip()
            if not page_url or page_url.lower() in ("top pages", "pages"):
                continue

            clicks = _parse_int(row[1]) if len(row) > 1 else 0
            impressions = _parse_int(row[2]) if len(row) > 2 else 0
            ctr = _parse_float(row[3]) if len(row) > 3 else 0.0
            position = _parse_float(row[4]) if len(row) > 4 else 0.0

            stmt = insert(SearchPerformanceDaily).values(
                site_id=site_id,
                date=target_date,
                page=page_url,
                query="",
                device="all",
                country="all",
                clicks=clicks,
                impressions=impressions,
                ctr=ctr,
                position=position,
            ).on_conflict_do_update(
                index_elements=["site_id", "date", "page", "query", "device", "country"],
                set_={
                    "clicks": clicks,
                    "impressions": impressions,
                    "ctr": ctr,
                    "position": position,
                },
            )
            db.execute(stmt)
            pages_imported += 1

    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path, "r") as z:
            for filename in z.namelist():
                lower = filename.lower()
                if "queries" in lower and lower.endswith(".csv"):
                    with z.open(filename) as f:
                        process_queries_csv(io.TextIOWrapper(f, encoding="utf-8-sig"))
                elif "pages" in lower and lower.endswith(".csv"):
                    with z.open(filename) as f:
                        process_pages_csv(io.TextIOWrapper(f, encoding="utf-8-sig"))
    elif path.suffix.lower() == ".csv":
        with open(path, encoding="utf-8-sig") as f:
            sample = f.read(2048)
            f.seek(0)
            if "top pages" in sample.lower() or "pages" in path.name.lower():
                process_pages_csv(f)
            else:
                process_queries_csv(f)
    elif path.is_dir():
        for p in path.glob("*.csv"):
            lower = p.name.lower()
            if "queries" in lower:
                with open(p, encoding="utf-8-sig") as f:
                    process_queries_csv(f)
            elif "pages" in lower:
                with open(p, encoding="utf-8-sig") as f:
                    process_pages_csv(f)

    # Update daily totals
    stmt = insert(SearchTotalsDaily).values(
        site_id=site_id,
        date=target_date,
        clicks=total_clicks,
        impressions=total_impressions,
        ctr=(total_clicks / total_impressions) if total_impressions > 0 else 0.0,
        position=0.0,
    ).on_conflict_do_update(
        index_elements=["site_id", "date"],
        set_={
            "clicks": total_clicks,
            "impressions": total_impressions,
            "ctr": (total_clicks / total_impressions) if total_impressions > 0 else 0.0,
        },
    )
    db.execute(stmt)
    db.commit()

    return {
        "status": "ok",
        "site_id": site_id,
        "site_name": site.name,
        "date": target_date.isoformat(),
        "queries_imported": queries_imported,
        "pages_imported": pages_imported,
        "total_clicks": total_clicks,
        "total_impressions": total_impressions,
    }

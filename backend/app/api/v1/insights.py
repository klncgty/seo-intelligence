"""First-party analytics depth + Growth Engine v2 API (spec §11, §15)."""

from datetime import date, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.api.v1.common import date_range, get_or_404
from app.auth import Principal, current_principal, get_db
from app.infra.clickhouse import ClickHouse
from app.models import CalendarEvent, GrowthOpportunity, Site, SplitTest
from app.services import analytics_depth as ad
from app.services import growth_v2

router = APIRouter()
Metric = Literal["gsc_clicks", "ga4_sessions"]


@router.get("/sites/{site_id}/data-quality")
def data_quality(site_id: int, start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    s, e = date_range(start, end)
    return ad.data_quality(db, get_or_404(db, Site, site_id, "site"), s, e)


@router.get("/sites/{site_id}/join-coverage")
def join_coverage(site_id: int, start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    s, e = date_range(start, end)
    return ad.join_coverage(db, get_or_404(db, Site, site_id, "site"), s, e)


@router.get("/sites/{site_id}/segments")
def segments(site_id: int, start: date | None = None, end: date | None = None, db: Session = Depends(get_db)):
    s, e = date_range(start, end)
    return ad.segments(db, get_or_404(db, Site, site_id, "site"), s, e)


@router.get("/sites/{site_id}/anomalies")
def anomalies(site_id: int, metric: Metric = "gsc_clicks", days: int = Query(90, ge=7, le=365),
              method: Literal["weekday", "stl"] = "weekday", db: Session = Depends(get_db)):
    return ad.anomalies(db, get_or_404(db, Site, site_id, "site"), metric, days, method)


@router.get("/sites/{site_id}/forecast")
def forecast(site_id: int, metric: Metric = "gsc_clicks", horizon: int = Query(14, ge=1, le=60), db: Session = Depends(get_db)):
    return ad.forecast(db, get_or_404(db, Site, site_id, "site"), metric, horizon)


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["campaign", "annotation", "search_update", "holiday"]
    name: str = Field(min_length=1, max_length=200)
    start_date: date
    end_date: date
    site_id: int | None = None

    @model_validator(mode="after")
    def _order(self):
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 366:
            raise ValueError("invalid date range")
        return self


@router.get("/calendar-events")
def list_events(start: date | None = None, end: date | None = None, site_id: int | None = None, db: Session = Depends(get_db)):
    s, e = start or date.today() - timedelta(days=365), end or date.today() + timedelta(days=90)
    return {"items": [{"id": x.id, "kind": x.kind, "name": x.name, "start_date": x.start_date.isoformat(), "end_date": x.end_date.isoformat(),
                       "site_id": x.site_id, "scope": "global" if x.org_id is None else "organization", "source": x.source}
                      for x in ad.events(db, s, e, site_id)]}


@router.post("/calendar-events", status_code=201)
def create_event(body: EventIn, db: Session = Depends(get_db), p: Principal = Depends(current_principal)):
    if body.site_id:
        get_or_404(db, Site, body.site_id, "site")
    ev = CalendarEvent(org_id=p.org_id, site_id=body.site_id, kind=body.kind, name=body.name, start_date=body.start_date,
                       end_date=body.end_date, source="user")
    db.add(ev)
    db.commit()
    return {"id": ev.id}


@router.post("/sites/{site_id}/opportunities/regenerate")
def regenerate(site_id: int, db: Session = Depends(get_db)):
    ch = ClickHouse()
    return growth_v2.regenerate(db, get_or_404(db, Site, site_id, "site"), ch if ch.configured else None)


@router.post("/opportunities/{opp_id}/measure")
def measure(opp_id: int, db: Session = Depends(get_db)):
    return growth_v2.measure(db, get_or_404(db, GrowthOpportunity, opp_id, "opportunity"))


@router.get("/opportunities-calibration")
def calibration(site_id: int | None = None, db: Session = Depends(get_db)):
    return growth_v2.calibration(db, site_id)


@router.get("/portfolio")
def portfolio(weekly_hours: float = Query(20, gt=0, le=200), weeks: int = Query(8, ge=1, le=52), db: Session = Depends(get_db)):
    return growth_v2.portfolio(db, weekly_hours, weeks)


class SplitTestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    hypothesis: str = Field(min_length=1, max_length=2000)
    urls: list[str] = Field(min_length=2, max_length=500)
    template_sig: str | None = Field(default=None, max_length=16)
    started_at: date | None = None


def _split_out(st: SplitTest) -> dict:
    return {
        "id": st.id,
        "site_id": st.site_id,
        "name": st.name,
        "hypothesis": st.hypothesis,
        "template_sig": st.template_sig,
        "control_urls": st.control_urls,
        "variant_urls": st.variant_urls,
        "started_at": st.started_at.isoformat() if st.started_at else None,
        "result": st.result,
    }


@router.post("/sites/{site_id}/split-tests", status_code=201)
def create_split_test(site_id: int, body: SplitTestIn, db: Session = Depends(get_db)):
    site = get_or_404(db, Site, site_id, "site")
    st = growth_v2.create_split_test(db, site, body.name, body.hypothesis, body.urls, body.template_sig, body.started_at)
    return _split_out(st)


@router.get("/sites/{site_id}/split-tests")
def list_split_tests(site_id: int, db: Session = Depends(get_db)):
    from sqlalchemy import select
    get_or_404(db, Site, site_id, "site")
    rows = db.scalars(select(SplitTest).where(SplitTest.site_id == site_id).order_by(SplitTest.id.desc())).all()
    return {"items": [_split_out(r) for r in rows]}


@router.post("/split-tests/{test_id}/evaluate")
def evaluate_split_test(test_id: int, db: Session = Depends(get_db)):
    st = get_or_404(db, SplitTest, test_id, "split_test")
    return growth_v2.evaluate_split_test(db, st)


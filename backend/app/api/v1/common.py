from datetime import date, timedelta

from fastapi import HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.schemas import DateRange


class ApiError(HTTPException):
    def __init__(self, status: int, code: str, message: str, details=None):
        super().__init__(status_code=status, detail={"code": code, "message": message, "details": details})


def not_found(what: str) -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def get_or_404(db: Session, model, obj_id: int, what: str):
    obj = db.get(model, obj_id)
    if obj is None:
        raise not_found(what)
    return obj


class Page:
    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
        self.page, self.page_size = page, page_size
        self.offset = (page - 1) * page_size


def paginate(db: Session, stmt, page: Page, mapper) -> dict:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = db.execute(stmt.offset(page.offset).limit(page.page_size)).scalars().all()
    return {"items": [mapper(i) for i in items], "total": total, "page": page.page, "page_size": page.page_size}


def date_range(start: date | None, end: date | None, default_days: int = 28) -> tuple[date, date]:
    end = end or date.today() - timedelta(days=3)
    start = start or end - timedelta(days=default_days - 1)
    try:
        DateRange(start=start, end=end)
    except ValueError as exc:
        raise ApiError(422, "validation_error", "invalid date range", str(exc)) from exc
    return start, end


def previous_range(start: date, end: date) -> tuple[date, date]:
    days = (end - start).days + 1
    pend = start - timedelta(days=1)
    return pend - timedelta(days=days - 1), pend

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@event.listens_for(engine, "checkin")
def _reset_tenant(dbapi_conn, _record):
    """Never let a tenant context (app.org_id, used by RLS) leak to the next user of a pooled connection."""
    try:
        cur = dbapi_conn.cursor()
        cur.execute("RESET app.org_id")
        cur.close()
        dbapi_conn.commit()
    except Exception:
        _record.invalidate()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

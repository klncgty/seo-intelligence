import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://seo:seo@127.0.0.1:5432/seo_intelligence_test")
os.environ["TOKEN_ENCRYPTION_KEY"] = "x6Xr3lBqZ0m3m3qXk7oB7d5jv4c8i1gQwZt2E3sKk4A="
os.environ["CRAWLER_MIN_REQUEST_DELAY_MS"] = "0"
os.environ["CRAWLER_DEFAULT_REQUEST_DELAY_MS"] = "0"
os.environ["COMPETITOR_MIN_REQUEST_DELAY_MS"] = "0"
os.environ["GOOGLE_CLIENT_ID"] = ""
os.environ["GOOGLE_CLIENT_SECRET"] = ""
os.environ["SEO_DATA_PROVIDER"] = ""

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.db.session import Base, SessionLocal, engine  # noqa: E402

BACKEND = os.path.dirname(os.path.dirname(__file__))


@pytest.fixture(scope="session", autouse=True)
def _migrate():
    cfg = Config(os.path.join(BACKEND, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND, "alembic"))
    cfg.attributes["url"] = os.environ["DATABASE_URL"]
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def db():
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        # re-seed the LOCAL tenant created by migration 0005
        conn.execute(text("INSERT INTO organizations (id, name, plan, is_demo, suspended, cross_tenant_opt_in) "
                          "VALUES (1, 'Local', 'local', false, false, false)"))
        conn.execute(text("INSERT INTO workspaces (id, org_id, name) VALUES (1, 1, 'Default')"))
        conn.execute(text("INSERT INTO projects (id, org_id, workspace_id, name) VALUES (1, 1, 1, 'Default')"))
        for t in ("organizations", "workspaces", "projects"):
            conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{t}', 'id'), (SELECT max(id) FROM {t}))"))
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def settings(monkeypatch):
    s = get_settings()

    class Setter:
        def __call__(self, **kw):
            for k, v in kw.items():
                monkeypatch.setattr(s, k, v)
            return s
    return Setter()


class FixtureSite:
    """Tiny real HTTP server on 127.0.0.1 serving a dict of routes."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.hits: list[str] = []
        self.request_headers: list[dict] = []
        self.posts: list[dict] = []
        site = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                site.hits.append(self.path)
                site.request_headers.append({k.lower(): v for k, v in self.headers.items()})
                status, headers, body = site.routes.get(self.path, (404, {"Content-Type": "text/html"}, b"<html><title>404</title></html>"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                body = body if isinstance(body, bytes) else body.encode()
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                site.posts.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": self.rfile.read(n)})
                status = site.routes.get(("POST", self.path), (200,))[0]
                self.send_response(status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture()
def fixture_site():
    sites = []

    def make(routes):
        s = FixtureSite(routes)
        sites.append(s)
        return s
    yield make
    for s in sites:
        s.close()


def html(title="T", body="", head=""):
    return (200, {"Content-Type": "text/html; charset=utf-8"},
            f"<html><head><title>{title}</title>{head}</head><body>{body}</body></html>")

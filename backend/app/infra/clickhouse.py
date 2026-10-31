"""Minimal ClickHouse HTTP client + SQL migration runner.

Reads use parameterized queries ({name:Type} placeholders, values sent as param_<name>), never
string interpolation. Tenant-scoped tables must always be queried through ``tenant_query`` which
injects the mandatory tenant_id filter (see docs/adr/0005-tenant-isolation.md).
"""

import json
import pathlib
import re

import httpx

from app.core.config import get_settings

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "clickhouse" / "migrations"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _param(v) -> str:
    """Encode a query parameter in ClickHouse's text format (arrays as ['a','b'] with escaping)."""
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(_quote(x) for x in v) + "]"
    if isinstance(v, bool):
        return "1" if v else "0"
    return v if isinstance(v, str) else str(v)


def _quote(x) -> str:
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return str(x)
    return "'" + str(x).replace("\\", "\\\\").replace("'", "\\'") + "'"


class ClickHouseUnavailable(RuntimeError):
    pass


class ClickHouse:
    def __init__(self, url: str | None = None, database: str | None = None, user: str | None = None,
                 password: str | None = None, timeout: float = 30.0):
        s = get_settings()
        self.url = (url if url is not None else s.clickhouse_url).rstrip("/")
        self.database = database or s.clickhouse_database
        if not _IDENT.match(self.database):
            raise ValueError("invalid ClickHouse database name")
        u = user or s.clickhouse_user
        p = password if password is not None else s.clickhouse_password
        self.auth = (u, p) if (u and p) else None
        self._client = httpx.Client(timeout=timeout, trust_env=False)

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def _post(self, sql: str, params: dict | None = None, database: str | None = None, data: bytes | None = None) -> str:
        if not self.configured:
            raise ClickHouseUnavailable("CLICKHOUSE_URL not set")
        q = {"database": database or self.database}
        for k, v in (params or {}).items():
            q[f"param_{k}"] = _param(v)
        from app.infra import observability as obs
        headers = {}
        if tp := obs.traceparent():
            headers["traceparent"] = tp
        try:
            if data is None:
                r = self._client.post(self.url, params=q, content=sql.encode(), auth=self.auth, headers=headers)
            else:
                q["query"] = sql
                r = self._client.post(self.url, params=q, content=data, auth=self.auth, headers=headers)
        except httpx.HTTPError as exc:
            raise ClickHouseUnavailable(f"ClickHouse unreachable: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise RuntimeError(f"ClickHouse error {r.status_code}: {r.text[:500]}")
        return r.text

    def ping(self) -> bool:
        try:
            return self._client.get(f"{self.url}/ping", timeout=3).text.strip() == "Ok."
        except httpx.HTTPError:
            return False

    def command(self, sql: str, params: dict | None = None, database: str | None = None) -> None:
        self._post(sql, params, database)

    def query(self, sql: str, params: dict | None = None) -> list[dict]:
        out = self._post(sql.rstrip().rstrip(";") + " FORMAT JSONEachRow", params)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def insert(self, table: str, rows: list[dict]) -> int:
        if not rows:
            return 0
        if not _IDENT.match(table):
            raise ValueError("invalid table name")
        body = "\n".join(json.dumps(r, default=str) for r in rows).encode()
        self._post(f"INSERT INTO {table} FORMAT JSONEachRow", data=body)
        return len(rows)

    def tenant_query(self, sql: str, tenant_id: int, params: dict | None = None) -> list[dict]:
        """Queries on tenant tables must reference {tenant_id:UInt64}; refuse otherwise."""
        if "{tenant_id:UInt64}" not in sql:
            raise ValueError("tenant-scoped query must filter on {tenant_id:UInt64}")
        return self.query(sql, {**(params or {}), "tenant_id": tenant_id})

    # ---------------------------------------------------------------- migrations
    def migrate(self) -> list[str]:
        self.command(f"CREATE DATABASE IF NOT EXISTS {self.database}", database="default")
        self.command("CREATE TABLE IF NOT EXISTS schema_migrations (version String, applied_at DateTime DEFAULT now()) "
                     "ENGINE = ReplacingMergeTree ORDER BY version")
        done = {r["version"] for r in self.query("SELECT version FROM schema_migrations FINAL")}
        applied = []
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if f.stem in done:
                continue
            for chunk in f.read_text().split(";\n"):
                stmt = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip()
                if stmt:
                    self.command(stmt)
            self.insert("schema_migrations", [{"version": f.stem}])
            applied.append(f.stem)
        return applied

    def head(self) -> str | None:
        rows = self.query("SELECT max(version) AS v FROM schema_migrations")
        return rows[0]["v"] if rows and rows[0]["v"] else None


def get_clickhouse() -> ClickHouse:
    return ClickHouse()

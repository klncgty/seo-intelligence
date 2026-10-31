"""Minimal read-only MCP server (stdio, JSON-RPC 2.0) so Claude can query the platform as a tool.

Tenant scope: every call goes through the HTTP API in-process with SGI_API_KEY (use a viewer-role key) or LOCAL mode.
Only GET endpoints are exposed; rate-limited to 5 calls/s. Register with Claude Code:
  claude mcp add growth-intelligence -- uv --directory <repo>/backend run seo mcp
"""

import json
import os
import sys
import time

PROTOCOL = "2025-06-18"
TOOLS = {
    "overview": ("Multi-site performance overview (GSC/GA4 first-party, separate)", "/api/v1/dashboard/overview", {}),
    "opportunities": ("Growth opportunities (sorted by priority)", "/api/v1/opportunities",
                      {"site_id": "integer", "status": "string", "type": "string"}),
    "crawl_summary": ("Audit summary of a crawl", "/api/v1/crawls/{crawl_id}/summary", {"crawl_id": "integer"}),
    "keyword_explorer": ("Keyword explorer (global dataset)", "/api/v1/keywords/explorer",
                         {"q": "string", "database": "string", "min_volume": "integer", "max_kd": "number"}),
    "rank_overview": ("Rank tracking overview", "/api/v1/rank-projects/{rank_project_id}/overview", {"rank_project_id": "integer"}),
    "backlinks_overview": ("Backlink overview for a domain (coverage-limited index)", "/api/v1/backlinks/{domain}/overview", {"domain": "string"}),
    "domain_overview": ("Domain overview: observed / provider / actual panels", "/api/v1/domains/{domain}/overview", {"domain": "string"}),
    "metric_catalog": ("Semantic metric catalog", "/api/v1/metrics/catalog", {}),
}


class Server:
    def __init__(self, client=None):
        if client is None:
            from fastapi.testclient import TestClient

            from app.main import app
            client = TestClient(app, raise_server_exceptions=False)
        self.client = client
        self.headers = {"Authorization": f"Bearer {os.environ['SGI_API_KEY']}"} if os.environ.get("SGI_API_KEY") else {}
        self.calls: list[float] = []

    def _rate_ok(self) -> bool:
        now = time.monotonic()
        self.calls = [t for t in self.calls if now - t < 1.0]
        if len(self.calls) >= 5:
            return False
        self.calls.append(now)
        return True

    def tools(self) -> list[dict]:
        return [{"name": n, "description": d,
                 "inputSchema": {"type": "object", "properties": {k: {"type": t} for k, t in params.items()},
                                 "required": [k for k in params if "{" + k + "}" in path]},
                 "annotations": {"readOnlyHint": True}} for n, (d, path, params) in TOOLS.items()]

    def call(self, name: str, args: dict) -> dict:
        if name not in TOOLS:
            return {"isError": True, "content": [{"type": "text", "text": f"unknown tool {name}"}]}
        if not self._rate_ok():
            return {"isError": True, "content": [{"type": "text", "text": "rate limited (5 calls/s)"}]}
        _, path, params = TOOLS[name]
        args = {k: v for k, v in (args or {}).items() if k in params}
        for k in list(args):
            if "{" + k + "}" in path:
                path = path.replace("{" + k + "}", str(args.pop(k)).replace("/", ""))
        if "{" in path:
            return {"isError": True, "content": [{"type": "text", "text": "missing required path argument"}]}
        r = self.client.get(path, params=args, headers=self.headers)
        return {"isError": r.status_code >= 400, "content": [{"type": "text", "text": r.text[:100_000]}]}

    def handle(self, msg: dict) -> dict | None:
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "growth-intelligence", "version": "0.2.0"}}
        elif method == "tools/list":
            result = {"tools": self.tools()}
        elif method == "tools/call":
            p = msg.get("params") or {}
            result = self.call(p.get("name", ""), p.get("arguments") or {})
        elif method == "ping":
            result = {}
        elif mid is None:
            return None  # notification (e.g. notifications/initialized)
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> None:
    srv = Server()
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            resp = srv.handle(json.loads(line))
        except Exception as exc:  # never crash the stdio loop
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": type(exc).__name__}}
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()

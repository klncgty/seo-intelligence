"""Safe HTTP fetching: allowlist + SSRF checks on every hop, bounded redirects/size/time."""

import http.cookiejar
import time
from dataclasses import dataclass, field

import httpcore
import httpx

from app.services.crawler.ssrf import BlockedDestination, SafeNetworkBackend, check_url
from app.services.crawler.urls import normalize_url


@dataclass
class FetchPolicy:
    allowed_hosts: list[str]
    user_agent: str
    timeout_s: float = 15.0
    max_bytes: int = 5 * 1024 * 1024
    max_redirects: int = 5
    allow_private: bool = False
    html_only: bool = True
    # Path regexes never requested (competitor crawls: login/account/cart/checkout areas).
    skip_path_patterns: list[str] = field(default_factory=list)


@dataclass
class FetchResult:
    requested_url: str
    final_url: str | None = None
    status_code: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str | None = None
    body: bytes = b""
    redirect_chain: list[dict] = field(default_factory=list)  # [{url, status_code}] hops before final
    elapsed_ms: int | None = None
    error: str | None = None
    blocked: bool = False
    truncated: bool = False

    @property
    def is_html(self) -> bool:
        return bool(self.content_type and "html" in self.content_type.lower())


class _NoCookieJar(http.cookiejar.CookieJar):
    def extract_cookies(self, response, request) -> None:
        return None

    def set_cookie(self, cookie) -> None:
        return None


def build_client(policy: FetchPolicy, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    if transport is None:
        transport = httpx.AsyncHTTPTransport(retries=0)
        # Replace the connection pool so every TCP connect goes through the SSRF-validating backend.
        transport._pool = httpcore.AsyncConnectionPool(
            network_backend=SafeNetworkBackend(allow_private=policy.allow_private),
            max_connections=20,
            http2=False,
        )
    # Cookie jar that stores nothing: the crawler never holds sessions or logged-in state.
    client = httpx.AsyncClient(
        transport=transport,
        follow_redirects=False,
        trust_env=False,  # never route through env proxies
        timeout=httpx.Timeout(policy.timeout_s),
        headers={"User-Agent": policy.user_agent, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"},
    )
    client.cookies.jar = _NoCookieJar()  # httpx copies jars passed in, so replace it in place
    return client


async def safe_get(client: httpx.AsyncClient, url: str, policy: FetchPolicy, accept_non_html: bool = False) -> FetchResult:
    result = FetchResult(requested_url=url)
    current = url
    visited: set[str] = set()
    start = time.monotonic()
    for hop in range(policy.max_redirects + 1):
        try:
            check_url(current, policy.allowed_hosts, policy.allow_private)
        except BlockedDestination as exc:
            result.blocked, result.error = True, f"blocked: {exc}"
            return result
        if current in visited:
            result.error = "redirect loop"
            result.redirect_chain.append({"url": current, "status_code": None, "loop": True})
            return result
        visited.add(current)
        try:
            async with client.stream("GET", current) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    nxt = normalize_url(resp.headers["location"], base=current)
                    result.redirect_chain.append({"url": current, "status_code": resp.status_code})
                    if nxt is None:
                        result.error = "redirect to unsupported URL"
                        return result
                    current = nxt
                    continue
                result.final_url = current
                result.status_code = resp.status_code
                result.headers = {k.lower(): v for k, v in resp.headers.items()}
                result.content_type = resp.headers.get("content-type")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > policy.max_bytes:
                    result.truncated, result.error = True, f"response exceeds {policy.max_bytes} bytes"
                    return result
                if policy.html_only and not accept_non_html and not result.is_html:
                    return result  # record status + type, skip body
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > policy.max_bytes:
                        result.truncated, result.error = True, f"response exceeds {policy.max_bytes} bytes"
                        return result
                    chunks.append(chunk)
                result.body = b"".join(chunks)
                return result
        except BlockedDestination as exc:
            result.blocked, result.error = True, f"blocked: {exc}"
            return result
        except httpx.TimeoutException:
            result.error = "timeout"
            return result
        except httpx.HTTPError as exc:
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, BlockedDestination) or "BlockedDestination" in repr(exc):
                result.blocked, result.error = True, f"blocked: {cause or exc}"
            else:
                result.error = f"{type(exc).__name__}: {exc}"[:500]
            return result
        finally:
            result.elapsed_ms = int((time.monotonic() - start) * 1000)
    result.error = f"too many redirects (>{policy.max_redirects})"
    return result


async def safe_post(url: str, payload: bytes, headers: dict[str, str], *, allow_private: bool = False, timeout_s: float = 10.0,
                    transport: httpx.AsyncBaseTransport | None = None) -> tuple[int | None, str | None]:
    """POST to a user-supplied URL (webhooks): same SSRF policy as the crawler, https/http on 80/443 only,
    redirects are NOT followed, response body ignored. Returns (status, error)."""
    host = (httpx.URL(url).host or "").lower()
    policy = FetchPolicy(allowed_hosts=[host], user_agent="GrowthIntelligence-Webhook/1.0", timeout_s=timeout_s,
                         max_bytes=64 * 1024, allow_private=allow_private)
    try:
        check_url(url, policy.allowed_hosts, allow_private)
    except BlockedDestination as exc:
        return None, f"blocked: {exc}"
    async with build_client(policy, transport) as c:
        try:
            r = await c.post(url, content=payload, headers={"Content-Type": "application/json", **headers})
        except httpx.HTTPError as exc:
            cause = exc.__cause__ or exc.__context__
            if isinstance(cause, BlockedDestination):
                return None, f"blocked: {cause}"
            return None, type(exc).__name__
        except BlockedDestination as exc:
            return None, f"blocked: {exc}"
    return r.status_code, None if r.status_code < 300 else f"HTTP {r.status_code}"

"""URL normalization.

Tracking-parameter policy: query parameters are preserved (they can change page content)
EXCEPT the well-known analytics/ad click identifiers listed in TRACKING_PARAMS and any
parameter starting with ``utm_``. Remaining parameters are kept in their original order.
Fragments are always removed. Scheme and host are lower-cased, default ports dropped,
empty path becomes "/". Percent-encoding is normalized for unreserved characters only.
"""

from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

TRACKING_PARAMS = frozenset(
    {"gclid", "gbraid", "wbraid", "dclid", "fbclid", "msclkid", "yclid", "twclid", "ttclid",
     "mc_cid", "mc_eid", "_ga", "_gl", "igshid", "srsltid"}
)
_SAFE_PATH = "/:@!$&'()*+,;=-._~%"


def _remove_dot_segments(path: str) -> str:
    out: list[str] = []
    for seg in path.split("/"):
        if seg == "..":
            if len(out) > 1:
                out.pop()
        elif seg != ".":
            out.append(seg)
    res = "/".join(out)
    if path.endswith(("/.", "/..")):
        res += "/"
    return res if res.startswith("/") else "/" + res


def is_tracking_param(name: str) -> bool:
    n = name.lower()
    return n.startswith("utm_") or n in TRACKING_PARAMS


def normalize_url(url: str, base: str | None = None) -> str | None:
    """Return the normalized absolute http(s) URL, or None if not crawlable."""
    url = url.strip()
    if base:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        port = parts.port
    except (UnicodeError, ValueError):
        return None
    if parts.username or parts.password:
        return None  # credentials in URLs are never followed
    netloc = host if ":" not in host else f"[{host}]"
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += f":{port}"
    path = quote(unquote(_remove_dot_segments(parts.path or "/")), safe=_SAFE_PATH) or "/"
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not is_tracking_param(k)]
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def redact_url(url: str) -> str:
    """For logs: strip query string values which may carry tokens."""
    p = urlsplit(url)
    if not p.query:
        return url
    keys = "&".join(f"{k}=***" for k, _ in parse_qsl(p.query, keep_blank_values=True))
    return urlunsplit((p.scheme, p.netloc, p.path, keys, ""))

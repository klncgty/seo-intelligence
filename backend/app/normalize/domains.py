"""Registrable domain (eTLD+1) via a repo-pinned Public Suffix List snapshot.

The list is never fetched at runtime. Update with `uv run seo psl-update` (writes
app/normalize/data/public_suffix_list.dat; review the diff and commit).
Algorithm: https://github.com/publicsuffix/list/wiki/Format (rules, wildcards, exceptions;
unlisted TLD → implicit "*" rule).
"""

import pathlib
from functools import lru_cache

PSL_PATH = pathlib.Path(__file__).parent / "data" / "public_suffix_list.dat"
PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"


@lru_cache(maxsize=1)
def _rules() -> tuple[frozenset, frozenset, frozenset]:
    normal, wildcard, exception = set(), set(), set()
    for raw in PSL_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip().split()[0] if raw.strip() else ""
        if not line or line.startswith("//"):
            continue
        line = line.encode("idna").decode("ascii") if not line.isascii() and not line.startswith(("*", "!")) else line
        if line.startswith("!"):
            exception.add(line[1:])
        elif line.startswith("*."):
            wildcard.add(line[2:])
        else:
            normal.add(line)
    return frozenset(normal), frozenset(wildcard), frozenset(exception)


def to_ascii_host(host: str) -> str | None:
    host = host.strip().lower().rstrip(".")
    if not host or ".." in host:
        return None
    try:
        return ".".join(label.encode("idna").decode("ascii") if not label.isascii() else label for label in host.split("."))
    except UnicodeError:
        return None


def public_suffix(host: str) -> str | None:
    h = to_ascii_host(host)
    if not h:
        return None
    normal, wildcard, exception = _rules()
    labels = h.split(".")
    best = labels[-1]  # implicit "*" rule
    for i in range(len(labels)):
        cand = ".".join(labels[i:])
        if cand in exception:
            return ".".join(labels[i + 1:])
        parent = ".".join(labels[i + 1:])
        if cand in normal or (parent and parent in wildcard):
            return cand
    return best


def registrable_domain(host: str) -> str | None:
    """eTLD+1, e.g. shop.example.com.tr → example.com.tr. None for IPs, bare suffixes or invalid hosts."""
    h = to_ascii_host(host)
    if not h or h.replace(".", "").isdigit() or ":" in h:
        return None
    suffix = public_suffix(h)
    if suffix is None or h == suffix:
        return None
    rest = h[: -len(suffix) - 1]
    return f"{rest.split('.')[-1]}.{suffix}"


def update_psl(timeout: float = 30.0) -> dict:
    import httpx
    r = httpx.get(PSL_URL, timeout=timeout, follow_redirects=False)
    r.raise_for_status()
    body = r.text
    if "===BEGIN ICANN DOMAINS===" not in body or len(body) < 100_000:
        raise ValueError("downloaded PSL looks invalid; not replacing snapshot")
    PSL_PATH.write_text(body, encoding="utf-8")
    _rules.cache_clear()
    return {"bytes": len(body), "path": str(PSL_PATH)}

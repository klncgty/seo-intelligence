"""Web-scale crawler frontier, Bloom filter deduplication, crawl-trap detector, and optional JS renderer (§9).

- ``BloomFilter``: memory-bounded probabilistic set (1M URLs at 1% FP uses ~1.2 MB).
- ``detect_crawl_trap(url)``: blocks parameter explosion, session IDs, infinite calendars, and repeating path loops.
- ``DistributedFrontier``: per-host priority queues with link-based priority, per-host budget, and recrawl scheduling.
- ``RendererClient``: isolated Playwright renderer client (flag-gated, verified-domains only, SSRF-checked).
"""

from __future__ import annotations

import hashlib
import heapq
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.services.crawler.ssrf import check_url

_SESSION_KEYS = {"phpsessid", "jsessionid", "aspsessionid", "cfid", "cftoken", "sessionid", "session_id", "sid"}
_CALENDAR_RE = re.compile(r"/(19[0-8]\d|20[3-9]\d|2[1-9]\d\d)/(0?[1-9]|1[0-2])(?:/|$)")


class BloomFilter:
    """Deterministic double-hashing Bloom filter backed by a bytearray."""

    def __init__(self, capacity: int = 1_000_000, error_rate: float = 0.01):
        capacity = max(100, capacity)
        error_rate = min(max(error_rate, 1e-6), 0.5)
        self.num_bits = max(64, int(-capacity * math.log(error_rate) / (math.log(2) ** 2)))
        self.num_hashes = max(2, int((self.num_bits / capacity) * math.log(2)))
        self._bytes = bytearray((self.num_bits + 7) // 8)
        self.count = 0

    def _positions(self, item: str):
        digest = hashlib.sha256(item.encode("utf-8", "replace")).digest()
        h1 = int.from_bytes(digest[:8], "big")
        h2 = int.from_bytes(digest[8:16], "big") or 1
        for i in range(self.num_hashes):
            yield (h1 + i * h2) % self.num_bits

    def add(self, item: str) -> bool:
        """Add ``item``; returns True if it was definitely not present before, False if already seen."""
        already = True
        for pos in self._positions(item):
            byte_idx, bit_mask = divmod(pos, 8)
            mask = 1 << bit_mask
            if not (self._bytes[byte_idx] & mask):
                already = False
                self._bytes[byte_idx] |= mask
        if not already:
            self.count += 1
        return not already

    def __contains__(self, item: str) -> bool:
        for pos in self._positions(item):
            byte_idx, bit_mask = divmod(pos, 8)
            if not (self._bytes[byte_idx] & (1 << bit_mask)):
                return False
        return True

    @property
    def memory_bytes(self) -> int:
        return len(self._bytes)


def detect_crawl_trap(url: str, max_params: int = 6, max_query_len: int = 240, max_depth_segments: int = 12) -> str | None:
    """Return a machine-readable trap reason if ``url`` matches a known crawl trap, else None."""
    p = urlsplit(url)
    if len(p.query) > max_query_len:
        return "query_too_long"
    if p.query:
        pairs = parse_qsl(p.query, keep_blank_values=True)
        if len(pairs) > max_params:
            return "parameter_explosion"
        key_counts = Counter(k.lower() for k, _ in pairs)
        if any(k in _SESSION_KEYS for k in key_counts):
            return "session_id_param"
        if any(c > 2 for c in key_counts.values()):
            return "repeated_query_param"
    path_lower = p.path.lower()
    if any(f";{s}=" in path_lower for s in _SESSION_KEYS):
        return "session_id_path"
    if _CALENDAR_RE.search(path_lower):
        return "infinite_calendar"
    segs = [s for s in path_lower.split("/") if s]
    if len(segs) > max_depth_segments:
        return "excessive_path_depth"
    seg_counts = Counter(segs)
    if any(c >= 3 for c in seg_counts.values()):
        return "repeating_path_segment"
    return None


def next_recrawl_interval_s(*, changed: bool, status_code: int | None, is_important: bool = False) -> int:
    """Adaptive recrawl schedule based on observed change frequency and page importance."""
    if status_code and status_code >= 500:
        return 3600  # retry server errors in 1h
    if status_code in (404, 410):
        return 7 * 86400
    base = 86400 if changed else 7 * 86400
    if is_important:
        base = max(3600, base // 4)
    return base


@dataclass(order=True)
class FrontierItem:
    priority: float
    depth: int
    seq: int
    url: str


class DistributedFrontier:
    """Per-host priority queues with Bloom deduplication, crawl-trap filtering, and per-host budgets."""

    def __init__(self, max_per_host: int = 5000, bloom_capacity: int = 100_000):
        self.max_per_host = max_per_host
        self.bloom = BloomFilter(capacity=bloom_capacity)
        self._queues: dict[str, list[FrontierItem]] = defaultdict(list)
        self._host_Popped: dict[str, int] = defaultdict(int)
        self._inlinks: dict[str, int] = defaultdict(int)
        self._seq = 0
        self.traps_skipped: dict[str, str] = {}

    def push(self, url: str, depth: int = 0, *, inlink_boost: int = 0, is_seed: bool = False) -> bool:
        trap = detect_crawl_trap(url)
        if trap is not None:
            self.traps_skipped[url] = trap
            return False
        self._inlinks[url] += max(0, inlink_boost)
        if not self.bloom.add(url):
            return False
        host = (urlsplit(url).hostname or "").lower()
        if self._host_Popped[host] + len(self._queues[host]) >= self.max_per_host:
            return False
        self._seq += 1
        # Lower priority value = popped earlier (seeds first, shallow depth, high inlinks)
        prio = (0.0 if is_seed else float(depth)) - min(5.0, self._inlinks[url] * 0.25)
        heapq.heappush(self._queues[host], FrontierItem(prio, depth, self._seq, url))
        return True

    def pop_for_host(self, host: str) -> tuple[str, int] | None:
        q = self._queues.get(host.lower())
        if not q:
            return None
        if self._host_Popped[host.lower()] >= self.max_per_host:
            q.clear()
            return None
        item = heapq.heappop(q)
        self._host_Popped[host.lower()] += 1
        return item.url, item.depth

    def pop_any(self) -> tuple[str, int] | None:
        # Round-robin pick the host with the fewest popped pages so no single host monopolizes discovery
        candidates = [h for h, q in self._queues.items() if q and self._host_Popped[h] < self.max_per_host]
        if not candidates:
            return None
        host = min(candidates, key=lambda h: (self._host_Popped[h], self._queues[h][0].priority))
        return self.pop_for_host(host)

    def __len__(self) -> int:
        return sum(len(q) for q in self._queues.values())


class RendererClient:
    """Optional isolated Playwright renderer client (spec §9).

    Only enabled when ``renderer_url`` is configured AND the target domain is verified.
    Every target URL is validated against the SSRF policy before dispatching to the renderer.
    """

    def __init__(self, renderer_url: str | None = None, timeout_s: float = 20.0):
        from app.core.config import get_settings
        s = get_settings()
        self.renderer_url = (renderer_url if renderer_url is not None else getattr(s, "renderer_url", "")).rstrip("/")
        self.timeout_s = timeout_s

    @property
    def configured(self) -> bool:
        return bool(self.renderer_url)

    async def render(self, url: str, *, allowed_hosts: list[str], domain_verified: bool,
                     allow_private: bool = False, transport: httpx.AsyncBaseTransport | None = None) -> dict:
        if not domain_verified:
            raise PermissionError("JS rendering is disabled for unverified domains")
        if not self.configured:
            return {"rendered": False, "reason": "renderer_not_configured"}
        # Enforce SSRF policy on the target URL before handing it to the isolated renderer
        check_url(url, allowed_hosts, allow_private=allow_private)
        async with httpx.AsyncClient(transport=transport, timeout=self.timeout_s, trust_env=False) as client:
            resp = await client.post(
                f"{self.renderer_url}/render",
                json={
                    "url": url,
                    "allowed_hosts": allowed_hosts,
                    "block_downloads": True,
                    "block_service_workers": True,
                    "block_file_scheme": True,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return {"rendered": True, "html": data.get("html", ""), "final_url": data.get("final_url", url),
                    "subrequests_blocked": data.get("subrequests_blocked", 0)}

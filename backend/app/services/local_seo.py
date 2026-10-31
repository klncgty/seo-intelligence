"""Local SEO (spec §14, optional). GBP API needs Google approval → reported as setup pending. NAP consistency checks
only the citation URLs the tenant provides, through the SSRF-safe fetcher (robots respected)."""

import re

from app.core.config import get_settings
from app.normalize.text import match_key
from app.services.crawler.fetcher import FetchPolicy, build_client, safe_get
from app.services.crawler.urls import host_of

GBP_STATUS = {"available": False, "status": "setup_pending",
              "setup": "Request Google Business Profile API access for your Google Cloud project, then connect the account "
                       "that manages your own listings. Only your own listings are accessed."}


def _digits(s: str) -> str:
    d = re.sub(r"\D", "", s or "")
    return d[-10:] if len(d) >= 10 else d


async def nap_check(name: str, address: str, phone: str, urls: list[str], transport=None) -> dict:
    s = get_settings()
    want_name, want_phone = match_key(name), _digits(phone)
    addr_tokens = [t for t in match_key(address).split() if len(t) > 2]
    results = []
    for u in urls[:50]:
        policy = FetchPolicy(allowed_hosts=[host_of(u)], user_agent=s.crawler_user_agent, timeout_s=15, max_bytes=2_000_000,
                             allow_private=s.crawler_allow_private_networks)
        async with build_client(policy, transport) as c:
            res = await safe_get(c, u, policy)
        if res.status_code != 200 or not res.body:
            results.append({"url": u, "status": "unreachable", "detail": res.error or res.status_code})
            continue
        text = match_key(res.body.decode("utf-8", "replace"))
        digits = re.sub(r"\D", "", text)
        addr_hit = sum(t in text for t in addr_tokens) / len(addr_tokens) if addr_tokens else None
        found = {"name": want_name in text, "phone": bool(want_phone) and want_phone in digits,
                 "address_token_match": round(addr_hit, 2) if addr_hit is not None else None}
        ok = found["name"] and found["phone"] and (addr_hit or 0) >= 0.7
        results.append({"url": u, "status": "consistent" if ok else "inconsistent", "found": found})
    return {"checked": len(results), "consistent": sum(r["status"] == "consistent" for r in results), "results": results,
            "source_class": "OBSERVED"}

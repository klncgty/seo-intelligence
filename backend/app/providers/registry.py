"""Provider registry: which provider serves which capability, with status/cost/limits metadata.

Priority/fallback order per capability is explicit; data from different providers is never silently
blended — every canonical record keeps its provider in lineage. Mock provider is only returned for
DEMO organizations or when explicitly injected in tests.
"""

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Organization
from app.providers.base import CAPABILITIES, ProviderNotConfigured

_OVERRIDES: dict[str, object] = {}  # tests inject providers here

SETUP = {
    "dataforseo": "Set SEO_DATA_PROVIDER=dataforseo, DATAFORSEO_LOGIN, DATAFORSEO_PASSWORD. Pay-as-you-go; "
                  "approx. costs: SERP live ~$0.002/req, search volume ~$0.05/1000 kw (verify current pricing).",
    "google_ads": "FREE: Google Ads account + developer token (apply in API Center) + OAuth refresh token with scope "
                  "https://www.googleapis.com/auth/adwords → GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CUSTOMER_ID, "
                  "GOOGLE_ADS_REFRESH_TOKEN. Volumes are rounded; low-spend accounts may get broad ranges.",
}


def _configured_providers() -> list:
    s = get_settings()
    out = []
    if s.dataforseo_login and s.dataforseo_password:  # priority 1 (paid, broadest coverage)
        from app.providers.dataforseo import DataForSEO
        out.append(DataForSEO(s.dataforseo_login, s.dataforseo_password))
    from app.providers.free_sources import GoogleAdsKeywordPlanner
    gads = GoogleAdsKeywordPlanner()
    if gads.is_configured():  # priority 2 (free; volumes rounded)
        out.append(gads)
    return out


def set_override(capability: str, provider) -> None:
    _OVERRIDES[capability] = provider


def clear_overrides() -> None:
    _OVERRIDES.clear()


def get(capability: str, db: Session | None = None, org_id: int | None = None):
    if capability not in CAPABILITIES:
        raise ValueError(f"unknown capability {capability!r}")
    if capability in _OVERRIDES:
        return _OVERRIDES[capability]
    for p in _configured_providers():
        if capability in p.capabilities and p.is_configured():
            return p
    if db is not None and org_id is not None:
        org = db.get(Organization, org_id)
        if org is not None and org.is_demo:
            from app.providers.mock import MockProvider
            p = MockProvider()
            if capability in p.capabilities:
                return p
    raise ProviderNotConfigured(f"no provider configured for {capability}. {SETUP['dataforseo']}")


def status() -> list[dict]:
    configured = {p.name: p for p in _configured_providers()}
    rows = []
    for name, setup in SETUP.items():
        p = configured.get(name)
        rows.append({"provider": name, "configured": p is not None,
                     "capabilities": list(p.capabilities) if p else [],
                     "unit_cost_usd": p.unit_cost_usd if p else None,
                     "rate_per_s": p.rate_per_s if p else None,
                     "supported_databases": list(p.supported_databases) if p else [],
                     "setup": None if p else setup})
    return rows

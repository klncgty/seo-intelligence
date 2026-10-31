"""Select an explicitly configured provider, or report its absence."""
from __future__ import annotations

from app.services.competitors.providers.base import SeoDataProvider
from app.services.competitors.providers.dataforseo import DataForSEOProvider
from app.services.competitors.providers.unavailable import UnavailableProvider


def get_provider(
    name: str | None, login: str, password: str
) -> SeoDataProvider:
    provider_name = (name or "").strip().lower().replace("-", "").replace("_", "")
    if provider_name != "dataforseo":
        hint = "Select the supported 'dataforseo' provider and configure its credentials."
        return UnavailableProvider(hint)
    if not login.strip() or not password.strip():
        return UnavailableProvider(
            "Set both DataForSEO login and password before requesting external SEO data."
        )
    return DataForSEOProvider(login=login, password=password)

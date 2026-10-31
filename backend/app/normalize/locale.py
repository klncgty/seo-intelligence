"""Locale ("database") model: country + language + engine + device + optional location.

Every keyword / SERP record is tied to a database id like ``tr-TR:google.com.tr``.
TR is first-class; more locales are added to LOCALES.
"""

from dataclasses import dataclass

DEVICES = ("desktop", "mobile")


@dataclass(frozen=True)
class Locale:
    country: str  # ISO 3166-1 alpha-2
    language: str  # ISO 639-1
    engine: str  # search engine domain
    currency: str
    timezone: str

    @property
    def database(self) -> str:
        return f"{self.language}-{self.country}:{self.engine}"


LOCALES = {loc.database: loc for loc in [
    Locale("TR", "tr", "google.com.tr", "TRY", "Europe/Istanbul"),
    Locale("US", "en", "google.com", "USD", "America/New_York"),
    Locale("GB", "en", "google.co.uk", "GBP", "Europe/London"),
    Locale("DE", "de", "google.de", "EUR", "Europe/Berlin"),
]}
SHORT_ALIASES = {f"{loc.language}-{loc.country}": loc.database for loc in LOCALES.values()}
DEFAULT_DATABASE = "tr-TR:google.com.tr"


def resolve_database(database: str) -> str:
    return SHORT_ALIASES.get(database, database)


@dataclass(frozen=True)
class SearchContext:
    """Full SERP context: locale + device + optional location (city name or 'lat,lng')."""
    database: str
    device: str = "desktop"
    location: str = ""

    def __post_init__(self):
        resolved = resolve_database(self.database)
        if resolved != self.database:
            object.__setattr__(self, "database", resolved)
        if self.database not in LOCALES:
            raise ValueError(f"unsupported database {self.database!r}; supported: {', '.join(LOCALES)}")
        if self.device not in DEVICES:
            raise ValueError(f"device must be one of {DEVICES}")
        if len(self.location) > 200:
            raise ValueError("location too long")

    @property
    def locale(self) -> Locale:
        return LOCALES[self.database]


def get_locale(database: str) -> Locale:
    resolved = resolve_database(database)
    try:
        return LOCALES[resolved]
    except KeyError as exc:
        raise ValueError(f"unsupported database {database!r}") from exc

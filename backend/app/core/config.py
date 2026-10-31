from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    app_version: str = "0.2.0"
    # LOCAL_SINGLE_USER (default, 127.0.0.1 only) or SERVER (auth mandatory)
    app_mode: str = "LOCAL_SINGLE_USER"
    # Analytics profile (optional; features degrade to "not available" when unset/unreachable)
    clickhouse_url: str = ""  # e.g. http://127.0.0.1:8123
    clickhouse_database: str = "seo"
    clickhouse_user: str = "default"
    clickhouse_password: str = ""
    redis_url: str = ""  # e.g. redis://127.0.0.1:6379/0
    minio_endpoint: str = ""  # e.g. 127.0.0.1:9000
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "seo-intelligence"
    minio_secure: bool = False
    database_url: str = "postgresql+psycopg://seo:seo@127.0.0.1:5432/seo_intelligence"
    # Fernet key (urlsafe base64, 32 bytes) used to encrypt Google refresh tokens.
    token_encryption_key: str = ""
    token_encryption_keys: str = ""  # comma-separated, first = primary (versioned keys / rotation)
    public_api_url: str = "http://127.0.0.1:8000"
    operator_redirect_url: str = "http://127.0.0.1:8000/api/v1/integrations/google/status"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5173"])

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/api/v1/integrations/google/callback"

    crawler_user_agent: str = "SEOGrowthIntelligenceBot/0.1 (+private; local crawler)"
    crawler_max_pages_limit: int = 5000
    crawler_max_depth_limit: int = 10
    crawler_max_concurrency_limit: int = 8
    crawler_min_request_delay_ms: int = 250
    crawler_max_response_bytes: int = 5 * 1024 * 1024
    crawler_max_redirects: int = 5
    crawler_default_max_pages: int = 500
    crawler_default_max_depth: int = 5
    crawler_default_concurrency: int = 4
    crawler_default_request_delay_ms: int = 500
    crawler_default_timeout_s: float = 15.0
    # Testing only: allow private/loopback targets (fixture sites). Never enable in real use.
    crawler_allow_private_networks: bool = False

    # Competitor crawls use stricter limits than own-site crawls.
    competitor_max_pages: int = 300
    competitor_min_request_delay_ms: int = 1000
    competitor_max_concurrency: int = 2
    # External SEO data provider (keyword gap / backlinks). Empty → feature reported as unavailable.
    seo_data_provider: str = ""
    dataforseo_login: str = ""
    dataforseo_password: str = ""

    # Global provider spend caps (USD). Calls are refused (and an alert logged) before exceeding them.
    provider_daily_budget_usd: float = 5.0
    provider_monthly_budget_usd: float = 50.0
    provider_timeout_s: float = 60.0
    provider_cache_ttl_hours: int = 24 * 7

    metrics_token: str = ""  # Bearer token for /metrics when scraped from a non-loopback Prometheus
    crawler_store_warc: bool = False  # archive raw responses as WARC records in MinIO (needs MINIO_*)
    renderer_url: str = ""  # Optional isolated Playwright renderer endpoint (http://127.0.0.1:3001)
    egress_proxy_url: str = ""  # Optional SSRF egress proxy (Smokescreen http://127.0.0.1:4750)
    llm_model: str = ""  # Optional LLM assistant model (e.g. claude-sonnet-4-5); disabled when empty
    stripe_secret_key: str = ""  # Feature-flagged Stripe billing (test mode sk_test_* locally)
    stripe_webhook_secret: str = ""
    oidc_issuer_url: str = ""  # Optional OIDC SSO issuer URL
    oidc_client_id: str = ""
    # Google Ads Keyword Planner (free Ads account + approved developer token; OAuth scope adwords)
    google_ads_developer_token: str = ""
    google_ads_customer_id: str = ""  # 10 digits, no dashes
    google_ads_login_customer_id: str = ""  # manager account, optional
    google_ads_refresh_token: str = ""
    google_ads_api_version: str = "v21"  # verify the current version in Google Ads API docs
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    github_token: str = ""
    github_repo: str = ""  # owner/name
    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: str = ""
    jira_project_key: str = ""
    linear_api_key: str = ""
    linear_team_id: str = ""
    crux_api_key: str = ""
    # AI visibility engines (official APIs only; each needs its own key + explicit model id)
    openai_api_key: str = ""
    openai_visibility_model: str = ""
    anthropic_api_key: str = ""
    anthropic_visibility_model: str = "claude-sonnet-5-5"
    gemini_api_key: str = ""
    gemini_visibility_model: str = ""
    perplexity_api_key: str = ""
    perplexity_visibility_model: str = "sonar"  # CrUX + PageSpeed Insights (Google API key)
    unverified_max_pages: int = 500
    unverified_max_depth: int = 5
    url_inspection_daily_quota: int = 2000  # per property; verify against current Google docs

    worker_poll_interval_s: float = 2.0
    worker_stale_after_s: int = 300
    gsc_initial_days: int = 90
    ga4_initial_days: int = 90


@lru_cache
def get_settings() -> Settings:
    return Settings()

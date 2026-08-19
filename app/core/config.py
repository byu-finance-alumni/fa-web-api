"""Application configuration loaded from environment variables.

Settings are read from the process environment and an optional `.env` file
(see `.env.example`). Secrets are never hardcoded and `.env` is gitignored.

Each field also accepts the variable names injected by the official
Supabase<->Vercel integration (e.g. `POSTGRES_URL`, `SUPABASE_JWT_SECRET`), so
the app works whether values are set manually or by the integration.
"""

import datetime
from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    environment: str = "development"
    debug: bool = False
    # SQL statement echo is OFF by default — it floods the terminal. Turn it on
    # only when actively debugging a query (SQL_ECHO=true), independent of DEBUG.
    sql_echo: bool = False

    # Connection-pool sizing for the session-pooler (:5432) path. Supabase's
    # session-mode pooler caps TOTAL clients to 15, so the app pool MUST stay
    # small (default 5 + 2 overflow = hard cap 7) to leave headroom for one-off
    # scripts and migrations. Overridable via env if needed. See database.py.
    db_pool_size: int = 5
    db_max_overflow: int = 2
    db_pool_timeout: int = 10
    db_pool_recycle: int = 1800

    # Database — optional so the app can boot before a DB is provisioned.
    # Prefers DATABASE_URL, then the Supabase/Vercel integration's POSTGRES_URL
    # (pooled) and POSTGRES_URL_NON_POOLING (direct).
    database_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "DATABASE_URL",
            "POSTGRES_URL",
            "POSTGRES_URL_NON_POOLING",
        ),
    )

    # Supabase (auth + storage).
    supabase_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"),
    )
    supabase_anon_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SUPABASE_ANON_KEY", "NEXT_PUBLIC_SUPABASE_ANON_KEY"
        ),
    )
    supabase_service_role_key: str | None = Field(default=None)

    # Auth — accepts the integration's SUPABASE_JWT_SECRET as well.
    jwt_secret: str | None = Field(
        default=None,
        validation_alias=AliasChoices("JWT_SECRET", "SUPABASE_JWT_SECRET"),
    )

    # Survey email (Resend). All optional so the app boots undeployed; the send
    # service raises ServiceError when a required one is missing. The API key
    # lives ONLY here (backend), never in the frontend.
    resend_api_key: str | None = Field(default=None)  # RESEND_API_KEY
    survey_from_email: str | None = Field(default=None)  # e.g. byufinancealumni@mailing.byu.edu
    survey_from_name: str = Field(default="BYU Finance Alumni")
    # Base URL of the frontend, used to build each recipient's /survey/<token> link.
    survey_app_base_url: str | None = Field(default=None)  # e.g. https://finance.alumni.byu.edu
    # HMAC secret that signs survey tokens (any long random string).
    survey_token_secret: str | None = Field(default=None)
    # NOTE: there is deliberately no `survey_daily_cap` here. The send budget is
    # the admin-editable `survey_send_config` row (see
    # `survey_schedule.get_send_config`) plus Resend's own 429; a config-file cap
    # was read by nothing and only misled anyone auditing this file.
    # Manual send-usage baseline (#544). If `survey_usage_baseline_at` is set, the
    # console's daily/monthly tallies START from these counts as of that instant
    # and add ONLY sends recorded AFTER it — a correction for when the audit
    # history is incomplete/polluted (e.g. dev testing). Leave `_at` unset (the
    # default) to report the pure audit-summed usage.
    survey_usage_baseline_at: datetime.datetime | None = Field(default=None)
    survey_usage_baseline_today: int = Field(default=0)
    survey_usage_baseline_month: int = Field(default=0)
    # API failure alerting (#444) — the engineer's pager. Comma-separated
    # recipients; UNSET (the default) DISABLES ALERTING ENTIRELY, which is what
    # keeps local runs, CI and preview deployments silent without a second flag
    # to remember. Sending reuses the survey's Resend account (RESEND_API_KEY),
    # so alerting is also off wherever mail is off.
    alert_email_to: str | None = Field(default=None)  # ALERT_EMAIL_TO
    # From-address for alerts. Falls back to SURVEY_FROM_EMAIL when unset — one
    # verified sending domain is all this app has. Kept separate so alerts can be
    # moved to their own address later without touching the survey identity.
    alert_from_email: str | None = Field(default=None)  # ALERT_FROM_EMAIL
    alert_from_name: str = Field(default="BYU Finance Alumni API")
    # Slack incoming-webhook URL alerts are ALSO posted to (#456). Same on/off
    # rule as ALERT_EMAIL_TO above and for the same reason: UNSET DISABLES SLACK
    # ENTIRELY, so local runs, the test suite, CI and preview deployments stay
    # silent without a second flag to remember to turn off. The two channels are
    # independently optional -- email only, Slack only, both, or neither -- so a
    # deployment can page a channel without a mailbox, or the reverse.
    #
    # This is a SECRET: the URL is the entire credential (anyone holding it can
    # post to the channel), so it lives only in backend env vars, is never
    # returned by an endpoint, and is never logged. Nothing in this app renders
    # it; the alerter logs the failure, never the target.
    slack_alert_webhook_url: str | None = Field(default=None)  # SLACK_ALERT_WEBHOOK_URL

    @property
    def slack_webhook(self) -> str | None:
        """The Slack incoming-webhook URL, or None when Slack alerting is off.

        Whitespace-only is treated as unset: Vercel env vars are edited in a web
        form and an accidental space would otherwise read as "configured" and
        turn every alert into a failed HTTP POST.
        """
        raw = (self.slack_alert_webhook_url or "").strip()
        return raw or None

    @property
    def alert_recipients(self) -> list[str]:
        """Parse the comma-separated alert recipients into a clean list."""
        raw = self.alert_email_to or ""
        return [addr.strip() for addr in raw.split(",") if addr.strip()]

    @property
    def alert_sender(self) -> str | None:
        """The From address alerts go out as (falls back to the survey sender)."""
        return self.alert_from_email or self.survey_from_email

    # Shared secret protecting the survey send-scheduler cron endpoint
    # (POST /survey/cron/run). Vercel Cron sends `Authorization: Bearer
    # $CRON_SECRET` automatically when CRON_SECRET is set as a project env var;
    # the route accepts the call only when the header matches. Unset (None) ->
    # the endpoint rejects every request (401), so it's never open by default.
    cron_secret: str | None = Field(default=None)  # CRON_SECRET

    # CORS — comma-separated list of allowed frontend origins.
    cors_origins: str = Field(
        default=(
            "http://localhost:3000,"
            "https://finance.alumni.byu.edu,"
            "https://finance-alumni-database.vercel.app,"
            "https://dev-fa-web-app.vercel.app"
        ),
        validation_alias=AliasChoices("CORS_ORIGINS", "CORS_ORIGIN"),
    )

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse the comma-separated CORS origins into a clean list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def async_database_url(self) -> str | None:
        """Return the DATABASE_URL normalized to the asyncpg driver.

        Supabase/Postgres connection strings come as ``postgresql://`` (or the
        legacy ``postgres://``); SQLAlchemy's async engine needs the
        ``postgresql+asyncpg://`` driver prefix. Query params (e.g.
        ``?sslmode=require``) are stripped because asyncpg rejects libpq-style
        params passed as kwargs — SSL is negotiated automatically.
        """
        url = self.database_url
        if not url:
            return None
        url = url.split("?", 1)[0]
        if url.startswith("postgresql+asyncpg://"):
            return url
        if url.startswith("postgresql://"):
            return url.replace("postgresql://", "postgresql+asyncpg://", 1)
        if url.startswith("postgres://"):
            return url.replace("postgres://", "postgresql+asyncpg://", 1)
        return url


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance."""
    return Settings()

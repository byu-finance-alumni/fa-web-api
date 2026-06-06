"""Application configuration loaded from environment variables.

Settings are read from the process environment and an optional `.env` file
(see `.env.example`). Secrets are never hardcoded and `.env` is gitignored.

Each field also accepts the variable names injected by the official
Supabase<->Vercel integration (e.g. `POSTGRES_URL`, `SUPABASE_JWT_SECRET`), so
the app works whether values are set manually or by the integration.
"""

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

    # CORS — comma-separated list of allowed frontend origins.
    cors_origins: str = Field(
        default=(
            "http://localhost:3000,"
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

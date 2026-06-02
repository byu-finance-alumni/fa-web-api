"""Application configuration loaded from environment variables.

Settings are read from the process environment and an optional `.env` file
(see `.env.example`). Secrets are never hardcoded and `.env` is gitignored.
"""

from functools import lru_cache

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

    # Database — optional so the app can boot before a DB is provisioned.
    database_url: str | None = None

    # Supabase (auth + storage) — wired up in later phases.
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    supabase_service_role_key: str | None = None

    # Auth
    jwt_secret: str | None = None

    @property
    def async_database_url(self) -> str | None:
        """Return the DATABASE_URL normalized to the asyncpg driver.

        Supabase/Postgres connection strings come as ``postgresql://`` (or the
        legacy ``postgres://``); SQLAlchemy's async engine needs the
        ``postgresql+asyncpg://`` driver prefix.
        """
        url = self.database_url
        if not url:
            return None
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

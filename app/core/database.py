"""Database engine, session factory, and connectivity check.

Uses SQLAlchemy 2.x async (asyncpg). The engine is created only when a
DATABASE_URL is configured, so the API can still start without a database
(the DB health endpoint will report it as unavailable).
"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    """Declarative base for ORM models (defined under app/models)."""


engine: AsyncEngine | None = None
SessionLocal: async_sessionmaker[AsyncSession] | None = None

if settings.async_database_url:
    _url = settings.async_database_url
    # Supabase pooler ports: 5432 = session pooler, 6543 = transaction pooler.
    # The transaction pooler (pgbouncer) is what serverless platforms like
    # Vercel should use. In that mode we must disable asyncpg's prepared-
    # statement cache and not hold a connection pool across invocations.
    _is_transaction_pooler = ":6543" in _url

    if _is_transaction_pooler:
        engine = create_async_engine(
            _url,
            echo=settings.sql_echo,
            poolclass=NullPool,
            connect_args={"statement_cache_size": 0},
        )
    else:
        engine = create_async_engine(
            _url,
            echo=settings.sql_echo,
            pool_pre_ping=True,
        )

    SessionLocal = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a database session.

    Raises RuntimeError if the database is not configured.
    """
    if SessionLocal is None:
        raise RuntimeError("DATABASE_URL is not configured.")
    async with SessionLocal() as session:
        yield session


async def check_database_connection() -> bool:
    """Return True if a trivial query succeeds against the database.

    Raises RuntimeError if no database is configured. Any driver/connection
    error is allowed to propagate so the caller can log it server-side.
    """
    if engine is None:
        raise RuntimeError("DATABASE_URL is not configured.")
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return True


async def dispose_engine() -> None:
    """Dispose the engine's connection pool on application shutdown."""
    if engine is not None:
        await engine.dispose()

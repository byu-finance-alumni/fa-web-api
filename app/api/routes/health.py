"""Health and diagnostic endpoints.

These endpoints are intentionally public (no auth) so that load balancers and
uptime checks can reach them. They expose no alumni data.
"""

import logging

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse

from app import __version__
from app.core.config import get_settings
from app.core.database import check_database_connection, engine
from app.schemas.health import DBHealthResponse, ErrorResponse, HealthResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])
settings = get_settings()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness check — confirms the API process is up."""
    return HealthResponse(
        status="ok",
        environment=settings.environment,
        version=__version__,
    )


@router.get(
    "/health/db",
    response_model=DBHealthResponse,
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ErrorResponse}},
)
async def health_db() -> JSONResponse:
    """Readiness check — verifies a live database connection (SELECT 1)."""
    if engine is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "database_not_configured",
                    "message": "DATABASE_URL is not set.",
                }
            },
        )

    try:
        await check_database_connection()
    except Exception:
        # Log the real error server-side; never expose SQL/stack traces.
        logger.exception("Database health check failed")
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "database_unavailable",
                    "message": "Could not connect to the database.",
                }
            },
        )

    return JSONResponse(content={"status": "ok", "database": "connected"})

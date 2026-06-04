"""FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.api.routes import (
    admin,
    alumni,
    audit,
    auth,
    dashboard,
    events,
    health,
)
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.errors import ConflictError, NotFoundError
from app.core.security import AuthError, AuthorizationError

logging.basicConfig(level=logging.INFO)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logging.getLogger(__name__).info(
        "Starting fa-web-api (env=%s, version=%s)",
        settings.environment,
        __version__,
    )
    yield
    # Shutdown — release database connections.
    await dispose_engine()


app = FastAPI(
    title="BYU Finance Alumni Database API",
    description="Backend API and database layer for the BYU Finance Alumni Database.",
    version=__version__,
    debug=settings.debug,
    lifespan=lifespan,
)

# Allow the configured frontend origins to call the API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(auth.router)
app.include_router(alumni.router)
app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(events.router)
app.include_router(audit.router)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    """Return 401 with the project error envelope for auth failures."""
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": {"code": "unauthorized", "message": exc.message}},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(AuthorizationError)
async def authorization_error_handler(
    request: Request, exc: AuthorizationError
) -> JSONResponse:
    """Return 403 with the project error envelope for permission failures."""
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"error": {"code": "forbidden", "message": exc.message}},
    )


@app.exception_handler(NotFoundError)
async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
    """Return 404 with the project error envelope."""
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={"error": {"code": "not_found", "message": exc.message}},
    )


@app.exception_handler(ConflictError)
async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
    """Return 409 with the project error envelope."""
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"error": {"code": "conflict", "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return 422 with the project error envelope.

    The generic message avoids echoing request input (which may contain PII).
    """
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed.",
            }
        },
    )


@app.get("/", tags=["root"])
async def root() -> dict[str, str]:
    """Root endpoint — basic service identification."""
    return {
        "service": "fa-web-api",
        "status": "ok",
        "docs": "/docs",
    }

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
    geography,
    health,
    tasks,
)
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.errors import ConflictError, NotFoundError, ServiceError
from app.core.security import (
    AuthError,
    AuthorizationError,
    DeactivatedAccountError,
    MustChangePasswordError,
)
from app.core.security_log import log_security_event

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


# Security: never expose interactive docs / the OpenAPI schema or FastAPI's
# debug tracebacks in production — the schema is a recon map of the full API.
_is_prod = settings.environment == "production"

app = FastAPI(
    title="BYU Finance Alumni Database API",
    description="Backend API and database layer for the BYU Finance Alumni Database.",
    version=__version__,
    debug=settings.debug and not _is_prod,
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
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
app.include_router(geography.router)
app.include_router(tasks.router)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    """Return 401 with the project error envelope for auth failures."""
    # Bad / missing / expired / forged token — a probe or a stale session.
    log_security_event(
        request, "auth_failed", status_code=401, detail=exc.message
    )
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": {"code": "unauthorized", "message": exc.message}},
        headers={"WWW-Authenticate": "Bearer"},
    )


@app.exception_handler(DeactivatedAccountError)
async def deactivated_account_handler(
    request: Request, exc: DeactivatedAccountError
) -> JSONResponse:
    """Return 403 for a deactivated account attempting an authenticated request.

    Logged as its own ``account_deactivated`` security event: a valid token whose
    user has been deactivated is high signal (an offboarded / suspended account
    still trying to act).
    """
    log_security_event(
        request, "account_deactivated", status_code=403, detail=exc.message
    )
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"error": {"code": "forbidden", "message": exc.message}},
    )


@app.exception_handler(MustChangePasswordError)
async def must_change_password_handler(
    request: Request, exc: MustChangePasswordError
) -> JSONResponse:
    """Return 403 / ``password_change_required`` for a user who still holds the
    force-password-change flag.

    Registered ahead of (and distinctly from) the generic AuthorizationError
    handler so the subclass is dispatched here: a user on an admin-issued temp
    password is blocked on EVERY authenticated route except the two that let them
    complete the change, and the block is recorded as its own
    ``password_change_required`` security event.
    """
    log_security_event(
        request, "password_change_required", status_code=403, detail=exc.message
    )
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "error": {
                "code": "password_change_required",
                "message": exc.message,
            }
        },
    )


@app.exception_handler(AuthorizationError)
async def authorization_error_handler(
    request: Request, exc: AuthorizationError
) -> JSONResponse:
    """Return 403 with the project error envelope for permission failures."""
    # Distinguish a valid-but-unprovisioned token (someone with a Supabase login
    # who was never granted access — high signal under deny-all RLS) from a real
    # user exceeding their role.
    event = (
        "not_provisioned"
        if "provision" in exc.message.lower()
        else "forbidden"
    )
    log_security_event(request, event, status_code=403, detail=exc.message)
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


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError) -> JSONResponse:
    """Return 502 for an upstream/dependency failure (e.g. Supabase Admin API).

    Logged as a security event (the detail is our own generic message, never the
    upstream body) so repeated failures are observable, and surfaced to the
    client with the generic envelope only.
    """
    log_security_event(
        request, "upstream_service_error", status_code=502, detail=exc.message
    )
    return JSONResponse(
        status_code=status.HTTP_502_BAD_GATEWAY,
        content={"error": {"code": "service_unavailable", "message": exc.message}},
    )


def _field_path(loc: tuple) -> str:
    """Render a Pydantic error ``loc`` as a dotted field path.

    Drops the leading request-section marker (``body`` / ``query`` / ``path``)
    so the client sees ``first_name`` rather than ``body.first_name``. Never
    includes the offending value, so no request input (possible PII) leaks.
    """
    parts = [str(p) for p in loc if p not in ("body", "query", "path")]
    return ".".join(parts) if parts else "__root__"


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return 422 with the project error envelope plus per-field details.

    The top-level message stays generic; the ``fields`` array names each
    failing field and its message so the UI can surface inline errors. We echo
    only the field path and our own validation message — never the submitted
    value (which may contain PII).
    """
    fields = [
        {"field": _field_path(err.get("loc", ())), "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request validation failed.",
                "fields": fields,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all: log the failure and return the generic error envelope.

    Never leaks the exception detail to the client (no stack traces / internals).
    The security event records only the exception *type*; the full traceback goes
    to the server logs for debugging.
    """
    log_security_event(
        request,
        "unhandled_error",
        status_code=500,
        detail=type(exc).__name__,
        level=logging.ERROR,
    )
    logging.getLogger(__name__).exception(
        "Unhandled error on %s %s", request.method, request.url.path
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": {
                "code": "internal_error",
                "message": "An unexpected error occurred.",
            }
        },
    )


@app.get("/", tags=["root"])
async def root() -> dict[str, str]:
    """Root endpoint — basic service identification."""
    payload = {
        "service": "fa-web-api",
        "status": "ok",
    }
    if not _is_prod:
        payload["docs"] = "/docs"
    return payload

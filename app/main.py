"""FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api.routes import (
    admin,
    alumni,
    audit,
    auth,
    dashboard,
    donations,
    engineer,
    events,
    geography,
    health,
    maintenance,
    notes,
    support,
    survey,
    tasks,
    vocabulary,
)
from app.core.config import get_settings
from app.core.database import dispose_engine
from app.core.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    ServiceError,
)
from app.core.security import (
    AuthError,
    AuthorizationError,
    DeactivatedAccountError,
    MaintenanceModeError,
    MustChangePasswordError,
    SessionSupersededError,
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

# Brand favicon (gear mark) served from the package; distinguishes the API tab
# from the web app's network mark. Loaded once at import so the route is a cheap
# in-memory send rather than a per-request disk read.
_FAVICON_PATH = Path(__file__).resolve().parent / "static" / "favicon.svg"
_FAVICON_SVG = _FAVICON_PATH.read_bytes()
_FAVICON_URL = "/favicon.svg"

app = FastAPI(
    title="BYU Finance Alumni Database API",
    description="Backend API and database layer for the BYU Finance Alumni Database.",
    version=__version__,
    debug=settings.debug and not _is_prod,
    # Disable the built-in docs routes so we can re-register them below with our
    # own favicon; the OpenAPI schema route stays on FastAPI's default.
    docs_url=None,
    redoc_url=None,
    openapi_url=None if _is_prod else "/openapi.json",
    lifespan=lifespan,
)


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg() -> Response:
    """Serve the brand gear favicon (SVG, crisp at every size)."""
    return Response(
        content=_FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> FileResponse:
    """Legacy ``/favicon.ico`` path — browsers that request it get the SVG.

    No binary .ico is shipped; modern browsers honor the SVG served with an
    image media type. Generate a real multi-size .ico later if legacy IE/older
    Safari support is ever needed (see the favicon notes).
    """
    return FileResponse(_FAVICON_PATH, media_type="image/svg+xml")


# Custom docs routes (only when docs are exposed, i.e. non-prod) so Swagger UI
# and ReDoc show the brand favicon instead of FastAPI's default.
if not _is_prod:

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui_html() -> Response:
        return get_swagger_ui_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} — Swagger UI",
            swagger_favicon_url=_FAVICON_URL,
        )

    @app.get("/redoc", include_in_schema=False)
    async def redoc_html() -> Response:
        return get_redoc_html(
            openapi_url=app.openapi_url,
            title=f"{app.title} — ReDoc",
            redoc_favicon_url=_FAVICON_URL,
        )

# Allow the configured frontend origins to call the API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Set baseline security response headers on every response (#176).

    - ``X-Content-Type-Options: nosniff`` — stop MIME sniffing.
    - ``X-Frame-Options: DENY`` — the API is never meant to be framed
      (clickjacking defense on the JSON/docs surfaces).
    - ``Referrer-Policy: no-referrer`` — never leak the request URL (which may
      carry ids/tokens) in an outbound Referer header.

    Runs outside the CORS middleware so it does not interfere with CORS
    negotiation; ``setdefault`` avoids clobbering any header a specific route
    already set.
    """
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


app.include_router(health.router)
app.include_router(maintenance.router)
app.include_router(auth.router)
app.include_router(alumni.router)
app.include_router(dashboard.router)
app.include_router(admin.router)
app.include_router(engineer.router)
app.include_router(engineer.admin_router)
app.include_router(events.router)
app.include_router(donations.router)
app.include_router(audit.router)
app.include_router(geography.router)
app.include_router(notes.router)
app.include_router(tasks.router)
app.include_router(vocabulary.router)
app.include_router(vocabulary.admin_router)
app.include_router(support.router)
app.include_router(support.admin_router)
app.include_router(survey.router)


@app.exception_handler(MaintenanceModeError)
async def maintenance_mode_handler(
    request: Request, exc: MaintenanceModeError
) -> JSONResponse:
    """Return 503 / ``maintenance_mode`` while the site-wide pause is on.

    503 rather than 401/403 on purpose: the caller's credentials are valid and
    their permissions are fine — the site is closed. The frontend keys on this
    code to show the maintenance page instead of signing the user out or
    rendering a permissions error. ``Retry-After`` tells well-behaved clients and
    crawlers to back off rather than treat it as a permanent failure.

    NOT logged as a security event: a paused user hitting the API is expected
    behaviour during maintenance, not an intrusion signal, and logging every one
    of them would bury the real events.
    """
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {"code": "maintenance_mode", "message": exc.message}},
        headers={"Retry-After": "300"},
    )


@app.exception_handler(SessionSupersededError)
async def session_superseded_handler(
    request: Request, exc: SessionSupersededError
) -> JSONResponse:
    """Return 401 / ``session_superseded`` for a session the account has replaced
    by signing in on another device (#147).

    Registered ahead of the generic AuthError handler so the subclass dispatches
    here with its own machine code — the frontend detects it, signs the device
    out, and tells the user why. Recorded as its own security event."""
    log_security_event(
        request, "session_superseded", status_code=401, detail=exc.message
    )
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"error": {"code": "session_superseded", "message": exc.message}},
        headers={"WWW-Authenticate": "Bearer"},
    )


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


@app.exception_handler(InvalidRequestError)
async def invalid_request_handler(
    request: Request, exc: InvalidRequestError
) -> JSONResponse:
    """Return 422 for a semantically invalid request (client-safe message)."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"error": {"code": "validation_error", "message": exc.message}},
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


# Maps a raw HTTP status to the project's stable ``error.code`` so a stray
# ``HTTPException`` (framework 404s, or a route that raises one directly)
# serializes as the SAME envelope the domain handlers use.
_HTTP_STATUS_ERROR_CODES: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "bad_request",
    status.HTTP_401_UNAUTHORIZED: "unauthorized",
    status.HTTP_403_FORBIDDEN: "forbidden",
    status.HTTP_404_NOT_FOUND: "not_found",
    status.HTTP_405_METHOD_NOT_ALLOWED: "method_not_allowed",
    status.HTTP_409_CONFLICT: "conflict",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "validation_error",
    status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
    status.HTTP_502_BAD_GATEWAY: "service_unavailable",
    status.HTTP_503_SERVICE_UNAVAILABLE: "service_unavailable",
}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Normalize ANY raw ``HTTPException`` into the project error envelope.

    Belt-and-suspenders (#175): the domain-specific exceptions above are the
    intended path, but framework-raised 404/405s and any bare
    ``HTTPException`` a route or dependency raises would otherwise leak
    FastAPI's default ``{"detail": ...}`` shape — or, if ``detail`` is already
    an envelope dict, double-nest it as ``{"detail": {"error": {...}}}``. This
    handler emits ``{"error": {"code": ..., "message": ...}}`` at the top level
    while preserving the status code and any headers (e.g. ``Retry-After`` on a
    429, ``WWW-Authenticate`` on a 401).
    """
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        # A caller already provided the envelope as ``detail`` — pass it through
        # unwrapped rather than nesting it under ``detail``.
        content = detail
    else:
        code = _HTTP_STATUS_ERROR_CODES.get(exc.status_code, "http_error")
        message = detail if isinstance(detail, str) else "Request failed."
        content = {"error": {"code": code, "message": message}}
    return JSONResponse(
        status_code=exc.status_code,
        content=content,
        headers=getattr(exc, "headers", None),
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

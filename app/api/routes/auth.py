"""Authentication routes."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import CurrentDBUser, CurrentUser
from app.core.database import get_session
from app.schemas.auth import AuthenticatedUser, UserContext
from app.services import login_lockout

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# Email is kept a bounded plain string (no email-validator dependency, matching
# the rest of the project): the throttle keys on the lowercased value and never
# trusts it as a verified identity. The length cap bounds the row key size.
_EmailField = Field(min_length=3, max_length=255)


class LoginPrecheckRequest(BaseModel):
    """Email to evaluate the pre-login throttle/lock state for."""

    model_config = ConfigDict(extra="forbid")

    email: str = _EmailField


class LoginRecordRequest(BaseModel):
    """Outcome of a login attempt, to update the rolling failed-login counter."""

    model_config = ConfigDict(extra="forbid")

    email: str = _EmailField
    success: bool


class LoginThrottleStatus(BaseModel):
    """Pre-login throttle status.

    ``reason`` is intentionally coarse and the frontend MUST collapse
    ``cooldown`` and ``locked`` into ONE generic user-facing message
    (anti-enumeration — see app/services/login_lockout.py). ``retry_after_seconds``
    is set for ``cooldown`` only; ``locked`` has no self-clearing timer (a
    super_admin reset is required).
    """

    allowed: bool
    reason: str
    retry_after_seconds: int | None = None


@router.get("/me", response_model=AuthenticatedUser)
async def me(current_user: CurrentUser) -> AuthenticatedUser:
    """Return the authenticated user's verified identity.

    Requires a valid Supabase access token in the `Authorization: Bearer`
    header.
    """
    return current_user


@router.get("/context", response_model=UserContext)
async def context(user: CurrentDBUser) -> UserContext:
    """Return the signed-in user resolved against the database, with roles.

    Used by the frontend for role-aware UI. Returns 403 if the authenticated
    user isn't provisioned (no active `users` row).
    """
    return user


# --- Pre-login throttling / lockout ------------------------------------------
#
# SECURITY: these two endpoints are UNAUTHENTICATED by necessity — they run
# before the user has a session. They are therefore abusable: anyone who knows a
# victim's email can spam `/auth/login/record` with `success=false` to drive a
# registered account into hard lockout (inherent lockout-DoS), or hammer
# `/auth/login/precheck` for probing. This is an accepted tradeoff (a sticky lock
# is the goal); both routes MUST be WAF rate-limited (per-IP) in front of the
# API. They never reveal whether an email is registered — see the anti-
# enumeration note in app/services/login_lockout.py and the single generic
# message the frontend shows for both `cooldown` and `locked`.


@router.post("/login/precheck", response_model=LoginThrottleStatus)
async def login_precheck(
    payload: LoginPrecheckRequest, session: SessionDep
) -> LoginThrottleStatus:
    """Return whether a login for this email may proceed right now (read-only).

    Unauthenticated. The frontend calls this before attempting the Supabase
    sign-in and refuses to attempt it when ``allowed`` is false, collapsing both
    throttle reasons into one generic message.
    """
    status_dict = await login_lockout.check_login(session, payload.email)
    return LoginThrottleStatus(**status_dict)


@router.post("/login/record", response_model=LoginThrottleStatus)
async def login_record(
    payload: LoginRecordRequest, session: SessionDep
) -> LoginThrottleStatus:
    """Record a login attempt outcome and return the resulting throttle status.

    Unauthenticated. Only FAILURES are accumulated here: a failure may trip the
    cooldown and (for a registered email) the hard lock. A ``success=true`` from
    this unauthenticated caller is deliberately IGNORED — it must NOT clear the
    rolling counter, because an attacker could otherwise POST ``{email,
    success:true}`` to wipe a legitimately-set cooldown and brute-force
    unbounded. The genuine success-clear happens on the AUTHENTICATED path
    (``get_current_db_user``), which only a real, signed-in user can reach.

    The ``locked`` flag the service returns is intentionally NOT echoed to the
    client (anti-enumeration); only the coarse ``reason`` is.
    """
    if payload.success:
        # Do NOT clear/delete the login_attempts row for an unauthenticated
        # "success" claim. Report the benign status without mutating state.
        return LoginThrottleStatus(
            allowed=True, reason="ok", retry_after_seconds=None
        )

    status_dict = await login_lockout.record_attempt(
        session, payload.email, success=False
    )
    return LoginThrottleStatus(
        allowed=status_dict["allowed"],
        reason=status_dict["reason"],
        retry_after_seconds=status_dict["retry_after_seconds"],
    )

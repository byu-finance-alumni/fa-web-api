"""Authentication routes."""

import datetime
import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    CurrentDBUserAllowMustChange,
    CurrentUser,
    PermissionConfig,
    _clear_login_attempts,
)
from app.core.capabilities import effective_capabilities
from app.core.database import get_session
from app.core.rate_limit import RecordLoginRateLimit
from app.models.audit import AuditLog
from app.models.login_event import LoginEvent
from app.models.login_failure import LoginFailure
from app.models.user import User
from app.schemas.auth import AuthenticatedUser, UserContext
from app.services import login_lockout

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# Email is kept a bounded plain string (no email-validator dependency, matching
# the rest of the project): the throttle keys on the lowercased value and never
# trusts it as a verified identity. The length cap bounds the row key size.
_EmailField = Field(min_length=3, max_length=255)


class LoginContext(BaseModel):
    """Optional client context for a sign-in attempt, forwarded by the frontend
    login action from the incoming request — the client IP (``x-forwarded-for``)
    and Vercel's IP-geolocation headers. All optional and length-bounded; purely
    informational (never trusted for authorization), stored on the
    ``login_events`` row (success) or ``login_failures`` row (failure) for the
    engineer Logins tabs. ``extra='forbid'`` rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")

    ip_address: str | None = Field(default=None, max_length=64)
    city: str | None = Field(default=None, max_length=128)
    region: str | None = Field(default=None, max_length=128)
    country: str | None = Field(default=None, max_length=64)


class LoginPrecheckRequest(BaseModel):
    """Email to evaluate the pre-login throttle/lock state for."""

    model_config = ConfigDict(extra="forbid")

    email: str = _EmailField


class LoginRecordRequest(BaseModel):
    """Outcome of a login attempt, to update the rolling failed-login counter.

    On a FAILURE the optional ``context`` (client IP + geo) and coarse ``reason``
    are also logged as a per-attempt ``login_failures`` row (the engineer
    Login-failures tab). Both are ignored on success. ``extra='forbid'`` rejects
    unknown keys."""

    model_config = ConfigDict(extra="forbid")

    email: str = _EmailField
    success: bool
    context: LoginContext | None = None
    reason: str | None = Field(default=None, max_length=64)


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

    NOTE (#189): this deliberately uses the token-only resolver, so it does NOT
    reflect single-session validity — a superseded token still gets 200 here. It
    is a pure token-identity echo, and session validity has its own purpose-built
    endpoint (``GET /auth/session/active``) that the frontend polls. If /me is
    ever meant to gate on session validity too, switch it to the session-aware
    resolver (``get_current_db_user``) — a scoped change left out here on purpose,
    since it would also change the response to a DB ``UserContext``.
    """
    return current_user


@router.get("/context", response_model=UserContext)
async def context(
    user: CurrentDBUserAllowMustChange, config: PermissionConfig
) -> UserContext:
    """Return the signed-in user resolved against the database, with roles.

    Used by the frontend for role-aware UI. Returns 403 if the authenticated
    user isn't provisioned (no active `users` row). ``must_change_password``
    reflects the current user's force-change flag. ``capabilities`` carries the
    user's effective capability codes under the live permission config (#164) so
    the UI can show/hide controls — the backend still re-enforces every request.

    EXEMPT from the force-password-change gate: a flagged user must be able to
    read their own context (to learn they're flagged) — so this depends on the
    exempt resolver, not the gated ``get_current_db_user``.
    """
    user.capabilities = sorted(effective_capabilities(config, user.roles))
    return user


def _clean(value: str | None) -> str | None:
    """Trim and collapse an empty/whitespace string to ``None`` (so a missing
    geo header doesn't store as ``""``)."""
    if value is None:
        return None
    value = value.strip()
    return value or None


class LoginRecordedResponse(BaseModel):
    """Acknowledgement that a successful sign-in was recorded, echoing the
    stamped time so a client could display it."""

    status: str = "ok"
    last_login_at: datetime.datetime


@router.post("/login", response_model=LoginRecordedResponse)
async def record_login(
    user: RecordLoginRateLimit,
    session: SessionDep,
    context: LoginContext | None = None,
) -> LoginRecordedResponse:
    """Record a successful sign-in for the AUTHENTICATED caller.

    Logins happen client-side via Supabase, so the backend has no native login
    hook — the frontend calls this exactly once, right after a successful
    password sign-in, with the freshly-issued token. It does two things:

      1. Stamps ``users.last_login_at`` = now (the column existed but nothing
         ever wrote it, so it was always NULL).
      2. Inserts a ``login_events`` row (the security log backing the engineer
         "Logins" tab; email is snapshotted so the history survives the user's
         later deletion).
      3. Clears the rolling failed-login counter for this email (#182) — a real
         sign-in is the correct, un-abusable place to reset it, so it no longer
         runs on every authenticated request.
      4. Claims this sign-in as the account's single active session (#147).

    Uses the force-password-change-EXEMPT resolver: a user on an admin-issued
    temp password has still genuinely signed in, so their login must be recorded
    even before they clear the flag. Takes no body and keys only on the token's
    own identity, so a caller can only ever record their OWN login.

    Best-effort by contract: the frontend never blocks the post-login redirect
    on this call. It is deliberately NOT written to ``audit_logs`` — sign-in
    events are a security log, not the record-change audit trail.
    """
    now = datetime.datetime.now(datetime.UTC)
    db_user = await session.scalar(
        select(User).where(User.user_id == user.user_id)
    )
    if db_user is not None:
        db_user.last_login_at = now
        # Single active session (#147): claim THIS sign-in as the account's
        # active session. A newer login overwrites it, so any earlier device's
        # session no longer matches and is rejected (forced logout) on the data
        # routes. Only claims when the token carried a session_id.
        if user.session_id:
            db_user.active_session_id = user.session_id
            db_user.active_session_at = now
        session.add(
            LoginEvent(
                user_id=db_user.user_id,
                email=db_user.email,
                occurred_at=now,
                ip_address=_clean(context.ip_address) if context else None,
                city=_clean(context.city) if context else None,
                region=_clean(context.region) if context else None,
                country=_clean(context.country) if context else None,
            )
        )
        # Login SUCCESS clears the rolling failed-login counter (#182). Runs in
        # this same transaction (the helper does not commit) so it lands
        # atomically with the sign-in bookkeeping in the single commit below.
        await _clear_login_attempts(session, db_user.email)
        await session.commit()
    return LoginRecordedResponse(last_login_at=now)


class SessionActiveResponse(BaseModel):
    """Whether the caller's session is still the account's single active one."""

    active: bool


@router.get("/session/active", response_model=SessionActiveResponse)
async def session_active(
    user: CurrentDBUserAllowMustChange,
) -> SessionActiveResponse:
    """Report whether THIS session is still the account's active session (#147).

    Uses the force-change-EXEMPT resolver, which does NOT reject a superseded
    session (unlike the data routes) — so a superseded device can still ask "am I
    still signed in?" and get a clean ``{active: false}`` instead of a 401. The
    frontend polls this and, on ``false``, signs the device out and explains why.

    Fails OPEN (``active: true``) when the account has no claimed session yet
    (``active_session_id`` NULL — e.g. a session predating this feature) or the
    token carried no ``session_id``, so nobody is spuriously logged out."""
    active_id = user.active_session_id
    current_id = user.session_id
    is_active = not (active_id and current_id and active_id != current_id)
    return SessionActiveResponse(active=is_active)


class PasswordCompleteResponse(BaseModel):
    """Acknowledgement that the caller's force-change flag was cleared."""

    status: str = "ok"


@router.post("/password/complete", response_model=PasswordCompleteResponse)
async def password_complete(
    user: CurrentDBUserAllowMustChange, session: SessionDep
) -> PasswordCompleteResponse:
    """Clear the force-password-change flag for the AUTHENTICATED caller.

    EXEMPT from the force-password-change gate (it depends on the exempt
    resolver): this is the very endpoint a flagged user calls to clear the flag,
    so it must remain reachable while ``must_change_password`` is true.

    Called by the frontend AFTER the user has set a new password via their own
    Supabase session (the actual password change happens client-side). This
    endpoint only flips ``users.must_change_password`` to false, and ONLY for
    the token's own user — it takes no id and so can never clear anyone else's
    flag. Any role may call it (it is a self-service action, not an admin one).

    Idempotent: a caller whose flag is already false simply gets a 200 and no
    audit row is written.
    """
    db_user = await session.scalar(
        select(User).where(User.user_id == user.user_id)
    )
    if db_user is not None and db_user.must_change_password:
        db_user.must_change_password = False
        session.add(
            AuditLog(
                user_id=user.user_id,
                action_type="password_changed",
                entity_type="user",
                entity_id=user.user_id,
                field_name="must_change_password",
                old_value="true",
                new_value="false",
            )
        )
        await session.commit()
    return PasswordCompleteResponse()


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


async def _record_login_failure(
    session: AsyncSession, payload: LoginRecordRequest
) -> None:
    """Log one per-attempt ``login_failures`` row for a failed sign-in.

    BEST-EFFORT: this is a pure side-effect for the engineer Login-failures tab
    and must never break the throttle response, so any error (a DB hiccup, the
    table missing) is swallowed after a rollback. The attempted email is
    snapshotted lowercased to match the throttle's case-insensitive keying
    (login_attempts); IP/geo are cleaned like the success path (login_events).
    Runs in its OWN commit — ``record_attempt`` has already committed the
    counter, so a failure here can't roll that back.
    """
    context = payload.context
    try:
        session.add(
            LoginFailure(
                email=payload.email.strip().lower(),
                ip_address=_clean(context.ip_address) if context else None,
                city=_clean(context.city) if context else None,
                region=_clean(context.region) if context else None,
                country=_clean(context.country) if context else None,
                reason=_clean(payload.reason),
            )
        )
        await session.commit()
    except Exception:
        # Never let a logging failure surface to the (unauthenticated) caller or
        # change the anti-enumeration response. Roll back best-effort and move on.
        logger.warning("Failed to record a login_failures row", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            pass


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
    (``POST /auth/login`` -> ``_clear_login_attempts``), which only a real,
    signed-in user can reach.

    The ``locked`` flag the service returns is intentionally NOT echoed to the
    client (anti-enumeration); only the coarse ``reason`` is.

    On a failure, in addition to bumping the rolling counter, a per-attempt
    ``login_failures`` row is logged (attempted email snapshotted + forwarded IP /
    geo / reason) so the engineer Login-failures tab can show who failed, when,
    and from where. That insert is BEST-EFFORT: a logging failure is swallowed so
    it can never break the throttle response, and it is a pure side-effect — the
    response body is unchanged, preserving the anti-enumeration behavior.
    """
    if payload.success:
        # Do NOT clear/delete the login_attempts row for an unauthenticated
        # "success" claim. Report the benign status without mutating state.
        # Also log NOTHING: only failures are recorded to login_failures.
        return LoginThrottleStatus(
            allowed=True, reason="ok", retry_after_seconds=None
        )

    status_dict = await login_lockout.record_attempt(
        session, payload.email, success=False
    )
    await _record_login_failure(session, payload)
    return LoginThrottleStatus(
        allowed=status_dict["allowed"],
        reason=status_dict["reason"],
        retry_after_seconds=status_dict["retry_after_seconds"],
    )

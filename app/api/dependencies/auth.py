"""Authentication and authorization dependencies.

`get_current_user` verifies the bearer token and returns the token identity.
`get_current_db_user` resolves that identity to a database user and loads their
roles. Authorization (the `require_*` capability guards at the bottom of this
module) is decided from those database roles resolved against the live,
engineer-editable permission config — never from the token's claims.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import set_audit_actor
from app.core.capabilities import Capability, effective_capabilities
from app.core.database import get_session
from app.core.security import (
    AuthError,
    AuthorizationError,
    DeactivatedAccountError,
    MaintenanceModeError,
    MustChangePasswordError,
    SessionSupersededError,
    verify_supabase_jwt,
)
from app.models.login_attempt import LoginAttempt
from app.repositories.permissions import load_grants
from app.repositories.user import get_user_with_roles_by_auth_id
from app.schemas.auth import AuthenticatedUser, UserContext
from app.services import maintenance

logger = logging.getLogger(__name__)

# auto_error=False so a missing/!Bearer header routes through our AuthError
# handler (401 + error envelope) instead of FastAPI's default 403.
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> AuthenticatedUser:
    if credentials is None or not credentials.credentials:
        raise AuthError("Missing bearer token.")

    claims = verify_supabase_jwt(credentials.credentials)

    subject = claims.get("sub")
    if not subject:
        raise AuthError("Token is missing a subject.")

    return AuthenticatedUser(
        auth_user_id=subject,
        email=claims.get("email"),
        token_role=claims.get("role"),
        session_id=claims.get("session_id"),
    )


# Convenience alias for route signatures.
CurrentUser = Annotated[AuthenticatedUser, Depends(get_current_user)]


async def get_current_db_user_allow_must_change(
    current: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserContext:
    """Resolve the verified token identity to a provisioned, active DB user,
    WITHOUT enforcing the force-password-change gate.

    Identical to ``get_current_db_user`` (valid token, provisioned + active user
    required) EXCEPT it does NOT raise ``MustChangePasswordError`` when the user
    still holds the flag. This is the EXEMPT variant used only by the handful of
    routes a flagged user must still reach to complete the change itself
    (``GET /auth/context`` and ``POST /auth/password/complete``). Every other
    authenticated route uses ``get_current_db_user``, which enforces the gate.

    Raises AuthError (401) if the token subject isn't a valid id,
    AuthorizationError (403) if there's no matching user (a valid token for
    someone who was never granted access), and DeactivatedAccountError (403) if
    the user exists but has been deactivated — the latter is enforced here so a
    deactivated account is blocked on EVERY authenticated route, not just at the
    point of deactivation, and surfaces as its own security event.
    """
    try:
        auth_uuid = uuid.UUID(current.auth_user_id)
    except ValueError as exc:
        raise AuthError("Token subject is not a valid identifier.") from exc

    user = await get_user_with_roles_by_auth_id(session, auth_uuid)
    if user is None:
        raise AuthorizationError("Your account is not provisioned for access.")
    if not user.active:
        raise DeactivatedAccountError()

    # NOTE (#182): the rolling failed-login counter is NO LONGER cleared here.
    # This resolver runs on EVERY authenticated request (data routes, the
    # /auth/session/active poll, …); clearing login_attempts here issued a
    # DELETE + its own commit on every single one, even with zero matching rows.
    # The counter is now cleared once, on the login-success path
    # (POST /auth/login -> ``_clear_login_attempts``), which is the only
    # trustworthy, un-abusable place a real sign-in reaches.

    context = UserContext.from_orm_user(user)
    # Record whether this request's actor is an engineer so the audit layer
    # can drop their audit_logs writes (#199). Set on the base resolver, which
    # runs on EVERY authenticated request, so the guard is central and the
    # value is freshly re-set per request (no cross-request leakage).
    set_audit_actor(context.roles)
    # Carry THIS request's token session so downstream guards (single active
    # session, #147) can compare it against the account's active session. This
    # base resolver does NOT reject a superseded session — that is layered on in
    # ``get_current_db_user`` so the claiming call (POST /auth/login) and the
    # status probe can still run.
    context.session_id = current.session_id
    return context


async def get_current_db_user(
    current: CurrentUser,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> UserContext:
    """Resolve the verified token identity to a provisioned, active DB user and
    ENFORCE the force-password-change gate.

    Performs the same resolution as ``get_current_db_user_allow_must_change``
    (delegated, so the lookup / deactivation logic lives in one place) and then
    layers the ``must_change_password`` check on top: raises
    MustChangePasswordError (403 / ``password_change_required``) when the user is
    on an admin-issued temp password. This is enforced server-side on EVERY
    authenticated route — the frontend gate is NOT sufficient, since a valid
    session token could otherwise call data endpoints directly and bypass the
    forced change. Only ``GET /auth/context`` and ``POST /auth/password/complete``
    depend on the exempt variant above so the user can read and clear the flag.
    """
    user = await get_current_db_user_allow_must_change(current, session)
    _enforce_single_session(user)
    await _enforce_maintenance_mode(session, user)
    if user.must_change_password:
        raise MustChangePasswordError()
    return user


async def _enforce_maintenance_mode(
    session: AsyncSession, user: UserContext
) -> None:
    """Refuse non-exempt callers while site-wide maintenance mode is on.

    This is the real pause. Refusing at the login route alone would only stop
    sign-ins that go through our frontend — a user still holding a valid
    Supabase token could keep calling the API directly. Enforcing on the strict
    resolver closes that: while maintenance is on, every data route returns
    503 / ``maintenance_mode`` for everyone except the exempt set.

    THREE ROUTES ARE OUTSIDE THIS GATE, and it is worth being explicit rather
    than letting "every route" read as absolute. They are the ones on the
    force-password-change-EXEMPT resolver above, which does not call this:

      * ``GET /auth/session/active`` — MUST stay reachable. It is how a paused
        user's browser learns its session was ended and signs itself out; 503ing
        it would leave force-logged-out clients sitting on a dead session.
      * ``GET /auth/context`` — returns only the CALLER'S OWN identity, roles,
        and capabilities. No alumni data, nobody else's record. A paused user can
        still read it while their access token lives out its remaining minutes;
        that is accepted, because the frontend relies on it to decide where to
        send them (the ``(app)`` layout reads it, finds they are not an engineer,
        and redirects to the maintenance page).
      * ``POST /auth/password/complete`` — clears the caller's own temp-password
        flag and nothing else.

    Everything that reads or writes application data goes through this gate.

    TWO ORDERING RULES MATTER HERE, both about not bricking the site:

      * The exemption is checked FIRST, from roles already loaded on the
        UserContext, so an engineer never touches the maintenance table at all.
        Even a completely unreadable ``maintenance_mode`` row cannot affect an
        engineer's access.
      * ``read_status`` fails OPEN, so an unreadable row means "site is up" for
        everyone else too.

    Runs after ``_enforce_single_session`` so a user whose session was ended by
    the switch gets the ``session_superseded`` signal their client already knows
    how to act on (sign out) rather than a 503 they'd sit on.
    """
    if maintenance.is_exempt(user.roles):
        return
    status = await maintenance.read_status(session)
    if status.enabled:
        raise MaintenanceModeError(status.message or maintenance.REFUSAL_MESSAGE)


def _enforce_single_session(user: UserContext) -> None:
    """Reject a session the account has superseded by signing in elsewhere (#147).

    Enforced here (the strict resolver used by every data route) and NOT in the
    base resolver, so the sign-in that CLAIMS the new session (POST /auth/login)
    and the status probe (GET /auth/session/active) still run. Fails open when
    either id is absent: a token predating the ``session_id`` claim, or a user
    who hasn't signed in since this shipped (``active_session_id`` still NULL),
    is never locked out — enforcement begins once both exist and they differ.

    KNOWN LOW-RISK EDGE CASE (#188, deliberately NOT solved here): the claim on
    ``active_session_id`` is written only by the best-effort ``POST /auth/login``
    the frontend fires after sign-in. If that call is dropped, a NEW device can
    momentarily be the one kicked (the OLD session still holds ``active``) rather
    than superseding it. A recency-based server-side reclaim was rejected because
    it lets a superseded device steal the session back on token refresh (refresh
    keeps ``session_id`` but bumps ``iat``) and it reintroduces per-request
    writes (#182). The correct fix is a reliable login-time claim (e.g. a
    server-driven sign-in hook), left for future work; strict enforcement here
    keeps the single-session security guarantee intact in the meantime.
    """
    active = user.active_session_id
    current = user.session_id
    if active and current and active != current:
        raise SessionSupersededError()


async def _clear_login_attempts(session: AsyncSession, email: str) -> None:
    """Clear the rolling failed-login counter for ``email`` (login-success path).

    Called from ``POST /auth/login`` (the authenticated sign-in the frontend
    fires right after a successful password auth) — the only trustworthy,
    un-abusable place to drop the rolling ``login_attempts`` row, replacing both
    the old unauthenticated ``/auth/login/record {success:true}`` clear AND the
    per-request resolver clear removed in #182. Keyed on the lowercased email to
    match the throttle's case-insensitive keying.

    Does NOT manage the transaction — the caller owns the commit, so the clear
    lands atomically with the rest of the sign-in bookkeeping (last_login_at,
    login_events, the single-session claim).
    """
    await session.execute(
        delete(LoginAttempt).where(LoginAttempt.email_lc == email.lower())
    )


CurrentDBUser = Annotated[UserContext, Depends(get_current_db_user)]
# EXEMPT variant: same resolution (valid token, provisioned + active) but does
# NOT enforce the force-password-change gate. Use ONLY on the routes a flagged
# user must reach to complete the change (GET /auth/context, POST
# /auth/password/complete).
CurrentDBUserAllowMustChange = Annotated[
    UserContext, Depends(get_current_db_user_allow_must_change)
]


async def get_permission_config(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, frozenset[str]]:
    """Load the editable role→capabilities grant map for this request.

    FastAPI caches sub-dependencies within a single request, so this runs once
    per request even when several capability guards depend on it — no stale
    in-process cache to invalidate, and a revoke takes effect on the very next
    request. Falls back to the historical defaults if the table is empty (see
    ``app/repositories/permissions.load_grants``).
    """
    return await load_grants(session)


PermissionConfig = Annotated[dict[str, frozenset[str]], Depends(get_permission_config)]


def require_capability(
    capability: str,
) -> Callable[[UserContext, dict[str, frozenset[str]]], Awaitable[UserContext]]:
    """Build a dependency that requires the user to hold ``capability``.

    The capability is resolved against the LIVE permission config (editable by
    the engineer), not a frozen role set. The engineer always holds every
    capability (hard override in ``effective_capabilities``), so the engineer
    can never lock themselves out. Returns the UserContext on success; raises
    AuthorizationError (403) otherwise.
    """

    async def _guard(
        user: CurrentDBUser, config: PermissionConfig
    ) -> UserContext:
        if capability not in effective_capabilities(config, user.roles):
            raise AuthorizationError()
        return user

    return _guard


# The historical guards, now expressed as capability checks. The capability →
# default-role mapping in ``app/core/capabilities.DEFAULT_GRANTS`` reproduces the
# old hardcoded allow-lists exactly, so behaviour is unchanged until an engineer
# edits the config. Names and type aliases are preserved so every existing route
# import keeps working.
#
# `engineer` ⊇ `super_admin` ⊇ `full_access` ⊇ `student` ⊇ `view_only`. The
# engineer holds every capability unconditionally; the other roles hold whatever
# the config grants them.
require_super_admin = require_capability(Capability.USER_ADMIN)
# Donation-ledger writes / imports (#189). Split out from USER_ADMIN so that
# delegating user administration does NOT silently grant donation-ledger writes;
# defaults to the same roles (super_admin + engineer), so behaviour is unchanged.
require_donations_manage = require_capability(Capability.DONATIONS_MANAGE)
# Donation-ledger READS — the donor list, the fund totals, and the per-alumnus
# giving amounts on a profile (#379). Split out of the retired ALUMNI_FULL so a
# role can be shown the ledger without being able to write to it.
require_donations_view = require_capability(Capability.DONATIONS_VIEW)
# --- the guards that replaced `require_full_access` (#379) --------------------
#
# ALUMNI_FULL is retired: it gated alumni create/archive, both importers, every
# export, headshots, event management, notes, the survey console, donation reads
# and the advanced reports behind ONE switch. Each guard below now covers exactly
# one of those. They REPLACE the old guard on the routes they cover — never
# supplement it — otherwise the new toggles would be unusable on their own.
# Every one of them defaults to the roles that held ALUMNI_FULL, so day-one
# authorization is identical.
require_alumni_create = require_capability(Capability.ALUMNI_CREATE)
require_alumni_archive = require_capability(Capability.ALUMNI_ARCHIVE)
require_alumni_import = require_capability(Capability.ALUMNI_IMPORT)
require_alumni_export = require_capability(Capability.ALUMNI_EXPORT)
require_alumni_photos = require_capability(Capability.ALUMNI_PHOTOS)
require_events_manage = require_capability(Capability.EVENTS_MANAGE)
require_notes_manage = require_capability(Capability.NOTES_MANAGE)
require_surveys_manage = require_capability(Capability.SURVEYS_MANAGE)
# Deleting an opportunity link (#441 follow-up). Carved OUT of surveys.manage
# rather than added alongside it: surveys.manage still covers approve / reject /
# add / edit, and this guard is the single rule for deletion — both the one-at-a-
# time DELETE and the multi-select bulk route check it, so there is one answer to
# "who can erase a link" instead of two. Defaults to super_admin + engineer only.
require_links_delete = require_capability(Capability.LINKS_DELETE)
require_reports_advanced = require_capability(Capability.REPORTS_ADVANCED)
# Event authoring (#378), split out of ALUMNI_FULL as two separate capabilities
# so the engineer can widen "create an event" without also widening "upload a
# file that creates an event plus hundreds of attendance rows". Both default to
# the same roles that held ALUMNI_FULL (full_access + super_admin + engineer),
# so behaviour is unchanged until the config is edited.
require_events_create = require_capability(Capability.EVENTS_CREATE)
require_events_import = require_capability(Capability.EVENTS_IMPORT)
# Edit an EXISTING alumnus / their nested records — not create/archive/import,
# and no longer interactions (see require_interactions_create).
require_alumni_edit = require_capability(Capability.ALUMNI_EDIT)
# Log / amend an interaction on an alumnus's timeline (#379). Pulled out of the
# timeline writes that ALUMNI_EDIT described and seeded to EVERY role: the
# interaction routes were already open to any view-access role (#129), so this
# does not widen access — it makes a rule that lived only in code visible and
# editable in the permission matrix.
require_interactions_create = require_capability(Capability.INTERACTIONS_CREATE)
require_view_only = require_capability(Capability.VIEW)
# Controlled-vocabulary administration (editable dropdowns, #82).
require_vocab_admin = require_capability(Capability.VOCAB_ADMIN)
# Engineer console + permission editor. The `engineer` capability is not
# assignable to any other role, so this stays engineer-exclusive even though it
# now routes through the config like the others.
require_engineer = require_capability(Capability.ENGINEER)

RequireSuperAdmin = Annotated[UserContext, Depends(require_super_admin)]
RequireDonationsManage = Annotated[UserContext, Depends(require_donations_manage)]
RequireDonationsView = Annotated[UserContext, Depends(require_donations_view)]
RequireAlumniCreate = Annotated[UserContext, Depends(require_alumni_create)]
RequireAlumniArchive = Annotated[UserContext, Depends(require_alumni_archive)]
RequireAlumniImport = Annotated[UserContext, Depends(require_alumni_import)]
RequireAlumniExport = Annotated[UserContext, Depends(require_alumni_export)]
RequireAlumniPhotos = Annotated[UserContext, Depends(require_alumni_photos)]
RequireEventsCreate = Annotated[UserContext, Depends(require_events_create)]
RequireEventsImport = Annotated[UserContext, Depends(require_events_import)]
RequireEventsManage = Annotated[UserContext, Depends(require_events_manage)]
RequireNotesManage = Annotated[UserContext, Depends(require_notes_manage)]
RequireSurveysManage = Annotated[UserContext, Depends(require_surveys_manage)]
RequireLinksDelete = Annotated[UserContext, Depends(require_links_delete)]
RequireReportsAdvanced = Annotated[UserContext, Depends(require_reports_advanced)]
RequireAlumniEdit = Annotated[UserContext, Depends(require_alumni_edit)]
RequireInteractionsCreate = Annotated[
    UserContext, Depends(require_interactions_create)
]
RequireViewAccess = Annotated[UserContext, Depends(require_view_only)]
RequireVocabAdmin = Annotated[UserContext, Depends(require_vocab_admin)]
RequireEngineer = Annotated[UserContext, Depends(require_engineer)]

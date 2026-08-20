"""User administration routes (super_admin only).

Scope: list provisioned users, manage their roles on EXISTING accounts, reset a
user's password, edit a user's name, and create a brand-new login user. Creating
a user provisions a Supabase *auth* identity over the Admin API (service-role
key, server-side only) and returns a one-time temporary password exactly once —
the same security posture as the password-reset flow (see docs/PRE-LAUNCH.md).

Also hosts the ENGINEER-gated security screens, which are a different audience
from the super_admin user administration above: the sign-in log, the failed
sign-in log, and the live-session inventory with its revoke controls.
"""

import datetime
import logging
import re
import secrets
import unicodedata
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies.auth import (
    CurrentDBUser,
    RequireEngineer,
    RequireSuperAdmin,
)
from app.api.params import IdPath
from app.core.database import get_session
from app.core.errors import ConflictError, NotFoundError
from app.core.rate_limit import (
    AlertTemplateRateLimit,
    AssignRoleRateLimit,
    CreateUserRateLimit,
    DeleteUserRateLimit,
    ResetPasswordRateLimit,
    RevokeSessionRateLimit,
    TestAlertRateLimit,
)
from app.core.roles import ROLE_LABELS, ROLE_ORDER, RoleName
from app.core.security import AuthorizationError
from app.models.audit import AuditLog
from app.models.engineer_action import EngineerActionLog
from app.models.login_attempt import LoginAttempt
from app.models.login_event import LoginEvent
from app.models.login_failure import LoginFailure
from app.models.user import Role, User, UserRole
from app.schemas.auth import UserContext

# Kept on ONE LINE on purpose: tests/test_login_auto_block.py pins the set of
# modules allowed to reach ``login_block`` with a line-anchored regex, and a
# parenthesised import would drop this file out of that guard silently.
from app.services import alert_templates, auth_sessions, failure_alert, login_abuse, login_block
from app.services.supabase_admin import create_user as create_auth_user
from app.services.supabase_admin import delete_auth_user, set_user_password

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Temp-password generation: 20 chars from a mixed alphabet (no ambiguous 0/O/1/l/I)
# via the CSPRNG ``secrets``. ~120 bits of entropy — far beyond brute force for a
# one-time, immediately-rotated credential.
_TEMP_PW_ALPHABET = (
    "ABCDEFGHJKLMNPQRSTUVWXYZ" "abcdefghijkmnopqrstuvwxyz" "23456789" "!@#$%^&*?-_"
)
_TEMP_PW_LENGTH = 20


def _generate_temp_password() -> str:
    """Return a strong, single-use temporary password from the CSPRNG."""
    return "".join(secrets.choice(_TEMP_PW_ALPHABET) for _ in range(_TEMP_PW_LENGTH))


# --- Privilege ceiling for role mutation (#178) ------------------------------
#
# Role-mutation endpoints must never let an actor grant, remove, or delete a
# role that outranks their own highest tier. The old guards only special-cased
# the ``engineer`` role, so a lower role that an engineer had delegated the
# ``USER_ADMIN`` capability to (supported by the permission editor) could pass
# the route guard and then assign itself ``super_admin`` — a self-escalation.
# Ranking via the canonical ROLE_ORDER closes that path for every top tier while
# leaving the default config (super_admin / engineer holding USER_ADMIN)
# unchanged: a super_admin can still manage super_admin and below, and only an
# engineer can touch the engineer tier.


def _role_rank(role_name: str) -> int:
    """Privilege rank of a role: lower index = more privileged (see ROLE_ORDER).

    An unrecognized role ranks below every known role (least privileged).
    """
    for index, role in enumerate(ROLE_ORDER):
        if role.value == role_name:
            return index
    return len(ROLE_ORDER)


def _actor_ceiling_rank(actor: UserContext) -> int:
    """The actor's highest tier as a rank (lower = more privileged).

    An actor with no recognized role sits below the entire ladder.
    """
    return min((_role_rank(r) for r in actor.roles), default=len(ROLE_ORDER))


def _outranks_actor(actor: UserContext, role_name: str) -> bool:
    """True if ``role_name`` is above the actor's own highest tier."""
    return _role_rank(role_name) < _actor_ceiling_rank(actor)


# --- Name validation ---------------------------------------------------------
#
# Mirror the alumni NAME rules (app/schemas/alumni.py): a permissive deny-list so
# international/Unicode names pass, rejecting only characters that are meaningless
# inside a human name but meaningful to a SQL parser, plus control chars. Names
# are optional and capped at 100 to match ``users.first_name``/``last_name``.
_NAME_DISALLOWED = set(";=<>|")
_NAME_MAX = 100


def _validate_optional_name(value: object) -> str | None:
    """Validate/normalize an optional person-name field (or return ``None``)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Must be a string.")
    value = value.strip()
    if not value:
        return None
    if len(value) > _NAME_MAX:
        raise ValueError(f"Must be at most {_NAME_MAX} characters.")
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValueError("Must not contain control characters.")
    bad = sorted(_NAME_DISALLOWED & set(value))
    if bad:
        raise ValueError("Must not contain these characters: " + " ".join(bad))
    if value.isdigit():
        raise ValueError("Must not be only digits.")
    return value


# Email: kept a bounded plain string (no email-validator dependency, matching the
# rest of the project — see app/api/routes/auth.py). A light shape check rejects
# obvious non-addresses; the value is stored lowercased and the throttle/auth
# layers never trust it as a verified identity.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class CreateUserRequest(BaseModel):
    """Provision a new login user. ``role_name`` accepts any known role; WHICH
    role the creator may actually assign is enforced in the handler by the
    privilege-ceiling guard (an actor can only create a user at or below their
    own tier) — mirroring the assign-role endpoint, so a super_admin still can't
    bootstrap an engineer above their own tier. An unknown role is a 422; names
    follow the alumni NAME rules (≤100 chars). ``extra='forbid'`` rejects unknown
    keys."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    first_name: str | None = None
    last_name: str | None = None
    # Typed as the plain ``RoleName`` enum so an unknown role produces a clean
    # Pydantic 422. Any known role is accepted here; the create handler's
    # privilege-ceiling guard (``_outranks_actor``) is what actually stops an
    # actor from creating a user above their own tier.
    role_name: RoleName = RoleName.VIEW_ONLY

    @field_validator("email", mode="before")
    @classmethod
    def _validate_email(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = value.strip().lower()
        if not _EMAIL_RE.match(value):
            raise ValueError("Must be a valid email address.")
        return value

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def _validate_name(cls, value: object) -> str | None:
        return _validate_optional_name(value)


class UpdateUserNameRequest(BaseModel):
    """Edit a user's name. Both fields optional; same NAME rules (≤100 chars).
    Only keys present in the body (``exclude_unset``) are applied — so a client
    can clear a name by sending ``null``, or leave it untouched by omitting it.
    ``extra='forbid'`` rejects unknown keys (notably ``active``, which has its own
    endpoint)."""

    model_config = ConfigDict(extra="forbid")

    first_name: str | None = None
    last_name: str | None = None

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def _validate_name(cls, value: object) -> str | None:
        return _validate_optional_name(value)


class ResetPasswordResponse(BaseModel):
    """The one-time temporary password, shown to the super_admin exactly once."""

    temp_password: str


class CreateUserResponse(BaseModel):
    """The created user plus the one-time temporary password (shown exactly once,
    like the reset flow). The password is NEVER persisted or audited."""

    user_id: int
    email: str
    first_name: str | None = None
    last_name: str | None = None
    active: bool
    roles: list[str]
    temp_password: str


class DeleteUserResponse(BaseModel):
    """Confirmation of a permanent user deletion (the row is gone, so there is
    nothing left to serialize). The deleted user's id + email are echoed back so
    the UI can confirm exactly which account was removed."""

    deleted: bool
    user_id: int
    email: str


class LoginEventRow(BaseModel):
    """One recorded sign-in for the engineer Logins tab. ``user_id`` is null once
    the user has been deleted; ``email`` is the snapshot taken at sign-in, so the
    row still shows who it was. ``ip_address`` + ``city``/``region``/``country``
    are the approximate (IP-based) origin captured at sign-in; any may be null."""

    login_event_id: int
    user_id: int | None = None
    email: str
    occurred_at: datetime.datetime
    ip_address: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None


class LoginEventPage(BaseModel):
    """A page of login events, newest first, with the total for pagination."""

    items: list[LoginEventRow]
    total: int
    limit: int
    offset: int


class LoginPurgeResult(BaseModel):
    """Count of login-history rows removed by the engineer purge (#200)."""

    deleted: int


class LoginFailureRow(BaseModel):
    """One recorded FAILED sign-in for the engineer Login-failures tab. ``email``
    is the attempted address, snapshotted at the attempt (it may not belong to any
    account — a probe/typo). ``ip_address`` + ``city``/``region``/``country`` are
    the approximate (IP-based) origin, and ``reason`` a coarse failure code; any
    may be null."""

    login_failure_id: int
    email: str
    occurred_at: datetime.datetime
    ip_address: str | None = None
    city: str | None = None
    region: str | None = None
    country: str | None = None
    reason: str | None = None


class LoginFailurePage(BaseModel):
    """A page of login failures, newest first, with the total for pagination."""

    items: list[LoginFailureRow]
    total: int
    limit: int
    offset: int


class LoginAttackSource(BaseModel):
    """One source IP rolled up from ``login_failures``, for the Maintenance
    page's attack table (#456).

    ``attempts`` and ``distinct_emails`` are counts over the requested window;
    ``first_seen``/``last_seen`` bound the source's activity inside it, so a
    16-second burst and a 10-minute grind are told apart. ``attack_type`` is
    ``login_abuse.classify_source`` — the same classifier the Slack alert uses,
    so the table and the alert cannot disagree — and ``is_attack`` says whether
    the source crossed the detector's thresholds at all.

    ⚠️ There is deliberately NO email field. See the endpoint docstring.
    """

    ip_address: str
    city: str | None = None
    region: str | None = None
    country: str | None = None
    first_seen: datetime.datetime
    last_seen: datetime.datetime
    attempts: int
    distinct_emails: int
    attack_type: str
    is_attack: bool


class LoginAttackSourcePage(BaseModel):
    """The attack table: sources busiest-first, plus the window they cover.

    ``window_hours`` and ``limit`` echo what was actually applied so the console
    can say "in the last N hours" without re-deriving it from the request.
    """

    items: list[LoginAttackSource]
    window_hours: int
    limit: int


class LoginIpBlockRow(BaseModel):
    """One automatic login block (#457), for the engineer console's list.

    ``active`` is computed by the database as
    ``lifted_at IS NULL AND blocked_until > now()`` rather than by the client,
    so a stale browser tab cannot show a lapsed block as live. ``blocked_until``
    is the whole safety story in one field: it is never null and never more than
    24 hours out, so every row here ends by itself.

    ⚠️ There is deliberately NO email field, for the same reason the attack table
    has none — see that endpoint's docstring.
    """

    block_id: int
    ip_address: str
    blocked_at: datetime.datetime
    blocked_until: datetime.datetime
    active: bool
    attempt_count: int
    distinct_email_count: int
    pattern: str | None = None
    abuse_incident_id: int | None = None
    lifted_at: datetime.datetime | None = None
    lifted_by_user_id: int | None = None


class LoginIpBlockPage(BaseModel):
    """Blocks for this environment, active ones first.

    ``block_seconds`` and ``auto_block_enabled`` are echoed so the console can
    say how long a new block lasts, and can show "automatic blocking is OFF"
    rather than presenting an empty list as if it meant "nobody is blocked".
    """

    items: list[LoginIpBlockRow]
    active_only: bool
    limit: int
    block_seconds: int
    auto_block_enabled: bool


class AlertTestResult(BaseModel):
    """What a test alert actually did, per channel.

    ⚠️ CONFIGURED AND DELIVERED ARE SEPARATE FIELDS ON PURPOSE. "Nothing arrived"
    has two very different causes -- the channel has no webhook, or it has one and
    the send failed -- and a single boolean cannot tell them apart. Reporting both
    is the entire reason this endpoint is more useful than watching a channel.
    """

    purpose: str
    slack_configured: bool
    slack_delivered: bool
    email_configured: bool
    email_delivered: bool
    fell_back_to_error_channel: bool


class LoginIpBlockLifted(BaseModel):
    """Acknowledgement that a block was lifted, echoing what stopped applying."""

    block_id: int
    ip_address: str


# --- Editable alert wording (2026-08-20) -------------------------------------


class AlertTemplatePlaceholder(BaseModel):
    """One ``{name}`` a template may use, with what it means and an example.

    The console shows these next to the field, which is the only way an owner
    editing a sentence can know what he is allowed to say. ``example`` is what
    drives the live preview.
    """

    name: str
    description: str
    example: str


class AlertTemplateRow(BaseModel):
    """One editable message: its default wording, its current wording, and how
    the current wording renders.

    ``default_body`` is always sent, so the console can show what "reset" would
    restore without asking a second time — and so "customised" is visible as a
    difference rather than as a claim.

    ⚠️ ``placeholders`` IS THE COMPLETE LIST OF FACTS THIS MESSAGE CAN CARRY.
    There is no placeholder for an attempted email address and there never will
    be: they are unverified strings a stranger typed, some belong to real people,
    and a list of them in a Slack channel is an enumeration oracle. See
    ``app/services/alert_templates.py``.
    """

    key: str
    label: str
    description: str
    default_body: str
    body: str
    customized: bool
    preview: str
    placeholders: list[AlertTemplatePlaceholder]
    max_chars: int
    updated_at: datetime.datetime | None = None
    updated_by_user_id: int | None = None


class AlertTemplateList(BaseModel):
    """Every editable message, in the order the console shows them."""

    items: list[AlertTemplateRow]


class AlertTemplateUpdate(BaseModel):
    """New wording for one message.

    ``extra="forbid"`` so a typo'd field is a 422 rather than a silent no-op that
    looks like a successful save. The real validation — length, control
    characters, which placeholders are allowed — lives in
    ``alert_templates.validate_body`` and is applied at RENDER time as well, so a
    row that arrives some other way is held to the same rules.
    """

    model_config = ConfigDict(extra="forbid")

    body: str


class RoleAssign(BaseModel):
    """Assign a canonical role to a user. ``role_name`` is validated against the
    RoleName enum, so an unknown role is a 422 before any query runs."""

    model_config = ConfigDict(extra="forbid")

    role_name: RoleName


class UserActiveUpdate(BaseModel):
    """Activate or deactivate an existing user account.

    Deactivation is the REVERSIBLE way to remove access: it flips
    ``users.active`` to false, which the auth dependency layer enforces — a
    deactivated user is blocked (403) on every authenticated route but keeps
    their row, roles, and history and can be reactivated later. To remove an
    account permanently instead, use DELETE ``/users/{id}``.
    """

    model_config = ConfigDict(extra="forbid")

    active: bool


def _serialize(u: User) -> dict:
    return {
        "user_id": u.user_id,
        "email": u.email,
        "first_name": u.first_name,
        "last_name": u.last_name,
        "active": u.active,
        # Lock state so the Admin -> Users page can show a "Locked" badge. The
        # boolean is derived from locked_at so the UI doesn't need to interpret
        # the timestamp; locked_at is exposed for display/sorting.
        "locked": u.locked_at is not None,
        "locked_at": u.locked_at,
        # When the account was provisioned — shown in the Users tab.
        "created_at": u.created_at,
        "roles": [r.role_name for r in u.roles],
    }


async def _load_user(
    session: AsyncSession, user_id: int, *, populate_existing: bool = False
) -> User:
    stmt = (
        select(User).options(selectinload(User.roles)).where(User.user_id == user_id)
    )
    if populate_existing:
        # Overwrite an already-identity-mapped instance's attributes (incl. the
        # viewonly ``roles`` collection) from the DB. Needed after a commit that
        # inserted a UserRole directly: expire_on_commit=False otherwise keeps the
        # cached, pre-insert collection so the response omits the new role (#175).
        stmt = stmt.execution_options(populate_existing=True)
    user = await session.scalar(stmt)
    if user is None:
        raise NotFoundError(f"User {user_id} not found.")
    return user


@router.get("/users")
async def list_users(
    actor: RequireSuperAdmin,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """List provisioned users with their assigned roles (paginated).

    Paginated (default 50, hard cap 200 — mirrors the audit endpoint) so a single
    request can't enumerate the entire user directory at once, and each call is
    audited (``list_users``) so reads of the user list leave a forensic trail.
    The ``total`` count lets the UI page through. The access itself is recorded
    (actor + applied limit/offset); the returned rows are NOT logged.
    """
    total = await session.scalar(select(func.count()).select_from(User))
    rows = await session.scalars(
        select(User)
        .options(selectinload(User.roles))
        .order_by(User.email)
        .limit(limit)
        .offset(offset)
    )
    items = [_serialize(u) for u in rows.all()]

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="list_users",
            entity_type="user",
            field_name=f"limit={limit};offset={offset}",
        )
    )
    await session.commit()

    return {
        "items": items,
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/logins", response_model=LoginEventPage)
async def list_logins(
    actor: RequireEngineer,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LoginEventPage:
    """List recorded sign-ins, newest first (paginated). Engineer only.

    Backs the Admin -> Logins tab. Rows come from ``login_events`` (written by
    POST /auth/login on each successful sign-in); the snapshotted email means a
    deleted user's past logins remain attributable. Paginated (default 50, hard
    cap 200 — mirrors the users/audit endpoints) so one request can't enumerate
    the whole history. Reading the log is itself audited (``read_login_log``;
    actor + applied limit/offset) — the returned rows are not logged.

    Only logins WITH a captured IP are returned (so the tab is consistent — every
    row has IP + location). Logins recorded before IP capture, and local-dev
    sign-ins with no Vercel geo headers, have a null ``ip_address`` and are
    omitted; ``total`` reflects the filtered set so pagination stays correct.
    """
    has_ip = LoginEvent.ip_address.isnot(None)
    total = await session.scalar(
        select(func.count()).select_from(LoginEvent).where(has_ip)
    )
    rows = await session.scalars(
        select(LoginEvent)
        .where(has_ip)
        .order_by(LoginEvent.occurred_at.desc(), LoginEvent.login_event_id.desc())
        .limit(limit)
        .offset(offset)
    )
    items = [
        LoginEventRow(
            login_event_id=e.login_event_id,
            user_id=e.user_id,
            email=e.email,
            occurred_at=e.occurred_at,
            ip_address=e.ip_address,
            city=e.city,
            region=e.region,
            country=e.country,
        )
        for e in rows.all()
    ]

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="read_login_log",
            entity_type="login_event",
            field_name=f"limit={limit};offset={offset}",
        )
    )
    await session.commit()

    return LoginEventPage(
        items=items, total=int(total or 0), limit=limit, offset=offset
    )


@router.delete("/logins", response_model=LoginPurgeResult)
async def purge_logins(
    actor: RequireEngineer,
    session: SessionDep,
) -> LoginPurgeResult:
    """Delete ALL recorded sign-ins (the entire ``login_events`` history).
    Engineer only.

    The irreversible counterpart to GET /admin/logins: it wipes the whole
    login-history table in one shot (e.g. to clear accumulated dev/test noise
    from the Admin -> Logins tab). Engineer-gated (RequireEngineer) like the
    listing. Since #199 stops auditing engineer actions, this purge is
    intentionally NOT written to the audit trail. Returns the row count removed.

    SCOPE (security review, #199/#200): this deletes ONLY ``login_events``. It
    deliberately does NOT touch ``engineer_action_log`` -- that append-only table
    is the tamper-resistant record of engineer actions and has no purge path, so
    an engineer cannot use this endpoint (or any other) to erase their own trail.
    """
    result = await session.execute(delete(LoginEvent))
    await session.commit()
    return LoginPurgeResult(deleted=result.rowcount or 0)


@router.get("/login-failures", response_model=LoginFailurePage)
async def list_login_failures(
    actor: RequireEngineer,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LoginFailurePage:
    """List recorded FAILED sign-in attempts, newest first (paginated). Engineer
    only.

    Backs the Admin -> Login-failures tab. Rows come from ``login_failures``
    (written by POST /auth/login/record on each failure); the snapshotted email
    is the address that was ATTEMPTED, which may not belong to any account (a
    probe/typo). Engineer-gated (RequireEngineer) exactly like GET /admin/logins,
    and paginated (default 50, hard cap 200 — mirrors the logins/users/audit
    endpoints) so one request can't enumerate the whole log. Reading the log is
    itself audited (``read_login_failure_log``; actor + applied limit/offset) —
    the returned rows are not logged.

    Unlike GET /admin/logins (which filters to rows WITH a captured IP), this
    returns ALL failures: an attempt with no IP (local dev, or a client that
    forwarded no context) is still a meaningful failure to surface, and dropping
    it would hide real activity from a security log.
    """
    total = await session.scalar(
        select(func.count()).select_from(LoginFailure)
    )
    rows = await session.scalars(
        select(LoginFailure)
        .order_by(
            LoginFailure.occurred_at.desc(),
            LoginFailure.login_failure_id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    items = [
        LoginFailureRow(
            login_failure_id=f.login_failure_id,
            email=f.email,
            occurred_at=f.occurred_at,
            ip_address=f.ip_address,
            city=f.city,
            region=f.region,
            country=f.country,
            reason=f.reason,
        )
        for f in rows.all()
    ]

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="read_login_failure_log",
            entity_type="login_failure",
            field_name=f"limit={limit};offset={offset}",
        )
    )
    await session.commit()

    return LoginFailurePage(
        items=items, total=int(total or 0), limit=limit, offset=offset
    )


@router.get("/login-attack-sources", response_model=LoginAttackSourcePage)
async def list_login_attack_sources(
    actor: RequireEngineer,
    session: SessionDep,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LoginAttackSourcePage:
    """Failed sign-ins rolled up per SOURCE IP over a recent window. Engineer only.

    Backs the attack table on the engineer Maintenance page — the screen the
    owner opens during an incident. GET /admin/login-failures answers "what
    attempts happened"; this answers "who is doing it", which is the question
    that matters when 750 rows scroll past. One row per source: where it appears
    to be, when it started and stopped, how many attempts, how many distinct
    addresses, and what shape the campaign is.

    Read-only and side-effect free: it opens no incident, sends no alert, and
    never blocks anything. Engineer-gated (RequireEngineer) exactly like
    /login-failures and /logins, and the read is audited
    (``read_login_attack_sources``; actor + applied window/limit).

    THE CLASSIFIER IS SHARED, ON PURPOSE. ``attack_type`` comes from
    ``login_abuse.classify_source``, which wraps the very ``is_abusive`` and
    ``classify`` the Slack alert renders. The table and the alert therefore
    cannot describe the same IP two different ways, and retuning a threshold
    moves both at once.

    ⚠️ NO ATTEMPTED EMAIL ADDRESSES ARE RETURNED. Only ``distinct_emails``, the
    count. Those addresses are unverified strings typed by a stranger, some of
    them belong to real people, and a list of them is both the material the
    attacker was probing with and an enumeration oracle for anyone who reaches
    this response. The per-attempt detail, addresses included, stays where it
    already was: GET /admin/login-failures, behind the same engineer gate.

    ⚠️ ``ip_address`` IS CLIENT-SUPPLIED. It is copied from ``login_failures``,
    which the Next.js login action populates from the incoming request's
    ``x-forwarded-for``. Anyone calling this API directly can put anything there,
    so a source here can be forged to implicate an innocent address or rotated
    per request to evade the grouping entirely. It is the only per-attacker
    identifier this data has, so it is used — but it is a LEAD, not a verdict.
    Verify against the edge's own logs before blocking on it. The console states
    this alongside the table.

    Attempts with no captured IP are excluded (they cannot be attributed to a
    source, and one "unknown" bucket would sum unrelated people into a row that
    looks like a campaign) — they remain visible per-attempt on /login-failures.

    ``hours`` defaults to 24 and is capped at a week; ``limit`` defaults to 50
    and is hard-capped at 200, mirroring the neighbouring log endpoints, so one
    request cannot ask the database to aggregate unbounded history.
    """
    sources = await login_abuse.summarize_sources(
        session, window_seconds=hours * 3600, limit=limit
    )

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="read_login_attack_sources",
            entity_type="login_failure",
            field_name=f"hours={hours};limit={limit}",
        )
    )
    await session.commit()

    return LoginAttackSourcePage(
        items=[LoginAttackSource(**s) for s in sources],
        window_hours=hours,
        limit=limit,
    )


# --- Live sessions: see them, and end them -----------------------------------
#
# The neighbouring engineer tabs above are HISTORIES — who signed in, who failed
# to. This one is an INVENTORY: which sessions are alive right now. It exists
# because Supabase sessions run for up to 400 days by default and this app's idle
# timeout is browser-memory only (#684), so a session opened weeks ago is still a
# live credential — and until now the only way to see one was to query
# ``auth.sessions`` by hand against production, and the only way to end one was
# to write a DELETE.
#
# HOW REVOCATION ACTUALLY ENDS ACCESS (both halves, and why one is not enough) is
# documented at length in app/services/auth_sessions.py. The short version: the
# ``auth.sessions`` DELETE kills the refresh token so no new access token can be
# minted, and the ``users.active_session_id`` sentinel (#147's machinery, reused
# exactly as maintenance mode reuses it) invalidates the OUTSTANDING access token
# on the very next request instead of leaving it valid until it expires. Both run
# in one transaction, so a revoke cannot half-apply.


class ActiveSessionRow(BaseModel):
    """One live Supabase session.

    ``user_id``/``roles`` come from OUR ``users`` table via the auth id; both are
    null/empty for a Supabase auth identity with no application user row, which
    is shown rather than hidden because a live session on one is an anomaly worth
    seeing. ``age_seconds`` is measured from ``created_at`` and is the number the
    screen exists to surface; ``idle_seconds`` runs from ``last_active_at``
    (the newest of created / updated / last-refreshed).

    ``is_current`` marks the caller's OWN session, so revoking it can be
    presented as the deliberate act it is. ``is_account_active_session`` says
    whether this is the session our API currently honours for that account
    (``users.active_session_id``) — a false here means #147 is already rejecting
    it even though Supabase would still refresh it.
    """

    session_id: uuid.UUID
    user_id: int | None = None
    email: str | None = None
    roles: list[str] = []
    account_active: bool
    created_at: datetime.datetime
    last_active_at: datetime.datetime
    refreshed_at: datetime.datetime | None = None
    age_seconds: int
    idle_seconds: int
    is_current: bool
    is_account_active_session: bool


class ActiveSessionPage(BaseModel):
    """A page of live sessions, OLDEST FIRST, with the total for pagination.

    Oldest-first is the opposite of the neighbouring log endpoints and is
    deliberate: the row that matters is the one that has been open for five
    weeks, so it must not be paged past.
    """

    items: list[ActiveSessionRow]
    total: int
    limit: int
    offset: int


class SessionRevokeResult(BaseModel):
    """Outcome of a revoke.

    ``sessions_deleted`` counts ``auth.sessions`` rows removed (the Supabase
    half). ``access_revoked`` says whether the ``users.active_session_id``
    sentinel was stamped (our half) — i.e. whether any outstanding access token
    on that account was invalidated immediately rather than left to expire.
    ``self_revoked`` is true when the caller ended their own current session and
    is about to be signed out.
    """

    revoked: bool
    sessions_deleted: int
    access_revoked: bool
    self_revoked: bool
    user_id: int | None = None
    email: str | None = None


@router.get("/sessions", response_model=ActiveSessionPage)
async def list_active_sessions(
    actor: RequireEngineer,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ActiveSessionPage:
    """List every LIVE Supabase session, oldest first (paginated). Engineer only.

    Backs the Admin -> Sessions tab. Rows come from ``auth.sessions`` joined to
    our ``users``/``roles`` tables, filtered to sessions that have not expired.
    Engineer-gated and paginated (default 50, hard cap 200) exactly like the
    logins / login-failures endpoints.

    Reading the inventory is itself audited (``read_active_sessions``; actor +
    applied limit/offset) — the returned rows are not logged. As with every other
    engineer action, the audit layer reroutes an engineer's ``AuditLog`` to
    ``engineer_action_log`` (#199).

    NOT rate-limited, on purpose: this is the read an engineer uses to DECIDE
    what to revoke, and throttling it would brake the recovery path rather than
    the destructive one (the same reasoning that leaves maintenance-mode DISABLE
    unthrottled).
    """
    rows, total = await auth_sessions.list_active(
        session, limit=limit, offset=offset
    )
    now = datetime.datetime.now(datetime.UTC)
    items = [
        ActiveSessionRow(
            session_id=r["session_id"],
            user_id=r["user_id"],
            email=r["email"],
            roles=list(r["roles"] or []),
            account_active=bool(r["account_active"]),
            created_at=r["created_at"],
            last_active_at=r["last_active_at"],
            refreshed_at=r["refreshed_at"],
            age_seconds=auth_sessions.age_seconds(r["created_at"], now),
            idle_seconds=auth_sessions.age_seconds(r["last_active_at"], now),
            is_current=(
                actor.session_id is not None
                and str(r["session_id"]) == actor.session_id
            ),
            is_account_active_session=bool(r["is_account_active_session"]),
        )
        for r in rows
    ]

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="read_active_sessions",
            entity_type="auth_session",
            field_name=f"limit={limit};offset={offset}",
        )
    )
    await session.commit()

    return ActiveSessionPage(
        items=items, total=total, limit=limit, offset=offset
    )


@router.delete("/sessions/{session_id}", response_model=SessionRevokeResult)
async def revoke_active_session(
    session_id: uuid.UUID,
    actor: RevokeSessionRateLimit,
    session: SessionDep,
    confirm_self: Annotated[bool, Query()] = False,
) -> SessionRevokeResult:
    """Revoke ONE live session. Engineer only. Destructive and irreversible —
    the person is signed out and must sign in again.

    Both halves, in one transaction (see app/services/auth_sessions.py):
      1. DELETE the ``auth.sessions`` row. ``auth.refresh_tokens`` cascades, so
         no new access token can ever be minted for that session.
      2. Stamp ``users.active_session_id`` with a ``revoked:<uuid4>`` sentinel
         when — and only when — that is needed to kill the OUTSTANDING access
         token: the session is the account's currently-honoured one, or the
         account has no claimed session at all (NULL fails open under #147). If
         the account has since claimed a DIFFERENT session, we do not stamp:
         #147 already rejects the revoked one, and stamping would sign the user
         out of the session they are legitimately using.

    SELF-REVOCATION (the lockout question). Ending your own current session is a
    legitimate thing to want — "sign me out of this device" — so it is allowed,
    but never as a side effect: it requires an explicit ``confirm_self=true``,
    and without it the call is refused (409) with nothing changed. The flag is
    the deliberate act; the confirmation dialog in the console is the second.

    It is NOT irrecoverable, and that is the point worth being explicit about.
    Unlike maintenance mode — where the switch that pauses the site could hide
    the switch that un-pauses it — nothing here touches the ability to sign in:
    the account is not locked, not deactivated, the password is unchanged, and
    ``POST /auth/login`` runs on the force-change-EXEMPT resolver, which does NOT
    enforce the single-session guard. So the very next sign-in re-claims
    ``active_session_id`` and clears the sentinel. The guard exists to prevent an
    ACCIDENT, not to prevent a lockout that cannot happen.

    404 if the session no longer exists (already expired, already revoked, or the
    listing was stale) — deliberately not a silent success, so the console does
    not report ending access it did not end.
    """
    self_revoke = (
        actor.session_id is not None and str(session_id) == actor.session_id
    )
    if self_revoke and not confirm_self:
        # Checked BEFORE anything is written, so a refusal changes nothing.
        raise ConflictError(
            "That is the session you are signed in with. Confirm that you want "
            "to sign yourself out of it."
        )

    # --- Supabase half ---
    auth_user_id = await auth_sessions.delete_supabase_session(session, session_id)
    if auth_user_id is None:
        # Nothing was deleted, so nothing was committed; roll back the empty
        # transaction rather than leaving it open.
        await session.rollback()
        raise NotFoundError("That session no longer exists.")

    # --- our half ---
    target = await session.scalar(
        select(User).where(User.auth_user_id == auth_user_id)
    )
    access_revoked = False
    if target is not None and auth_sessions.should_stamp_sentinel(
        active_session_id=target.active_session_id,
        revoked_session_id=str(session_id),
    ):
        target.active_session_id = auth_sessions.new_sentinel()
        target.active_session_at = datetime.datetime.now(datetime.UTC)
        access_revoked = True

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="revoke_session",
            entity_type="auth_session",
            entity_id=target.user_id if target is not None else None,
            field_name="session_id",
            old_value=str(session_id),
            new_value=(
                f"revoked access_revoked={str(access_revoked).lower()} "
                f"self={str(self_revoke).lower()}"
            ),
        )
    )
    await session.commit()

    return SessionRevokeResult(
        revoked=True,
        sessions_deleted=1,
        access_revoked=access_revoked,
        self_revoked=self_revoke,
        user_id=target.user_id if target is not None else None,
        email=target.email if target is not None else None,
    )


@router.delete("/users/{user_id}/sessions", response_model=SessionRevokeResult)
async def revoke_user_sessions(
    user_id: IdPath,
    actor: RevokeSessionRateLimit,
    session: SessionDep,
    confirm_self: Annotated[bool, Query()] = False,
) -> SessionRevokeResult:
    """Revoke EVERY live session for one user. Engineer only. Destructive and
    irreversible — the person is signed out on every device.

    Same two halves as the single revoke, except the sentinel is ALWAYS stamped:
    ending every session on the account is exactly what was asked for, so there
    is no case where leaving an outstanding access token alive is correct. Runs
    even when the user currently has zero ``auth.sessions`` rows — stamping the
    sentinel still invalidates any access token already in flight, so "sign this
    person out" does the whole job rather than most of it.

    SELF-REVOCATION: this necessarily includes the caller's own current session
    when they target themselves, so targeting your own account requires
    ``confirm_self=true`` — the same explicit act the single revoke requires,
    for the same reason (see ``revoke_active_session`` for why signing yourself
    out is recoverable and therefore permitted at all).

    SCOPE: there is deliberately no "revoke everything, everyone" endpoint. The
    only mass sign-out in this app is maintenance mode, which is built to keep
    engineers signed in precisely so the operator cannot strand themselves; a
    second, unguarded fleet-wide revoke would reintroduce that risk for no
    benefit this screen needs. Per-user is the widest blast radius offered here.
    """
    if user_id == actor.user_id and not confirm_self:
        # Checked BEFORE anything is written, so a refusal changes nothing.
        raise ConflictError(
            "That is your own account. Confirm that you want to sign yourself "
            "out everywhere, including this device."
        )

    target = await _load_user(session, user_id)

    # --- Supabase half: every session row for this auth identity ---
    deleted = await auth_sessions.delete_supabase_sessions_for_user(
        session, target.auth_user_id
    )

    # --- our half: always, for this endpoint ---
    target.active_session_id = auth_sessions.new_sentinel()
    target.active_session_at = datetime.datetime.now(datetime.UTC)

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="revoke_user_sessions",
            entity_type="user",
            entity_id=user_id,
            field_name="sessions",
            old_value=target.email,
            new_value=(
                f"revoked sessions_deleted={deleted} "
                f"self={str(user_id == actor.user_id).lower()}"
            ),
        )
    )
    await session.commit()

    return SessionRevokeResult(
        revoked=True,
        sessions_deleted=deleted,
        access_revoked=True,
        self_revoked=user_id == actor.user_id,
        user_id=user_id,
        email=target.email,
    )


@router.get("/login-ip-blocks", response_model=LoginIpBlockPage)
async def list_login_ip_blocks(
    actor: RequireEngineer,
    session: SessionDep,
    active_only: Annotated[bool, Query()] = True,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> LoginIpBlockPage:
    """Automatic login blocks for this environment (#457). Engineer only.

    The "see it" half of the requirement that an engineer can see and lift
    blocks; DELETE below is the "lift it" half. Active blocks come first, then —
    when ``active_only=false`` — recent history including lifted and lapsed ones,
    which is what makes "did this ever fire on us?" answerable.

    Read-only and side-effect free apart from the read audit
    (``read_login_ip_blocks``), exactly like /login-attack-sources.

    ⚠️ NO ATTEMPTED EMAIL ADDRESSES ARE RETURNED — only ``distinct_email_count``.
    Same rule and same reason as the attack table and the Slack alert: those
    addresses are unverified strings a stranger typed, some belong to real
    people, and a list of them is an enumeration oracle for anything that reaches
    this response.

    ⚠️ ``ip_address`` IS CLIENT-SUPPLIED, forwarded from ``x-forwarded-for``. It
    is a LEAD, not a verdict, and the console states this alongside the table.
    Blocking on it is safe only because ``login_block`` refuses to block an
    address with a recent successful sign-in or one an engineer has signed in
    from — read that module before drawing conclusions from a row here.
    """
    blocks = await login_block.list_blocks(
        session, active_only=active_only, limit=limit
    )

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="read_login_ip_blocks",
            entity_type="login_ip_block",
            field_name=f"active_only={active_only};limit={limit}",
        )
    )
    await session.commit()

    return LoginIpBlockPage(
        items=[LoginIpBlockRow(**b) for b in blocks],
        active_only=active_only,
        limit=limit,
        block_seconds=login_block.BLOCK_SECONDS,
        auto_block_enabled=login_block.blocking_enabled(),
    )


@router.delete("/login-ip-blocks/{block_id}", response_model=LoginIpBlockLifted)
async def lift_login_ip_block(
    block_id: IdPath,
    actor: RequireEngineer,
    session: SessionDep,
) -> LoginIpBlockLifted:
    """Lift one automatic login block early (#457). Engineer only.

    The manual override on a control that can refuse people. It exists because a
    block is a heuristic acting on a CLIENT-SUPPLIED address, and the person who
    can tell it got one wrong must be able to say so without waiting out the
    hour or editing the production database by hand.

    A lifted source is not automatically re-blocked for
    ``login_block.LIFT_GRACE_SECONDS`` (24 hours). Without that grace the next
    failed login from the same address would re-open the block and this endpoint
    would be decorative — the false positive would outlive the fix.

    404 if there is no ACTIVE block with that id (already lifted, or never
    existed), so a double-click is a clean 404 rather than a second lift that
    rewrites who lifted it.

    Engineer-gated (RequireEngineer) like the neighbouring login endpoints, and
    audited (``lift_login_ip_block`` + the source address). Note the gate is on
    the ROLE the caller holds, not on where they are calling from: blocks are
    consulted only on the two unauthenticated pre-login routes, so an engineer
    signing in from a blocked address is unaffected and can reach this endpoint
    to clear it.
    """
    lifted = await login_block.lift(
        session, block_id=block_id, actor_user_id=actor.user_id
    )
    if lifted is None:
        raise NotFoundError("No active login block with that id.")

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="lift_login_ip_block",
            entity_type="login_ip_block",
            entity_id=block_id,
            field_name="lifted_at",
            new_value=str(lifted["ip_address"]),
        )
    )
    await session.commit()

    return LoginIpBlockLifted(
        block_id=int(lifted["block_id"]), ip_address=str(lifted["ip_address"])
    )


@router.post("/alerts/test", response_model=AlertTestResult)
async def send_test_alert(
    actor: TestAlertRateLimit,
    session: SessionDep,
    purpose: Annotated[Literal["operational", "security"], Query()] = "operational",
) -> AlertTestResult:
    """Send one clearly-marked TEST alert to a channel. Engineer only.

    Answers "is alerting actually wired up?" without breaking anything. Before
    this, the only way to prove the operational channel worked was to make the API
    fail three times over a minute -- a deliberate production outage to check a
    webhook. The security channel could at least be proved by simulating an
    attack, which is how the 2026-08-19 misrouting was found at all.

    It uses the real renderer, the real fan-out and the real webhooks, and it
    touches NO incident state: nothing is opened, claimed or resolved, so a test
    can never suppress the alert for a real incident starting a second later.

    The response reports each channel separately -- configured, and delivered --
    plus whether a security alert is currently falling back to the error channel
    because ``SLACK_SECURITY_WEBHOOK_URL`` is unset. That fallback is deliberate
    and documented, but it is invisible from inside Slack, which is exactly how it
    went unnoticed.

    Rate limited to six an hour (engineer-gated on top): it is the one route whose
    whole job is to post to a third party.
    """
    result = await failure_alert.deliver_test_alert(
        purpose=purpose, requested_by=actor.email
    )

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="send_test_alert",
            entity_type="alert",
            field_name=f"purpose={purpose}",
            new_value=(
                f"slack={'sent' if result['slack_delivered'] else 'no'};"
                f"email={'sent' if result['email_delivered'] else 'no'}"
            ),
        )
    )
    await session.commit()

    return AlertTestResult(**result)


# --- Editable alert wording (2026-08-20) -------------------------------------
#
# The owner reads these sentences before he reads anything else, and until now
# every word of them was a string literal behind a deploy. These three routes make
# the WORDING data while leaving the FACTS in the renderers: a template names
# placeholders and can reach nothing else, so no edit here can widen what an
# alert is able to say. See app/services/alert_templates.py.

#: The message being edited. Bounded and pattern-checked so an absurd path
#: segment is a 422 before any query runs; the real check is membership of
#: ``alert_templates.KINDS``, which the service does.
TemplateKeyPath = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[a-z_]+$")]


@router.get("/alert-templates", response_model=AlertTemplateList)
async def list_alert_templates(
    actor: RequireEngineer,
    session: SessionDep,
) -> AlertTemplateList:
    """The editable Slack alert wording, with defaults and previews. Engineer only.

    The "see it" half of letting the owner write his own alerts; PUT below is the
    "change it" half and DELETE is the undo. Each row carries the built-in
    default alongside the current body, so the console can show what reset would
    restore and can mark a message as customised by comparing the two rather than
    by asking whether a row exists — which matters because the migration SEEDS the
    defaults, and a seeded row is not an edit.

    Read UNCACHED, unlike the alerting path's read: the console must show what is
    stored right now, or an engineer saves an edit and appears to see it not take.

    Engineer-gated (``RequireEngineer``) like the neighbouring alerting routes,
    and audited (``read_alert_templates``). Not rate limited — it is a read, and
    braking the screen someone uses to fix a bad template is the same
    lockout-shaped mistake as limiting the maintenance-mode disable route.
    """
    items = await alert_templates.list_all(session)

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="read_alert_templates",
            entity_type="alert_template",
        )
    )
    await session.commit()

    return AlertTemplateList(items=[AlertTemplateRow(**item) for item in items])


@router.put("/alert-templates/{template_key}", response_model=AlertTemplateRow)
async def update_alert_template(
    template_key: TemplateKeyPath,
    payload: AlertTemplateUpdate,
    actor: AlertTemplateRateLimit,
    session: SessionDep,
) -> AlertTemplateRow:
    """Rewrite one alert's wording. Engineer only.

    422 with a message the engineer can act on if the body is empty, longer than
    ``alert_templates.MAX_BODY_CHARS``, contains a control or invisible character,
    names a placeholder this message does not have, or carries a brace that is not
    part of one. Those rules exist because a template that cannot render costs a
    real alert its wording, and the moment to find that out is while someone is
    typing — not while the site is down.

    ⚠️ WHAT THIS CANNOT DO. It cannot make an alert say something the renderer
    does not already compute, and specifically it cannot reach an attempted email
    address: substitution resolves names against a dict the renderer built, so the
    reachable facts are exactly the ``placeholders`` list on the GET above. It also
    cannot silence an alert — a body that will not render is refused here, and a
    body that somehow gets stored anyway is discarded whole at render time in
    favour of the built-in default.

    Engineer-gated through the rate limiter's own ``actor_guard`` (the same shape
    as POST /admin/alerts/test), rate limited to 30 per ten minutes, and audited
    (``update_alert_template``). The audit records WHICH message changed, not the
    prose — the wording itself is one SELECT away in the table, and an audit row
    is not the place to keep a copy of it.
    """
    if template_key not in alert_templates.KINDS:
        raise NotFoundError("No alert message with that name.")

    await alert_templates.set_body(
        session,
        kind_key=template_key,
        body=payload.body,
        actor_user_id=actor.user_id,
    )

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="update_alert_template",
            entity_type="alert_template",
            field_name=template_key,
            new_value="customized",
        )
    )
    await session.commit()

    return await _one_alert_template(session, template_key)


@router.delete("/alert-templates/{template_key}", response_model=AlertTemplateRow)
async def reset_alert_template(
    template_key: TemplateKeyPath,
    actor: RequireEngineer,
    session: SessionDep,
) -> AlertTemplateRow:
    """Put one alert's wording back to the built-in default. Engineer only.

    The undo, and deliberately the CHEAP direction: it deletes the override row,
    after which the message says exactly what it said before anybody edited it.

    404 if there was nothing stored, so a double-click is a clean "already the
    default" rather than a second delete that implies something changed.

    NOT rate limited, on purpose and for the same reason the maintenance-mode
    *disable* route is not: this is the recovery path from wording that broke the
    message, and a limiter on the way back is itself the failure mode. The brake
    belongs on the direction that does the damage, which is the PUT above.
    """
    cleared = await alert_templates.clear(session, kind_key=template_key)
    if not cleared:
        raise NotFoundError("That alert message is already using its default wording.")

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="reset_alert_template",
            entity_type="alert_template",
            field_name=template_key,
            new_value="default",
        )
    )
    await session.commit()

    return await _one_alert_template(session, template_key)


async def _one_alert_template(
    session: AsyncSession, template_key: str
) -> AlertTemplateRow:
    """Re-read one row through the SAME code path the list uses.

    Both writes answer with a freshly read row rather than echoing what they were
    handed, so the console never has to guess at ``customized`` or at the preview
    — and so the two writes can never drift from the list in how they describe the
    same template.
    """
    for item in await alert_templates.list_all(session):
        if item["key"] == template_key:
            return AlertTemplateRow(**item)
    raise NotFoundError("No alert message with that name.")


# --- Engineer-action oversight log (#199 / #200 forensic blind spot) ----------
#
# ``engineer_action_log`` is the append-only, tamper-resistant record of actions
# taken by an engineer actor (rerouted there from ``audit_logs`` by the
# before_flush guard in app/models/audit.py). It is oversight *of* the engineer,
# so the READ gate below is ROLE-based (super_admin) and explicitly EXCLUDES the
# engineer -- unlike RequireSuperAdmin, which is capability-based (USER_ADMIN) and
# which an engineer also satisfies (the engineer holds every capability via a hard
# override in effective_capabilities). Gating on the capability would let the
# audited party read their own oversight log; the strict role gate keeps the log
# beyond engineer control (they cannot read, view-gate, delete, or disable it).


async def require_super_admin_role_strict(actor: CurrentDBUser) -> UserContext:
    """Require the ``super_admin`` role, explicitly denying ``engineer`` (403).

    Used only for the engineer-action oversight log: the engineer is the audited
    party and must not be able to read or otherwise control it, so the plain
    capability-based RequireSuperAdmin (which the engineer satisfies) is too broad
    here. Any actor holding ``engineer`` is rejected even if they also hold
    ``super_admin``.
    """
    if (
        RoleName.ENGINEER.value in actor.roles
        or RoleName.SUPER_ADMIN.value not in actor.roles
    ):
        raise AuthorizationError()
    return actor


RequireSuperAdminStrict = Annotated[
    UserContext, Depends(require_super_admin_role_strict)
]


class EngineerActionRow(BaseModel):
    """One recorded engineer action for the super_admin oversight view.
    ``actor_user_id`` is null once the engineer has been deleted; ``actor_email``
    is the snapshot taken at write time, so the row still shows who acted."""

    engineer_action_log_id: int
    actor_user_id: int | None = None
    actor_email: str | None = None
    action_type: str
    entity_type: str
    entity_id: int | None = None
    field_name: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    occurred_at: datetime.datetime


class EngineerActionPage(BaseModel):
    """A page of engineer actions, newest first, with the total for pagination."""

    items: list[EngineerActionRow]
    total: int
    limit: int
    offset: int


@router.get("/engineer-actions", response_model=EngineerActionPage)
async def list_engineer_actions(
    actor: RequireSuperAdminStrict,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> EngineerActionPage:
    """List recorded engineer actions, newest first (paginated). super_admin only.

    Reads the append-only ``engineer_action_log`` -- the tamper-resistant oversight
    trail of engineer actions (#199/#200). ROLE-gated to ``super_admin`` and
    explicitly denied to the ``engineer`` (see require_super_admin_role_strict), so
    the audited party can neither read nor disable it; there is no delete/purge
    route for this table at all. Paginated (default 50, hard cap 200 -- mirrors the
    users/logins/audit endpoints) so one request can't enumerate the whole log.

    Reading the log is itself audited (``read_engineer_action_log``; actor +
    applied limit/offset) -- the returned rows are not logged.
    """
    total = await session.scalar(
        select(func.count()).select_from(EngineerActionLog)
    )
    rows = await session.scalars(
        select(EngineerActionLog)
        .order_by(
            EngineerActionLog.occurred_at.desc(),
            EngineerActionLog.engineer_action_log_id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    items = [
        EngineerActionRow(
            engineer_action_log_id=e.engineer_action_log_id,
            actor_user_id=e.actor_user_id,
            actor_email=e.actor_email,
            action_type=e.action_type,
            entity_type=e.entity_type,
            entity_id=e.entity_id,
            field_name=e.field_name,
            old_value=e.old_value,
            new_value=e.new_value,
            occurred_at=e.occurred_at,
        )
        for e in rows.all()
    ]

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="read_engineer_action_log",
            entity_type="engineer_action_log",
            field_name=f"limit={limit};offset={offset}",
        )
    )
    await session.commit()

    return EngineerActionPage(
        items=items, total=int(total or 0), limit=limit, offset=offset
    )


# ``{user_id}`` is declared ``int`` (below), so string sub-paths like
# ``/users/{user_id}/name`` are unambiguous and never shadowed by this route —
# a non-numeric segment can't match an int path param.
@router.patch("/users/{user_id}")
async def set_user_active(
    user_id: IdPath,
    payload: UserActiveUpdate,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Deactivate or reactivate an existing user. super_admin only.

    Deactivation is the REVERSIBLE way to remove access: once ``active`` is false
    the auth dependency rejects every authenticated request from that user, but
    the row/roles/history are kept and access can be restored later. (Permanent
    removal is the separate DELETE ``/users/{id}`` endpoint.) A super_admin cannot
    deactivate their own account — that could lock administration out of the
    system. Every change is audited; a no-op (already in the requested state) is
    idempotent and not re-audited.
    """
    if payload.active is False and user_id == actor.user_id:
        raise ConflictError("You cannot deactivate your own account.")

    user = await _load_user(session, user_id)

    if user.active != payload.active:
        old_active = user.active
        user.active = payload.active
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="activate_user" if payload.active else "deactivate_user",
                entity_type="user",
                entity_id=user_id,
                field_name="active",
                old_value=str(old_active),
                new_value=str(payload.active),
            )
        )
        await session.commit()
    return _serialize(await _load_user(session, user_id))


@router.delete("/users/{user_id}", response_model=DeleteUserResponse)
async def delete_user(
    user_id: IdPath,
    actor: DeleteUserRateLimit,
    session: SessionDep,
) -> DeleteUserResponse:
    """Permanently delete a user — both the ``users`` row and the Supabase auth
    identity. super_admin and engineer only (engineer satisfies the guard).

    This is the irreversible counterpart to deactivation: use PATCH
    ``/users/{id}`` (``active=false``) to suspend access reversibly; use this to
    remove the account entirely (e.g. a wrong/duplicate provision).

    Integrity is handled by the schema's foreign keys, NOT by cascading our own
    deletes: ``user_roles`` is ``ON DELETE CASCADE`` (role grants are removed
    with the user), and every other reference — audit logs, interactions, tasks,
    events, attachments, import batches — is ``ON DELETE SET NULL``. So the
    FERPA audit trail and all alumni-side history are preserved; only the actor
    pointer on those rows becomes null.

    Guards (mirroring remove_role):
      * You cannot delete your own account.
      * Privilege ceiling (#178): an actor may only delete a user whose highest
        role is at or below the actor's own highest tier (ranked via
        ROLE_ORDER) — so only an engineer may delete an engineer, and only
        super_admin/engineer may delete a super_admin.
      * Last-holder guard: you cannot delete the final holder of a top role when
        that would lock administration out of the system. The last ENGINEER is
        always protected (the engineer holds unique vocab/database powers no
        other role can). The last SUPER_ADMIN is protected only when NO engineer
        remains — the engineer tier is a superset of super_admin (engineer ⊇
        super_admin), so as long as an engineer exists, user administration is
        still available and the sole super_admin CAN be deleted (notably, an
        engineer deleting it).

    Order of operations: the DB row (plus a ``delete_user`` audit entry,
    attributed to the actor and recording the deleted user's email) is committed
    FIRST, then the Supabase auth identity is best-effort deleted. If that last
    step fails the account is already gone from the app (the auth layer requires
    a matching ``users`` row), so we log the orphaned auth UUID for manual
    reconciliation rather than failing the request.
    """
    if user_id == actor.user_id:
        raise ConflictError("You cannot delete your own account.")

    user = await _load_user(session, user_id)
    target_roles = {r.role_name for r in user.roles}

    # Privilege ceiling (#178): never delete a user who holds a role above the
    # actor's own highest tier. Ranking every role via ROLE_ORDER (not just
    # special-casing engineer) blocks a lower role that was delegated the
    # USER_ADMIN capability from deleting a super_admin/engineer.
    highest_target = min(target_roles, key=_role_rank, default=None)
    if highest_target is not None and _outranks_actor(actor, highest_target):
        label = ROLE_LABELS.get(highest_target, highest_target)
        raise AuthorizationError(
            f"You cannot delete a user who holds the {label} role; "
            "it is above your privilege tier."
        )

    # System-wide last-holder guard: never delete the final holder of a top role
    # when that would leave the system with no one able to administer it. The two
    # top tiers differ in what "locks administration out" means:
    #   * ENGINEER holds capabilities NO other role can (vocab/database admin, the
    #     engineer console), so the last engineer is irreplaceable and must never
    #     be deletable.
    #   * SUPER_ADMIN only holds USER_ADMIN + alumni capabilities, ALL of which the
    #     engineer also holds (engineer ⊇ super_admin). So the last super_admin is
    #     only truly the last user-administrator when there is ALSO no engineer.
    #     Guarding it unconditionally was a MISFIRE: it blocked an engineer (the
    #     top role, which retains every super_admin capability) from deleting the
    #     sole super_admin, even though administration was never at risk.
    async def _holder_count(role_name: str) -> int:
        role = await session.scalar(
            select(Role).where(Role.role_name == role_name)
        )
        if role is None:
            return 0
        return (
            await session.scalar(
                select(func.count())
                .select_from(UserRole)
                .where(UserRole.role_id == role.role_id)
            )
            or 0
        )

    if RoleName.ENGINEER.value in target_roles:
        if await _holder_count(RoleName.ENGINEER.value) <= 1:
            raise ConflictError(
                f"Cannot delete the last {RoleName.ENGINEER.value}."
            )

    if RoleName.SUPER_ADMIN.value in target_roles:
        # Blocked only when this is the last super_admin AND there is no engineer
        # to fall back on; otherwise the engineer tier still administers users.
        if (
            await _holder_count(RoleName.SUPER_ADMIN.value) <= 1
            and await _holder_count(RoleName.ENGINEER.value) == 0
        ):
            raise ConflictError(
                f"Cannot delete the last {RoleName.SUPER_ADMIN.value}."
            )

    auth_user_id = user.auth_user_id
    email = user.email

    # Audit BEFORE the delete: the actor still exists, and we capture the deleted
    # user's email/id (entity_id has no FK, so it survives the row removal).
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="delete_user",
            entity_type="user",
            entity_id=user_id,
            field_name="email",
            old_value=email,
        )
    )
    await session.delete(user)  # cascades user_roles; SET NULL on every other ref
    await session.commit()

    # Best-effort removal of the auth identity (compensating-style, like
    # create_user's cleanup). A failure here leaves an orphaned Supabase identity
    # that can no longer use the app; log the UUID (never any secret) so it can be
    # reconciled manually.
    try:
        await delete_auth_user(auth_user_id)
    except Exception:
        logger.error(
            "User %s (%s) deleted from the database, but the Supabase auth "
            "identity %s could not be deleted; reconcile manually.",
            user_id,
            email,
            auth_user_id,
        )

    return DeleteUserResponse(deleted=True, user_id=user_id, email=email)


@router.post("/users/{user_id}/roles")
async def assign_role(
    user_id: IdPath,
    payload: RoleAssign,
    actor: AssignRoleRateLimit,
    session: SessionDep,
) -> dict:
    """Grant a role to an existing user (idempotent). super_admin and up.

    Rate-limited per actor (best-effort, in-process) to brake bulk privilege
    changes. Privilege ceiling (#178): an actor may only grant a role at or
    below their own highest tier (ranked via ROLE_ORDER). So only an
    ``engineer`` may grant ``engineer``, and only ``super_admin``/``engineer``
    may grant ``super_admin`` — a lower role that was delegated ``USER_ADMIN``
    still cannot mint an account that outranks it.
    """
    if _outranks_actor(actor, payload.role_name.value):
        label = ROLE_LABELS.get(payload.role_name.value, payload.role_name.value)
        raise AuthorizationError(
            f"You cannot grant the {label} role; it is above your privilege tier."
        )
    user = await _load_user(session, user_id)
    role = await session.scalar(
        select(Role).where(Role.role_name == payload.role_name.value)
    )
    if role is None:
        raise NotFoundError(f"Role {payload.role_name.value} is not seeded.")

    if role.role_name not in {r.role_name for r in user.roles}:
        session.add(UserRole(user_id=user_id, role_id=role.role_id))
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="assign_role",
                entity_type="user",
                entity_id=user_id,
                field_name="role",
                new_value=role.role_name,
            )
        )
        await session.commit()
        # ``User.roles`` is a viewonly relationship and the session keeps objects
        # alive across commit (expire_on_commit=False), so a plain reload returns
        # the cached instance WITHOUT the just-added role. Reload with
        # populate_existing to overwrite the cached collection (#175).
        user = await _load_user(session, user_id, populate_existing=True)
    return _serialize(user)


@router.delete("/users/{user_id}/roles/{role_name}")
async def remove_role(
    user_id: IdPath,
    role_name: RoleName,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Revoke a role from an existing user (idempotent). super_admin and up.

    Privilege ceiling (symmetric with assign_role, #178): an actor may only
    remove a role at or below their own highest tier (ranked via ROLE_ORDER).
    So only an ``engineer`` may remove ``engineer``, and only
    ``super_admin``/``engineer`` may remove ``super_admin`` — a lower role that
    was delegated ``USER_ADMIN`` cannot strip a role that outranks it.

    Guards against an admin removing their OWN top role (super_admin or
    engineer), which would lock user administration (or, for engineer, vocab /
    database administration) out of the system if they were the last holder.
    """
    if _outranks_actor(actor, role_name.value):
        label = ROLE_LABELS.get(role_name.value, role_name.value)
        raise AuthorizationError(
            f"You cannot remove the {label} role; it is above your privilege tier."
        )
    await _load_user(session, user_id)  # 404 if the user doesn't exist
    role = await session.scalar(
        select(Role).where(Role.role_name == role_name.value)
    )
    if role is None:
        raise NotFoundError(f"Role {role_name.value} is not seeded.")

    link = await session.scalar(
        select(UserRole).where(
            UserRole.user_id == user_id, UserRole.role_id == role.role_id
        )
    )
    if link is not None:
        if role.role_name in {
            RoleName.SUPER_ADMIN.value,
            RoleName.ENGINEER.value,
        }:
            if user_id == actor.user_id:
                raise ConflictError(
                    f"You cannot remove your own {role.role_name} role."
                )
            # System-wide last-holder guard: never let the final holder of a top
            # role (super_admin / engineer) be stripped, which would lock user
            # (or, for engineer, vocab/database) administration out of the system
            # for everyone — not just the actor. One COUNT over the role's links.
            holders = await session.scalar(
                select(func.count())
                .select_from(UserRole)
                .where(UserRole.role_id == role.role_id)
            )
            if (holders or 0) <= 1:
                raise ConflictError(
                    f"Cannot remove the last {role.role_name}."
                )
        await session.delete(link)
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="remove_role",
                entity_type="user",
                entity_id=user_id,
                field_name="role",
                old_value=role.role_name,
            )
        )
        await session.commit()
    return _serialize(await _load_user(session, user_id))


@router.post("/users/{user_id}/reset-password", response_model=ResetPasswordResponse)
async def reset_password(
    user_id: IdPath,
    actor: ResetPasswordRateLimit,
    session: SessionDep,
) -> ResetPasswordResponse:
    """Set a strong one-time temporary password on a user. super_admin only.

    Flow:
      1. Load the target user and resolve its Supabase auth identity
         (``users.auth_user_id``).
      2. Generate a CSPRNG temp password and set it on the Supabase auth user via
         the Admin API (server-side, service-role key). A non-2xx / transport
         failure raises ServiceError (502) WITHOUT leaking the upstream response.
      3. On success, clear any hard lock (``locked_at`` / ``locked_reason``) and
         delete the rolling ``login_attempts`` row for that email, so the user can
         log in again immediately.
      4. Audit the action (``reset_password``; actor = the super_admin, entity =
         target user). The password is NEVER logged, audited, or returned in any
         channel other than this one-time response body.

    The temp password is returned ONCE in the response for the super_admin to
    hand to the user; the user should change it on next login.
    """
    user = await _load_user(session, user_id)

    temp_password = _generate_temp_password()

    # Set the password on the auth provider FIRST. If this fails we raise before
    # touching our DB, so we never clear a lock for a reset that didn't happen.
    await set_user_password(user.auth_user_id, temp_password)

    was_locked = user.locked_at is not None
    user.locked_at = None
    user.locked_reason = None
    # The user is now on a temp password — force them to set their own on next
    # login (cleared via POST /auth/password/complete).
    user.must_change_password = True

    # Drop the rolling failed-login counter for this email so a prior cooldown
    # doesn't immediately re-block the freshly-reset user. Match the throttle's
    # case-insensitive keying (lowercased email).
    await session.execute(
        delete(LoginAttempt).where(LoginAttempt.email_lc == user.email.lower())
    )

    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="reset_password",
            entity_type="user",
            entity_id=user_id,
            # The audited field is the password; the prior account state is
            # recorded as old_value. The password itself is NEVER stored.
            field_name="password",
            old_value="locked" if was_locked else "active",
            new_value="reset",
        )
    )
    await session.commit()

    return ResetPasswordResponse(temp_password=temp_password)


@router.post("/users", response_model=CreateUserResponse, status_code=201)
async def create_user(
    payload: CreateUserRequest,
    actor: CreateUserRateLimit,
    session: SessionDep,
) -> CreateUserResponse:
    """Provision a brand-new login user. super_admin only.

    Flow:
      1. Reject up front if a ``users`` row with that email already exists. The
         message is generic (anti-enumeration, consistent with the rest of the
         codebase) — a 409 either way.
      2. Generate a CSPRNG temp password and create the Supabase *auth* user over
         the Admin API (server-side, service-role key, ``email_confirm=True`` so
         the user can sign in immediately). A transport/non-2xx failure raises
         ServiceError (502) WITHOUT leaking the upstream response, and BEFORE we
         touch our DB — so a failed provision never leaves an orphaned row.
      3. Insert the ``users`` row (linked by ``auth_user_id``) and a
         ``user_roles`` row for the chosen role. If this DB write fails after the
         auth identity was created, the auth user is deleted (compensating
         action) so no orphaned identity with a known temp password is left.
      4. Audit the action (``create_user``; actor = the super_admin, entity = the
         new user; ``new_value`` = email). The password is NEVER logged, audited,
         or returned in any channel other than this one-time response body.

    The temp password is returned ONCE for the super_admin to hand to the user;
    the user should change it on next login.
    """
    # Privilege ceiling (#178): an actor may only create a user with a role at or
    # below their own highest tier — mirrors the assign-role guard so a
    # super_admin can't bootstrap an engineer (a tier above them).
    if _outranks_actor(actor, payload.role_name.value):
        label = ROLE_LABELS.get(payload.role_name.value, payload.role_name.value)
        raise AuthorizationError(
            f"You cannot create a user with the {label} role; it is above your "
            "privilege tier."
        )

    # Anti-enumeration: a duplicate email is a generic conflict, not a "user
    # already exists" disclosure beyond the 409 itself.
    existing = await session.scalar(
        select(User.user_id).where(User.email == payload.email)
    )
    if existing is not None:
        raise ConflictError("A user with that email already exists.")

    role = await session.scalar(
        select(Role).where(Role.role_name == payload.role_name.value)
    )
    if role is None:
        raise NotFoundError(f"Role {payload.role_name.value} is not seeded.")

    temp_password = _generate_temp_password()

    # Create the auth identity FIRST. If this fails we raise (502) before writing
    # any row, so we never persist a user without a matching auth account.
    auth_user_id = await create_auth_user(payload.email, temp_password)

    # The auth identity now exists with a known temp password. If the DB write
    # below fails for ANY reason, that identity would be orphaned — so we
    # compensate by best-effort deleting it, then re-raise the original error.
    try:
        user = User(
            auth_user_id=auth_user_id,
            email=payload.email,
            first_name=payload.first_name,
            last_name=payload.last_name,
            active=True,
            # New users start on a one-time temp password and must set their own
            # on first login (cleared via POST /auth/password/complete).
            must_change_password=True,
        )
        session.add(user)
        await session.flush()  # populate user.user_id for the role link + audit

        session.add(UserRole(user_id=user.user_id, role_id=role.role_id))
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="create_user",
                entity_type="user",
                entity_id=user.user_id,
                field_name="email",
                # The email is the safe identifier to record; the password is
                # NEVER stored or audited.
                new_value=payload.email,
            )
        )
        await session.commit()
    except Exception:
        # Compensating delete of the just-created auth user so we don't leave an
        # orphaned identity with a known temp password. Best-effort: if cleanup
        # also fails, log the orphaned UUID at ERROR (never the password) so it
        # can be reconciled manually, then re-raise the ORIGINAL error.
        try:
            await delete_auth_user(auth_user_id)
        except Exception:
            logger.error(
                "Orphaned Supabase auth user %s: DB write failed and the "
                "compensating delete also failed; reconcile manually.",
                auth_user_id,
            )
        raise

    created = await _load_user(session, user.user_id)
    return CreateUserResponse(
        user_id=created.user_id,
        email=created.email,
        first_name=created.first_name,
        last_name=created.last_name,
        active=created.active,
        roles=[r.role_name for r in created.roles],
        temp_password=temp_password,
    )


@router.patch("/users/{user_id}/name")
async def update_user_name(
    user_id: IdPath,
    payload: UpdateUserNameRequest,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Edit a user's first/last name. super_admin only.

    Only fields present in the body are applied (``exclude_unset``); each field
    that actually changes is audited separately (``update_user``; ``field_name``
    = ``first_name``/``last_name``; old + new value). A no-op (same value, or no
    fields sent) is idempotent and not audited. 404 if the user doesn't exist.
    """
    user = await _load_user(session, user_id)

    changes = payload.model_dump(exclude_unset=True)
    audited = False
    for field_name in ("first_name", "last_name"):
        if field_name not in changes:
            continue
        new_value = changes[field_name]
        old_value = getattr(user, field_name)
        if old_value == new_value:
            continue
        setattr(user, field_name, new_value)
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="update_user",
                entity_type="user",
                entity_id=user_id,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )
        )
        audited = True

    if audited:
        await session.commit()
    return _serialize(await _load_user(session, user_id))

"""User administration routes (super_admin only).

Scope: list provisioned users, manage their roles on EXISTING accounts, reset a
user's password, edit a user's name, and create a brand-new login user. Creating
a user provisions a Supabase *auth* identity over the Admin API (service-role
key, server-side only) and returns a one-time temporary password exactly once —
the same security posture as the password-reset flow (see docs/PRE-LAUNCH.md).
"""

import datetime
import logging
import re
import secrets
import unicodedata
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies.auth import RequireEngineer, RequireSuperAdmin
from app.core.database import get_session
from app.core.errors import ConflictError, NotFoundError
from app.core.rate_limit import (
    AssignRoleRateLimit,
    CreateUserRateLimit,
    DeleteUserRateLimit,
    ResetPasswordRateLimit,
)
from app.core.roles import RoleName
from app.core.security import AuthorizationError
from app.models.audit import AuditLog
from app.models.login_attempt import LoginAttempt
from app.models.login_event import LoginEvent
from app.models.user import Role, User, UserRole
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
    """Provision a new login user. ``role_name`` is restricted to the
    non-privileged roles (full_access / student / view_only) — the top roles
    (engineer, super_admin) are NOT bootstrappable here — so an unknown or
    disallowed role is a 422 before any query runs; names follow the alumni NAME
    rules (≤100 chars). ``extra='forbid'`` rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    first_name: str | None = None
    last_name: str | None = None
    # The top roles (engineer, super_admin) must NOT be bootstrappable via
    # account creation — they can only be granted to an EXISTING user through
    # the assign-role endpoint. Restrict the create payload to the non-privileged
    # roles (full_access / student / view_only); anything else (incl. engineer
    # and super_admin) is a clean 422.
    role_name: Literal[
        RoleName.FULL_ACCESS, RoleName.STUDENT, RoleName.VIEW_ONLY
    ] = RoleName.VIEW_ONLY

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


async def _load_user(session: AsyncSession, user_id: int) -> User:
    user = await session.scalar(
        select(User).options(selectinload(User.roles)).where(User.user_id == user_id)
    )
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


# ``{user_id}`` is declared ``int`` (below), so string sub-paths like
# ``/users/{user_id}/name`` are unambiguous and never shadowed by this route —
# a non-numeric segment can't match an int path param.
@router.patch("/users/{user_id}")
async def set_user_active(
    user_id: int,
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
    user_id: int,
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
      * Privilege ceiling: only an engineer may delete a user who holds the
        engineer role.
      * Last-holder guard: you cannot delete the final holder of a top role
        (super_admin / engineer), which would lock administration out for
        everyone.

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

    # Privilege ceiling: the engineer tier is managed exclusively by engineers.
    if RoleName.ENGINEER.value in target_roles and not actor.is_engineer:
        raise AuthorizationError("Only an engineer can delete an engineer.")

    # System-wide last-holder guard: never delete the final holder of a top role,
    # which would lock user (or engineer-only vocab/database) administration out
    # of the system for everyone.
    for top_role in (RoleName.SUPER_ADMIN.value, RoleName.ENGINEER.value):
        if top_role in target_roles:
            role = await session.scalar(
                select(Role).where(Role.role_name == top_role)
            )
            holders = await session.scalar(
                select(func.count())
                .select_from(UserRole)
                .where(UserRole.role_id == role.role_id)
            )
            if (holders or 0) <= 1:
                raise ConflictError(f"Cannot delete the last {top_role}.")

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
    user_id: int,
    payload: RoleAssign,
    actor: AssignRoleRateLimit,
    session: SessionDep,
) -> dict:
    """Grant a role to an existing user (idempotent). super_admin and up.

    Rate-limited per actor (best-effort, in-process) to brake bulk privilege
    changes. Privilege ceiling: only an ``engineer`` may grant the ``engineer``
    role. A
    ``super_admin`` (who is below engineer) cannot mint an account that outranks
    them — that would be a privilege escalation above the actor's own ceiling.
    """
    if payload.role_name == RoleName.ENGINEER and not actor.is_engineer:
        raise AuthorizationError(
            "Only an engineer can grant the engineer role."
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
    return _serialize(await _load_user(session, user_id))


@router.delete("/users/{user_id}/roles/{role_name}")
async def remove_role(
    user_id: int,
    role_name: RoleName,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Revoke a role from an existing user (idempotent). super_admin and up.

    Privilege ceiling (symmetric with assign_role): only an ``engineer`` may
    remove the ``engineer`` role. A ``super_admin`` cannot demote an engineer —
    the engineer tier is managed exclusively by engineers.

    Guards against an admin removing their OWN top role (super_admin or
    engineer), which would lock user administration (or, for engineer, vocab /
    database administration) out of the system if they were the last holder.
    """
    if role_name == RoleName.ENGINEER and not actor.is_engineer:
        raise AuthorizationError(
            "Only an engineer can remove the engineer role."
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
    user_id: int,
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
    user_id: int,
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

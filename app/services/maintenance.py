"""Site-wide maintenance mode.

ONE engineer-only switch that (a) force-logs-out everyone currently signed in,
(b) refuses new sign-ins and every authenticated request, and (c) drives the
public maintenance page. Reversible from the engineer console.

-------------------------------------------------------------------------------
THE LOCKOUT PROBLEM, AND HOW THIS AVOIDS IT
-------------------------------------------------------------------------------
A maintenance switch that pauses *all* logins bricks the site: nobody can sign
in, so nobody can turn it off, and recovery means hand-editing the production
database. Three things prevent that here, deliberately layered so that no single
one of them has to hold:

  1. ENGINEERS ARE EXEMPT. ``is_exempt()`` is the single definition, used by the
     login route, the per-request gate, and the console. A user holding the
     ``engineer`` role signs in and browses normally while maintenance is on.
     Engineer is also the role that owns the disable endpoint (``RequireEngineer``),
     so the exempt set and the "can turn it off" set are the SAME set by
     construction — there is no way to be exempt-but-powerless or
     powerful-but-locked-out.

  2. ENGINEERS ARE NOT FORCE-LOGGED-OUT. ``enable()`` invalidates the sessions of
     every NON-engineer account only. The engineer who presses the button keeps
     their session, so the "Turn off maintenance mode" control is reachable
     immediately, with no re-authentication step in between. This is the
     deliberate answer to "does force-logout include the engineer who pressed
     it?" — it does not. Logging them out would add a re-login round trip to the
     recovery path for zero benefit (they are exempt from the pause anyway), and
     would make the recovery depend on Supabase Auth being healthy at exactly the
     moment an engineer is doing maintenance.

  3. THE GATE FAILS OPEN. If the maintenance row cannot be read (table missing,
     DB hiccup, migration not yet applied), ``read_status`` reports DISABLED
     rather than raising. A failure to read the switch can therefore never lock
     anyone out. Combined with (1), the exemption is checked BEFORE any database
     read is attempted, so an engineer's access does not depend on this table
     being readable at all.

-------------------------------------------------------------------------------
HOW FORCE-LOGOUT WORKS
-------------------------------------------------------------------------------
It reuses the single-active-session machinery (#147) rather than inventing a
second mechanism. ``_enforce_single_session`` rejects a request when the
account's ``users.active_session_id`` is set and differs from the session id in
the caller's token.

NOTE THE DIRECTION: clearing ``active_session_id`` to NULL does NOT log anyone
out — that guard FAILS OPEN on NULL, so clearing it would actually *restore*
superseded sessions. Instead ``enable()`` stamps every signed-in non-engineer
account with an opaque SENTINEL (``maintenance:<uuid4>``) that no Supabase
session id can ever equal. Every live token then mismatches, so every data
route returns 401 / ``session_superseded``, and the frontend's SessionGuard poll
signs the device out on its own within ~20s. No new enforcement code, no second
source of truth.

Only rows that ALREADY have a non-null ``active_session_id`` are stamped. Rows
sitting at NULL are left alone on purpose: NULL is the "fails open" state, and
moving an account out of it would make that account permanently dependent on the
best-effort ``POST /auth/login`` claim (the known-flaky path in #188) to ever
work again. Those accounts are still fully paused — the per-request gate below
blocks them regardless of session state.

``disable()`` does NOT clear the sentinels. Ending maintenance must not resurrect
the sessions that were killed (and clearing to NULL would resurrect genuinely
superseded ones too); everyone simply signs in again, which reclaims the row.
"""

from __future__ import annotations

import datetime
import logging
import time
import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.roles import RoleName
from app.models.audit import AuditLog
from app.models.maintenance import MaintenanceMode
from app.models.user import Role, User, UserRole
from app.schemas.maintenance import (
    MaintenanceEnableResult,
    MaintenanceState,
    MaintenanceStatus,
)

logger = logging.getLogger(__name__)

#: Public copy used when the engineer does not supply their own message. Says
#: the site is unavailable and nothing else — no cause, no ETA, no internals.
DEFAULT_MESSAGE = (
    "The site is temporarily unavailable for scheduled maintenance. "
    "Please check back soon."
)

#: Client-safe refusal shown when a paused user tries to sign in or call the
#: API. Identical for every non-exempt account, so it reveals nothing about the
#: account itself.
REFUSAL_MESSAGE = DEFAULT_MESSAGE

#: Prefix of the opaque value written to ``users.active_session_id`` to
#: invalidate a live session. A Supabase session id is a bare UUID, so a
#: prefixed value can never collide with a real one.
SESSION_SENTINEL_PREFIX = "maintenance:"

#: The row is a singleton pinned to id 1 (CHECK constraint in the migration).
_ROW_ID = 1

#: How long a read of the switch is reused within one process. Serverless
#: instances each hold their own, so a change takes effect everywhere within
#: this many seconds. Keeps the gate off the hot path of every request while
#: staying responsive enough that turning maintenance OFF is felt immediately.
_CACHE_TTL_SECONDS = 5.0

# (monotonic timestamp, status) or None. Module-level by design: it is a pure
# read-through cache of a single boolean and is safe to lose at any time.
_cached: tuple[float, MaintenanceStatus] | None = None


def is_exempt(roles: list[str] | set[str] | tuple[str, ...]) -> bool:
    """Whether ``roles`` may act while maintenance mode is on.

    THE SINGLE DEFINITION of the exempt set — the login route, the per-request
    gate, and the console all call this, so they can never disagree.

    Exempt = holds the ``engineer`` role, and nothing else. Not super_admin:
    only an engineer can reach the disable endpoint (``RequireEngineer``), so
    exempting a role that cannot turn maintenance off would grant access without
    granting recovery, which is the failure mode this control exists to avoid.
    """
    return RoleName.ENGINEER.value in set(roles)


def _new_sentinel() -> str:
    """A session-id value no real Supabase session can equal.

    Fresh per activation so a second maintenance window can never reuse a value
    that some stale token might match.
    """
    return f"{SESSION_SENTINEL_PREFIX}{uuid.uuid4()}"


def reset_cache() -> None:
    """Drop the process-local cache (used by ``enable``/``disable`` and tests)."""
    global _cached
    _cached = None


def _remember(status: MaintenanceStatus) -> MaintenanceStatus:
    global _cached
    _cached = (time.monotonic(), status)
    return status


async def _load_status(session: AsyncSession) -> MaintenanceStatus:
    """Read the switch straight from the database (no cache)."""
    row = await session.scalar(
        select(MaintenanceMode).where(MaintenanceMode.id == _ROW_ID)
    )
    if row is None or not row.enabled:
        return MaintenanceStatus(enabled=False, message=None)
    return MaintenanceStatus(enabled=True, message=row.message or DEFAULT_MESSAGE)


async def read_status(session: AsyncSession) -> MaintenanceStatus:
    """The public switch state, cached for ``_CACHE_TTL_SECONDS`` per process.

    NEVER RAISES. If the row cannot be read — the migration has not been applied
    yet, the table is missing, the database blipped — this returns the last value
    it successfully read, or DISABLED if it has never read one. A control that
    can hide the whole application must not be driven by an unreadable value.

    So it fails OPEN from a cold start (unreadable and never read => site is up),
    and STICKY otherwise (an error mid-window keeps the window closed rather than
    flapping the site open for a few seconds). Neither direction can strand an
    engineer: the gate exempts them before it ever calls this.
    """
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
        return _cached[1]
    try:
        return _remember(await _load_status(session))
    except Exception:
        logger.warning("Could not read maintenance_mode; treating as OFF", exc_info=True)
        if _cached is not None:
            return _cached[1]
        return MaintenanceStatus(enabled=False, message=None)


async def get_state(session: AsyncSession) -> MaintenanceState:
    """The full engineer-console view (uncached — the console wants the truth).

    Carries the actor and timestamp, so this must only ever be returned from an
    engineer-gated route. The public route returns ``MaintenanceStatus``.
    """
    row = await session.scalar(
        select(MaintenanceMode).where(MaintenanceMode.id == _ROW_ID)
    )
    if row is None:
        return MaintenanceState(enabled=False, message=None)

    email: str | None = None
    if row.enabled_by_user_id is not None:
        email = await session.scalar(
            select(User.email).where(User.user_id == row.enabled_by_user_id)
        )
    return MaintenanceState(
        enabled=row.enabled,
        message=(row.message or DEFAULT_MESSAGE) if row.enabled else row.message,
        enabled_at=row.enabled_at,
        enabled_by_email=email,
    )


def _engineer_user_ids():
    """Subquery of every user id holding the ``engineer`` role."""
    return (
        select(UserRole.user_id)
        .join(Role, Role.role_id == UserRole.role_id)
        .where(Role.role_name == RoleName.ENGINEER.value)
    )


async def _upsert_row(session: AsyncSession) -> MaintenanceMode:
    row = await session.scalar(
        select(MaintenanceMode).where(MaintenanceMode.id == _ROW_ID)
    )
    if row is None:
        row = MaintenanceMode(id=_ROW_ID)
        session.add(row)
    return row


async def enable(
    session: AsyncSession,
    *,
    actor_user_id: int,
    message: str | None = None,
) -> MaintenanceEnableResult:
    """Turn maintenance mode ON: pause access and end every non-engineer session.

    Engineers keep their sessions and keep their access (see the module
    docstring), so the engineer who runs this can turn it back off without
    signing in again.

    Audited as ``maintenance_mode_enabled`` with the acting user. As with every
    other engineer action, the audit layer reroutes an engineer's ``AuditLog``
    to ``engineer_action_log`` (#199) — the actor is recorded either way.
    """
    now = datetime.datetime.now(datetime.UTC)
    sentinel = _new_sentinel()

    row = await _upsert_row(session)
    row.enabled = True
    row.message = (message or "").strip() or None
    row.enabled_at = now
    row.enabled_by_user_id = actor_user_id

    # Force-logout: stamp a value no live token can match. Restricted to rows
    # that already hold a session (see the module docstring) and to
    # non-engineers, so the acting engineer — and any other engineer — stays in.
    result = await session.execute(
        update(User)
        .where(
            User.active_session_id.is_not(None),
            User.user_id.not_in(_engineer_user_ids()),
        )
        .values(active_session_id=sentinel, active_session_at=now)
        .execution_options(synchronize_session=False)
    )
    sessions_ended = int(result.rowcount or 0)

    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="maintenance_mode_enabled",
            entity_type="maintenance_mode",
            entity_id=None,
            field_name="enabled",
            old_value="false",
            new_value=f"true sessions_ended={sessions_ended}",
        )
    )
    await session.commit()

    _remember(MaintenanceStatus(enabled=True, message=row.message or DEFAULT_MESSAGE))
    return MaintenanceEnableResult(
        enabled=True,
        message=row.message or DEFAULT_MESSAGE,
        enabled_at=now,
        enabled_by_email=None,
        sessions_ended=sessions_ended,
    )


async def disable(
    session: AsyncSession, *, actor_user_id: int
) -> MaintenanceState:
    """Turn maintenance mode OFF and restore normal logins.

    Sessions ended by ``enable`` are NOT restored — the sentinel stays, so those
    users sign in again (which reclaims their row). Restoring them would also
    resurrect genuinely superseded sessions, which would break #147.

    Audited as ``maintenance_mode_disabled`` with the acting user.
    """
    row = await _upsert_row(session)
    was_enabled = bool(row.enabled)
    row.enabled = False
    row.message = None
    row.enabled_at = None
    row.enabled_by_user_id = actor_user_id

    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="maintenance_mode_disabled",
            entity_type="maintenance_mode",
            entity_id=None,
            field_name="enabled",
            old_value="true" if was_enabled else "false",
            new_value="false",
        )
    )
    await session.commit()

    # Publish the OFF state to this process immediately; other instances pick it
    # up within _CACHE_TTL_SECONDS.
    _remember(MaintenanceStatus(enabled=False, message=None))
    return MaintenanceState(enabled=False, message=None)

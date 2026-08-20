"""Live Supabase sessions: read them, and end them for real.

Backs the engineer console's Sessions screen (list) and its revoke controls.

-------------------------------------------------------------------------------
WHY THIS EXISTS
-------------------------------------------------------------------------------
Supabase sessions are long-lived by default (up to 400 days) and this app's idle
timeout is browser-memory only (#684, investigated and deliberately left as-is).
A session opened weeks ago is therefore still a live credential, and until now
the only way to SEE one was to query ``auth.sessions`` by hand against the
production database — and the only way to END one was to write a DELETE. This
module makes both a console action instead of a hand-written statement.

-------------------------------------------------------------------------------
REVOCATION HAS TWO HALVES, AND ONE HALF IS NOT ENOUGH
-------------------------------------------------------------------------------
A signed-in device holds two credentials:

  * a short-lived ACCESS TOKEN (a JWT, ~1 hour) that this API verifies by
    SIGNATURE — nothing is looked up in Supabase to accept it; and
  * a long-lived REFRESH TOKEN, which Supabase exchanges for a fresh access
    token roughly forever.

So the two halves are:

  1. THE SUPABASE HALF — delete the ``auth.sessions`` row. ``auth.refresh_tokens``
     is ``ON DELETE CASCADE`` on ``session_id`` (verified against the live
     schema), so the refresh token dies with it and no NEW access token can ever
     be minted for that session.

     ALONE, THIS IS NOT ENOUGH. Supabase documents it plainly: "Access Tokens of
     revoked sessions remain valid until their expiry time, encoded in the exp
     claim. The user won't be immediately logged out." Deleting only the row
     leaves the device with a signature-valid access token that this API — and
     Supabase Storage, and PostgREST — keep accepting for up to another hour.
     For a revoke that exists to cut off a suspected intruder, an hour is the
     whole event.

  2. OUR HALF — stamp ``users.active_session_id`` with a sentinel, reusing the
     single-active-session machinery (#147) exactly as maintenance mode does.
     ``_enforce_single_session`` in app/api/dependencies/auth.py rejects any
     request whose token ``session_id`` differs from the account's stored one, so
     the stamp invalidates the outstanding access token on the VERY NEXT request,
     with no waiting for expiry. The frontend's SessionGuard poll then signs the
     device out on its own within ~20s.

     ALONE, THIS IS ALSO NOT ENOUGH. It is an application-layer rule, and it is
     not durable: the Supabase session and its refresh token survive, so (a) the
     holder can keep minting fresh access tokens and use them against Supabase
     Storage directly, which never consults our ``users`` table, and (b) the next
     genuine sign-in by the real owner overwrites the sentinel via
     ``POST /auth/login``, at which point the surviving stolen session — same
     ``session_id``, still refreshable — is accepted again. Our half buys
     immediacy; only the Supabase half makes it stick.

Both halves run in ONE transaction (the ``auth.sessions`` DELETE and the
``users`` UPDATE are issued on the same AsyncSession and committed together), so
a revoke can never half-apply.

-------------------------------------------------------------------------------
WHY RAW SQL AND NOT THE AUTH ADMIN API
-------------------------------------------------------------------------------
There is no admin endpoint for this. GoTrue's admin sign-out
(``supabase.auth.admin.signOut``) is keyed on the USER'S OWN access token JWT,
which a console operator does not have and must never have, and there is no
admin endpoint that lists sessions at all. Reading and deleting
``auth.sessions`` over the existing database connection is the only mechanism
available; ``app/services/supabase_admin.py`` is left for the calls the Admin API
genuinely serves (create / delete user, set password).

The application's database role owns these tables' privileges already (verified:
SELECT and DELETE on ``auth.sessions`` and ``auth.refresh_tokens``). A read that
fails on privileges is surfaced as a ServiceError rather than a 500, so a
misconfigured environment produces a legible message instead of a stack trace.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ServiceError

logger = logging.getLogger(__name__)

#: Prefix of the opaque value written to ``users.active_session_id`` to
#: invalidate whatever session the account currently holds. A Supabase session
#: id is a bare UUID, so a prefixed value can never collide with a real one —
#: the same trick, and the same guarantee, as maintenance mode's
#: ``maintenance:`` sentinel (see app/services/maintenance.py).
SESSION_SENTINEL_PREFIX = "revoked:"


def new_sentinel() -> str:
    """A session-id value no real Supabase session can equal.

    Fresh per revoke so a later revoke can never reuse a value some stale token
    might match.
    """
    return f"{SESSION_SENTINEL_PREFIX}{uuid.uuid4()}"


# --- reads -------------------------------------------------------------------

# One row per LIVE session. Expired rows are excluded (``not_after``): GoTrue
# reaps them lazily, so without the filter the screen would show sessions that
# already grant nothing and bury the ones that matter.
#
# ``last_active_at`` is GREATEST of the three timestamps GoTrue maintains —
# ``refreshed_at`` is only written on a token refresh (and is a naive UTC
# ``timestamp``, hence the cast), ``updated_at`` moves on other session activity,
# and ``created_at`` is the floor for a session that has done neither. GREATEST
# ignores NULLs in Postgres, so a never-refreshed session reports its creation
# time rather than null.
#
# The join to ``public.users`` is a LEFT JOIN on purpose: a Supabase auth
# identity with no application user row cannot use the app, but a LIVE session on
# one is exactly the anomaly this screen exists to surface, so it is shown (with
# no roles) rather than filtered away.
_SQL_LIST = text(
    """
    SELECT
        s.id                                        AS session_id,
        s.created_at                                AS created_at,
        GREATEST(
            s.created_at,
            s.updated_at,
            s.refreshed_at AT TIME ZONE 'UTC'
        )                                           AS last_active_at,
        s.refreshed_at AT TIME ZONE 'UTC'           AS refreshed_at,
        s.not_after                                 AS not_after,
        u.user_id                                   AS user_id,
        COALESCE(u.email, au.email)                 AS email,
        COALESCE(u.active, false)                   AS account_active,
        COALESCE(u.active_session_id = s.id::text, false)
                                                    AS is_account_active_session,
        COALESCE(
            (
                SELECT array_agg(r.role_name ORDER BY r.role_name)
                  FROM public.user_roles ur
                  JOIN public.roles r ON r.role_id = ur.role_id
                 WHERE ur.user_id = u.user_id
            ),
            ARRAY[]::varchar[]
        )                                           AS roles
      FROM auth.sessions s
      LEFT JOIN public.users u ON u.auth_user_id = s.user_id
      LEFT JOIN auth.users au ON au.id = s.user_id
     WHERE s.not_after IS NULL OR s.not_after > now()
     ORDER BY s.created_at ASC, s.id ASC
     LIMIT :limit OFFSET :offset
    """
)

# Same liveness filter as the listing, so ``total`` and the page agree.
_SQL_COUNT = text(
    """
    SELECT count(*)
      FROM auth.sessions s
     WHERE s.not_after IS NULL OR s.not_after > now()
    """
)


async def list_active(
    session: AsyncSession, *, limit: int, offset: int
) -> tuple[list[dict], int]:
    """Return one dict per live session (oldest first) and the total count.

    OLDEST FIRST is deliberate and is the whole point of the screen: the row that
    matters is the one that has been open for five weeks, so it sits at the top
    instead of being paged past. The neighbouring engineer logs are newest-first
    because they are histories; this is a live inventory.

    Raises ServiceError (502) if ``auth.sessions`` cannot be read, so a database
    role without the grant produces a legible message rather than a 500.
    """
    try:
        total = await session.scalar(_SQL_COUNT)
        rows = await session.execute(
            _SQL_LIST, {"limit": limit, "offset": offset}
        )
        mappings = [dict(m) for m in rows.mappings().all()]
    except SQLAlchemyError as exc:
        # Log the exception type/message only — never the row contents.
        logger.error("Could not read auth.sessions", exc_info=True)
        raise ServiceError(
            "Active sessions are unavailable: the authentication session store "
            "could not be read."
        ) from exc
    return mappings, int(total or 0)


# --- revocation --------------------------------------------------------------

# Deleting the session row cascades to auth.refresh_tokens (and
# auth.mfa_amr_claims) via ON DELETE CASCADE, so the refresh token dies with it.
# RETURNING tells the caller whether a row actually existed AND which auth
# identity it belonged to, in one round trip.
_SQL_DELETE_ONE = text(
    "DELETE FROM auth.sessions WHERE id = :session_id RETURNING user_id"
)

_SQL_DELETE_FOR_USER = text(
    "DELETE FROM auth.sessions WHERE user_id = :auth_user_id RETURNING id"
)


async def delete_supabase_session(
    session: AsyncSession, session_id: uuid.UUID
) -> uuid.UUID | None:
    """Delete ONE ``auth.sessions`` row. Returns its auth user id, or None if the
    row was already gone. Does NOT commit — the caller owns the transaction, so
    this lands atomically with our half.
    """
    try:
        row = (
            await session.execute(_SQL_DELETE_ONE, {"session_id": session_id})
        ).first()
    except SQLAlchemyError as exc:
        logger.error("Could not delete an auth.sessions row", exc_info=True)
        raise ServiceError(
            "The session could not be revoked: the authentication session store "
            "could not be written."
        ) from exc
    return row[0] if row else None


async def delete_supabase_sessions_for_user(
    session: AsyncSession, auth_user_id: uuid.UUID
) -> int:
    """Delete EVERY ``auth.sessions`` row for one auth identity; returns the
    count. Does NOT commit — see ``delete_supabase_session``.
    """
    try:
        rows = (
            await session.execute(
                _SQL_DELETE_FOR_USER, {"auth_user_id": auth_user_id}
            )
        ).all()
    except SQLAlchemyError as exc:
        logger.error("Could not delete auth.sessions rows", exc_info=True)
        raise ServiceError(
            "The sessions could not be revoked: the authentication session store "
            "could not be written."
        ) from exc
    return len(rows)


def should_stamp_sentinel(
    *, active_session_id: str | None, revoked_session_id: str
) -> bool:
    """Whether revoking ``revoked_session_id`` must also stamp our sentinel.

    ``users.active_session_id`` is a per-ACCOUNT switch, not a per-session one,
    so stamping it ends every outstanding access token on the account — not just
    the revoked session's. That is only ever the right thing to do when the
    revoked session is (or might be) the one this API is currently honouring:

      * IT MATCHES the account's active session -> stamp. This is the live one;
        without the stamp its access token keeps working until expiry.
      * The account has NO claimed session (NULL) -> stamp. NULL is the
        deliberate fail-OPEN state of #147, so with no stamp EVERY token on the
        account is accepted, including the revoked session's.
      * It matches NEITHER -> do NOT stamp. The account has since claimed a
        different session, so #147 is already rejecting the revoked one; stamping
        anyway would sign the user out of the session they are legitimately using
        right now, which is not what "revoke that old device" asked for.

    Revoke-ALL does not consult this — it always stamps, because ending every
    session on the account is precisely what it was asked to do.
    """
    if active_session_id is None:
        return True
    return active_session_id == revoked_session_id


def age_seconds(created_at: datetime.datetime, now: datetime.datetime) -> int:
    """Whole seconds between ``created_at`` and ``now`` (never negative)."""
    return max(0, int((now - created_at).total_seconds()))

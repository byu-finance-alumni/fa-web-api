"""Expire a session that nobody has touched for 24 hours (#684).

-------------------------------------------------------------------------------
THE GAP THIS CLOSES
-------------------------------------------------------------------------------
The app HAD an idle timeout: ``SessionTimeout``, a client component holding a
timer in browser memory. It works while a tab is open and is worth keeping --
it warns, it counts down, it syncs across tabs. But it lives in the tab. Close
a laptop and the tab dies with the timer; restore it a week later and the timer
starts again at zero, having never had a chance to fire. Nothing on the server
made up the difference: Supabase sessions run up to 400 days, and
``GET /auth/session/active`` only ever asked "has another login superseded this
one?" (#147), never "how long has this been sitting there?".

So a session opened weeks ago stayed a live credential and the only thing
between a left-open laptop and the data was the OS lock screen. #684 is exactly
that report: "still signed in after a full laptop restart".

-------------------------------------------------------------------------------
WHY WE STAMP OUR OWN TIMESTAMP INSTEAD OF READING auth.sessions
-------------------------------------------------------------------------------
``app/services/auth_sessions.py`` already derives ``last_active_at`` as
GREATEST(created_at, updated_at, refreshed_at), and reusing it here would cost
nothing. It would also not work.

``refreshed_at`` moves on every TOKEN REFRESH. After 24h idle the access token
(~1h) is long dead, so restoring the tab makes the Supabase client mint a new
one immediately -- before this API is asked anything at all. By the time a
resolver could read the row it reports a session seconds old. GoTrue's
timestamps measure the CLIENT's liveness; only a stamp written while serving an
authenticated request measures the USER's.

-------------------------------------------------------------------------------
EXPIRY REUSES REVOKE RATHER THAN INVENTING A THIRD REJECTION
-------------------------------------------------------------------------------
When a session goes idle we run the same two halves the engineer console's
revoke does (see auth_sessions.py, which explains at length why one half is not
enough): stamp ``users.active_session_id`` with a sentinel, AND delete the
``auth.sessions`` row so the refresh token dies with it.

That choice is what keeps this small. The single-active-session guard already
rejects a token whose ``session_id`` does not match the stored one, so an
expired session is refused on the very next data route with no new error type;
``GET /auth/session/active`` already returns ``{active: false}`` for a
mismatch, so the frontend's existing ``SessionGuard`` poll signs the device out
within ~20s, with its existing message and its existing ``?next=`` handling. No
frontend change is needed for this to work end to end.

-------------------------------------------------------------------------------
⚠️ NULL IS FRESH, NOT INFINITELY IDLE
-------------------------------------------------------------------------------
Every session alive when this ships predates the column. Reading NULL as "never
seen, therefore idle" would sign out the entire department on their first
request after deploy. NULL means "not yet stamped": the resolver writes now()
and lets the request through, so the 24h clock starts at first contact.

⚠️ THE THROTTLE IS NOT AN OPTIMISATION, IT IS THE REASON THIS IS AFFORDABLE.
The resolver runs on EVERY authenticated request, including the
``/auth/session/active`` poll. Stamping unconditionally would mean an UPDATE and
a commit per request -- the exact mistake #182 removed from this same resolver
when it was clearing login_attempts on every call. One write a minute per
active user is enough to measure a 24h threshold to within a rounding error.
"""

from __future__ import annotations

import datetime

#: How long a session may go untouched before it is expired (Jake, 2026-08-22).
#: A working day, so someone who signs in Monday morning and comes back Tuesday
#: morning is asked to sign in again, but a lunch break costs nothing.
IDLE_LIMIT_SECONDS = 24 * 60 * 60

#: Don't rewrite the stamp more often than this. See the throttle note above.
#: Well under IDLE_LIMIT_SECONDS, so the measured idle time can never be more
#: than this much older than the truth.
TOUCH_THROTTLE_SECONDS = 60


def idle_seconds(last_seen: datetime.datetime | None, now: datetime.datetime) -> int | None:
    """Seconds since the session was last seen, or None if never stamped.

    Negative differences (a clock skew, or a stamp written by a host running
    slightly ahead) clamp to 0 rather than reading as a long-idle session --
    getting signed out because two machines disagree about the time would be
    indistinguishable from a bug, and erring toward "fresh" is the safe
    direction for a control whose failure mode is locking people out.
    """
    if last_seen is None:
        return None
    return max(0, int((now - last_seen).total_seconds()))


def is_expired(last_seen: datetime.datetime | None, now: datetime.datetime) -> bool:
    """Has this session been untouched past the limit?

    NULL is FRESH -- see the module note. A session that has never been stamped
    is one that predates this feature, not one that has been idle forever.
    """
    seconds = idle_seconds(last_seen, now)
    return seconds is not None and seconds >= IDLE_LIMIT_SECONDS


def should_touch(last_seen: datetime.datetime | None, now: datetime.datetime) -> bool:
    """Is the stamp stale enough to be worth an UPDATE on this request?

    True when never stamped (so the clock starts) or when the throttle window
    has passed. False otherwise, which is the common case and the whole point.
    """
    seconds = idle_seconds(last_seen, now)
    return seconds is None or seconds >= TOUCH_THROTTLE_SECONDS

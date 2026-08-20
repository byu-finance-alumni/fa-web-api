"""Automatic, self-expiring IP blocks on the login path (#457).

#456 shipped DETECTION for the 2026-08-19 campaigns and deliberately blocked
nothing — it told a human, who could then block the source at the edge. The edge
turned out not to exist: Vercel's rate limiting is a Pro feature and this account
is on Hobby. So the owner asked the obvious next question —

    "is there a way to block an ip if they try 5 different emails, that would
     have stopped these attacks way sooner"

— and this module is the answer. When one source crosses the SAME threshold that
already opens an incident and sends the Slack message, further login attempts
from that source are refused for an hour.

--------------------------------------------------------------------------------
THE THRESHOLD, AND WHY IT IS THE DETECTOR'S AND NOT A SECOND NUMBER
--------------------------------------------------------------------------------
There is no threshold constant in this module. The decision is
``login_abuse.is_abusive`` — eight distinct addresses OR thirty attempts inside
fifteen minutes — the same call the Slack alert and the engineer console's attack
table make. Three reasons, in increasing order of importance:

  1. FIVE AND EIGHT STOP THE SAME ATTACKS. Replaying the real prod numbers, the
     fifth distinct address arrives at attempt ~5 / ~9 / ~14 (Miami / Seattle /
     Romania) and the eighth at ~8 / ~15 / ~22. Choosing five instead of eight
     buys 17 fewer attempts out of 750 and at most 25 extra seconds of exposure,
     against a campaign in which nothing succeeded. That is the entire benefit.

  2. FIVE IS INSIDE THE HONEST RANGE AND EIGHT IS NOT. There are ~4 accounts.
     Four staff behind one office NAT each fumbling once is four distinct
     addresses; one of them mistyping their own address once makes five. One
     person who cannot remember which of their addresses is the account
     (``jake@``, ``gunnjake@``, ``jake.gunn@``, ``jgunn@``, ``jake@byu.ed``) is
     five on their own. The suite already encodes three staff × four failures as
     a must-not-fire, and eight was chosen with headroom over exactly that. At
     five, a bad Tuesday becomes an hour-long lockout of the whole office.

  3. TWO NUMBERS WOULD MEAN A SILENT BLOCK. At five, a source would be blocked
     BEFORE an incident row exists, before the Slack message, and before the
     Maintenance page's table calls it an attack — a refusal with nothing to look
     at and nothing to explain it. Sharing ``is_abusive`` means the block, the
     alert and the table are one decision, and retuning it moves all three in one
     edit. If five is still wanted after reading this, it is one line —
     ``login_abuse.SPRAY_MIN_DISTINCT_EMAILS`` — and it moves all three surfaces
     together, which is the point.

--------------------------------------------------------------------------------
THE SEVEN SAFETY PROPERTIES, AND WHERE EACH ONE LIVES
--------------------------------------------------------------------------------
A block is far more consequential than an alert: a misfiring alert costs one
Slack message, a misfiring block locks the department out of its own system. So
these matter more than the feature, and none of them is a convention — each is
either a clause Postgres evaluates or a branch with a test naming it.

 1. AN IP WITH A RECENT SUCCESSFUL LOGIN IS NEVER BLOCKED. ``ip_address`` is
    copied from ``login_failures``, which the frontend fills from the incoming
    request's ``x-forwarded-for`` — anyone calling this API directly can put
    anything there. Without this rule an attacker puts the OWNER'S address in
    that header, fails eight sign-ins, and locks the staff out: a failed attack
    converted into a successful denial of service, strictly worse than the attack
    this feature exists to stop. The exemption is a ``NOT EXISTS`` INSIDE the
    only statement that creates a block (:data:`_SQL_BLOCK`), so there is no code
    path that can write a block without it. It reads ``login_events``, which only
    a genuinely AUTHENTICATED caller can write (``POST /auth/login`` needs a
    valid Supabase token) — the shield is not forgeable by the party that can
    forge the block.

 2. BLOCKS AUTO-EXPIRE. ``blocked_until`` is a timestamp and every read carries
    ``AND blocked_until > now()``. A block lapses because time passed, not
    because anything ran: no cron, no sweep, no engineer. The column is NOT NULL
    and a CHECK constraint caps any block at 24 hours, so "permanent" is not
    representable. Default :data:`BLOCK_SECONDS` is one hour.

 3. IT FAILS OPEN. :func:`seconds_remaining` returns ``None`` — not blocked — on
    any exception: table missing, migration not applied, database blipped,
    timeout. Same reasoning as ``maintenance.read_status``: a failure to read a
    switch that can refuse people must never be what locks them out.

 4. ENGINEERS STAY ABLE TO SIGN IN. Same principle the maintenance switch is
    built on — the people who fix it must not be the people locked out by it —
    and layered the same way, so no single layer has to hold:
      (a) A SECOND ``NOT EXISTS`` in :data:`_SQL_BLOCK` exempts, with no time
          bound at all, any address an engineer has ever successfully signed in
          from. Property 1 already covers an engineer who signed in this month;
          this covers the one who has been away for six weeks, which is precisely
          when a forged-header attack would be worth attempting.
      (b) Blocks are consulted ONLY on the two unauthenticated pre-login routes.
          An engineer who is already signed in is untouched, so the console —
          including the lift endpoint below — is reachable from a blocked
          address.
      (c) An engineer can lift any block from any address (:func:`lift`), and a
          lifted source is not re-blocked for :data:`LIFT_GRACE_SECONDS`.
      (d) Everything expires in an hour regardless.
    Note what is deliberately NOT done: the block is never waived because the
    SUBMITTED EMAIL belongs to an engineer. That would make the response depend
    on the account, which is property 7 — see below.

 5. IT IS DURABLE, NOT IN-MEMORY. ``app/core/rate_limit.py`` is an in-memory
    fixed-window counter, so on Vercel each warm instance keeps its own and
    shares it with nobody — which is exactly why it never fired on 2026-08-19.
    The state is a Postgres row, following ``service_incidents`` /
    ``login_abuse_incidents``.

 6. IT IS SCOPED TO THE LOGIN PATH. Two call sites, both in
    ``app/api/routes/auth.py``: ``POST /auth/login/precheck`` and
    ``POST /auth/login/record``. There is no middleware and no global gate. The
    public survey — the only public page, used by alumni worldwide — is
    completely unaffected, and an over-broad block there would be far worse than
    the login block is good.

 7. THE REFUSAL IS NOT AN ENUMERATION ORACLE. The login routes return ONE generic
    shape whatever email you send (see ``login_lockout``'s anti-enumeration
    note), and a block must not become the exception. So a blocked caller gets
    the EXISTING ``cooldown`` refusal — ``{"allowed": false, "reason":
    "cooldown", "retry_after_seconds": N}`` — with ``N`` derived from the IP and
    from nothing else. It is byte-identical for a registered address and for one
    that has never existed, because the routes return it BEFORE they look the
    address up at all, so not even the query pattern differs. No new ``reason``
    string is introduced, which also means the deployed frontend needs no change
    to collapse it into the single generic message it already shows.

    A per-IP control is of course distinguishable from no control — the caller
    can tell they are being refused — but that is a fact about the CALLER, the
    same one the existing 429 exposes. It separates no account from any other.

--------------------------------------------------------------------------------
WHY BLOCKING DOES NOT DEPEND ON ALERTING BEING CONFIGURED
--------------------------------------------------------------------------------
``login_abuse`` returns before touching the database when no alert channel is
set. Blocking deliberately does NOT sit behind that gate. Alerting is
observability and blocking is protection, and wiring a security control to the
presence of a Slack webhook means rotating that webhook silently disables it —
the exact "a forgotten env var must never become silence about an attack" failure
the alerting module argues against. A block is never invisible even with no
channel configured: the row is in ``login_ip_blocks`` and on the engineer
console.

The kill switch is :attr:`Settings.login_auto_block_enabled` (env
``LOGIN_AUTO_BLOCK_ENABLED=false``), which turns creation AND enforcement off
together; existing rows simply stop being consulted.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.roles import RoleName

log = logging.getLogger(__name__)

# ------------------------------------------------------------------- policy --

#: How long one block lasts. An hour: long enough that a campaign gives up or
#: moves on, short enough that a false positive heals itself over a coffee
#: instead of over a support ticket. Nobody has to be woken up to undo it, and
#: nobody has to remember to. The database independently caps any block at 24
#: hours (``ck_login_ip_blocks_bounded``), so raising this by mistake cannot
#: produce a block that outlives the working day.
BLOCK_SECONDS = 3600

#: How far back a SUCCESSFUL sign-in from an address shields it (property 1).
#:
#: Thirty days, and the direction of the trade is deliberate. Too short and a
#: staff member who has been on holiday loses the shield exactly when a forged
#: header would be worth sending; too long and an address that once served a
#: legitimate login — a coffee-shop NAT, a recycled residential lease — is immune
#: forever. Both failure modes are not symmetric: a wrongly-shielded attacker
#: costs an alert instead of a block, which is where this project was an hour
#: ago, while a wrongly-blocked staff address costs the department its own
#: system. So this errs long.
SUCCESS_LOOKBACK_SECONDS = 30 * 24 * 3600

#: After an engineer lifts a block, that source is not automatically re-blocked
#: for this long. A lift means "this was wrong"; a heuristic that re-applies it
#: on the next failed login would make the console's lift button decorative and
#: would let a false positive persist through the one control meant to end it.
LIFT_GRACE_SECONDS = 24 * 3600

#: Time budget for the two statements. Both sit on unauthenticated public
#: routes, so they are short by design — better a missed block, or a missed
#: enforcement, than a request held open. Both time-outs fail OPEN.
_DB_TIMEOUT_SECONDS = 2.0


def blocking_enabled() -> bool:
    """Whether automatic blocking is armed at all (creation AND enforcement).

    The one kill switch. ``LOGIN_AUTO_BLOCK_ENABLED=false`` turns the feature off
    without a code change; existing rows stop being consulted rather than being
    deleted, so flipping it back does not resurrect anything (they will have
    expired).
    """
    return bool(getattr(get_settings(), "login_auto_block_enabled", True))


# --------------------------------------------------------------- statements --
#
# Raw statements rather than ORM writes, for the same reason as ``failure_alert``
# and ``login_abuse``: the guarantee is that POSTGRES evaluates these conditions.
# ``INSERT ... SELECT ... WHERE NOT EXISTS`` and ``ON CONFLICT ... DO UPDATE`` on
# a partial unique index are what make "the exemptions cannot be skipped" and
# "twenty instances produce one row" true, and neither survives being re-expressed
# as read-then-write in Python.

# Is this source blocked right now? One index probe on uq_login_ip_blocks_active.
#
# ⚠️ ``blocked_until > now()`` IS THE EXPIRY. It is in the read, not in a cleanup
# job, which is what makes property 2 hold even if every other part of this
# feature stops working.
_SQL_IS_BLOCKED = text(
    """
    SELECT ceil(extract(epoch FROM (blocked_until - now())))::int AS seconds_left
      FROM login_ip_blocks
     WHERE environment = :environment
       AND ip_address = :ip
       AND lifted_at IS NULL
       AND blocked_until > now()
     LIMIT 1
    """
)

# Open (or re-arm) the block for one source.
#
# ⚠️ THE TWO `NOT EXISTS` CLAUSES ARE THE SAFETY PROPERTIES. They are in the
# statement, not in a Python `if`, so no future caller and no partially-applied
# refactor can create a block without them:
#
#   * the first is "this address has signed in successfully lately" (property 1,
#     the anti-DoS shield against a forged x-forwarded-for);
#   * the second is "an engineer has EVER signed in from this address" (property
#     4, with no time bound, so the person who fixes it is never the person
#     locked out);
#   * the third is the lift grace — a human override outranks the heuristic.
#
# `login_events` is the shield's source and only an authenticated caller can
# write it, so the party who can forge `ip_address` cannot forge the exemption.
#
# ON CONFLICT re-arms the existing row rather than inserting a second one; the
# counters take GREATEST for the same reason `login_abuse`'s upsert does (the
# measurement is over a rolling window and the row is a high-water mark), and
# `blocked_until` takes GREATEST so a re-arm can only ever extend, never shorten,
# an expiry that is already in force.
_SQL_BLOCK = text(
    """
    INSERT INTO login_ip_blocks
        (environment, ip_address, blocked_at, blocked_until,
         attempt_count, distinct_email_count, pattern, abuse_incident_id)
    SELECT CAST(:environment AS varchar), CAST(:ip AS varchar), now(),
           now() + (CAST(:block_seconds AS int) * interval '1 second'),
           CAST(:attempts AS int), CAST(:distinct_emails AS int),
           CAST(:pattern AS varchar), CAST(:incident_id AS bigint)
     WHERE NOT EXISTS (
               SELECT 1 FROM login_events
                WHERE ip_address = :ip
                  AND occurred_at >= now()
                      - (CAST(:success_lookback_seconds AS int)
                         * interval '1 second')
           )
       AND NOT EXISTS (
               SELECT 1
                 FROM login_events le
                 JOIN user_roles ur ON ur.user_id = le.user_id
                 JOIN roles r ON r.role_id = ur.role_id
                WHERE le.ip_address = :ip
                  AND r.role_name = :engineer_role
           )
       AND NOT EXISTS (
               SELECT 1 FROM login_ip_blocks
                WHERE environment = :environment
                  AND ip_address = :ip
                  AND lifted_at IS NOT NULL
                  AND lifted_at >= now()
                      - (CAST(:lift_grace_seconds AS int)
                         * interval '1 second')
           )
    ON CONFLICT (environment, ip_address) WHERE lifted_at IS NULL
    DO UPDATE SET
        -- The re-arm restarts the CURRENT block period rather than extending an
        -- old one in place. Without this, `blocked_at` would stay at the first
        -- sighting and a campaign still being re-armed a day later would push
        -- `blocked_until` past `blocked_at + 24 hours` and trip
        -- ck_login_ip_blocks_bounded — the constraint would start rejecting the
        -- write instead of bounding it.
        blocked_at           = EXCLUDED.blocked_at,
        blocked_until        = GREATEST(login_ip_blocks.blocked_until,
                                        EXCLUDED.blocked_until),
        attempt_count        = GREATEST(login_ip_blocks.attempt_count,
                                        EXCLUDED.attempt_count),
        distinct_email_count = GREATEST(login_ip_blocks.distinct_email_count,
                                        EXCLUDED.distinct_email_count),
        pattern              = EXCLUDED.pattern,
        abuse_incident_id    = COALESCE(EXCLUDED.abuse_incident_id,
                                        login_ip_blocks.abuse_incident_id),
        updated_at           = now()
    RETURNING block_id, ip_address, blocked_at, blocked_until,
              attempt_count, distinct_email_count, pattern
    """
)

# Back-link a block to the incident that produced it, for the console. Guarded on
# the column still being NULL so a later re-arm cannot re-point an existing block
# at a different campaign.
_SQL_LINK_INCIDENT = text(
    """
    UPDATE login_ip_blocks
       SET abuse_incident_id = :incident_id,
           updated_at = now()
     WHERE block_id = :block_id
       AND abuse_incident_id IS NULL
    """
)

# The engineer console's list. Active blocks first, then recent history.
_SQL_LIST = text(
    """
    SELECT block_id, environment, ip_address, blocked_at, blocked_until,
           attempt_count, distinct_email_count, pattern, abuse_incident_id,
           lifted_at, lifted_by_user_id,
           (lifted_at IS NULL AND blocked_until > now()) AS active
      FROM login_ip_blocks
     WHERE environment = :environment
       AND (:active_only = false
            OR (lifted_at IS NULL AND blocked_until > now()))
     ORDER BY (lifted_at IS NULL AND blocked_until > now()) DESC,
              blocked_at DESC
     LIMIT :limit
    """
)

# Lift one block. Guarded on `lifted_at IS NULL` so a double-click cannot rewrite
# who lifted it, and RETURNING tells the route whether it actually did anything
# (404 vs 200) without a second read.
_SQL_LIFT = text(
    """
    UPDATE login_ip_blocks
       SET lifted_at = now(),
           lifted_by_user_id = :actor_id,
           updated_at = now()
     WHERE block_id = :block_id
       AND environment = :environment
       AND lifted_at IS NULL
    RETURNING block_id, ip_address, blocked_until
    """
)


# ------------------------------------------------------------- enforcement ---


async def seconds_remaining(
    session: AsyncSession, *, ip_address: str | None
) -> int | None:
    """Seconds this source is still blocked for, or ``None`` if it may proceed.

    ⚠️ NEVER RAISES, AND ``None`` IS THE SAFE ANSWER (property 3). Every failure
    mode — the table missing because the migration has not been applied, a
    database blip, a timeout, a session already poisoned by an earlier error —
    returns ``None``, i.e. "not blocked". A control that can refuse people must
    never refuse them because it could not be read; the same argument
    ``maintenance.read_status`` makes for the switch that can hide the whole
    site.

    ``None`` is also returned for a caller with no forwarded address at all.
    There is nothing to match a block against, and matching "unknown" would put
    every unattributed caller in one bucket.

    Deliberately UNCACHED, unlike ``maintenance.read_status``. That cache exists
    to keep a per-REQUEST gate off the hot path; this runs twice per sign-in
    attempt at most, and caching it would mean either serving a stale block after
    an engineer lifted it, or serving a block past its own expiry — both on the
    wrong side of "a false positive must heal itself".
    """
    if not blocking_enabled():
        return None
    ip = (ip_address or "").strip()
    if not ip:
        return None
    try:
        row = await asyncio.wait_for(
            _read_block(session, ip=ip[:64]), timeout=_DB_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 - unreadable store must never lock anyone out
        log.warning("login_block: could not read the block store", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    if row is None:
        return None
    # A row that reports zero or less is expiring as we read it; treat it as
    # over rather than as a block with no time left.
    seconds = int(row["seconds_left"] or 0)
    return seconds if seconds > 0 else None


async def _read_block(session: AsyncSession, *, ip: str) -> dict | None:
    result = await session.execute(
        _SQL_IS_BLOCKED,
        {"environment": get_settings().environment, "ip": ip},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------- creation ---


async def apply(
    session: AsyncSession,
    *,
    ip_address: str,
    attempts: int,
    distinct_emails: int,
    pattern: str,
    incident_id: int | None = None,
) -> dict | None:
    """Block ``ip_address`` for :data:`BLOCK_SECONDS`, unless it is exempt.

    Returns the block row when one was created or re-armed, and ``None`` when the
    source was exempt (a recent successful sign-in, an engineer's address, or a
    recent lift), when blocking is switched off, or when the write failed.

    THE CALLER DOES NOT DECIDE WHETHER TO BLOCK. It decides only that the source
    is abusive — via ``login_abuse.is_abusive``, the shared classifier — and the
    three exemptions are evaluated by Postgres inside :data:`_SQL_BLOCK`. That is
    on purpose: the exemptions are the safety properties, and a safety property
    expressed as a Python ``if`` in the caller is one refactor away from being
    dropped.

    Does NOT commit. It runs inside ``login_abuse.evaluate``'s transaction, which
    commits once, so a block and the incident row it belongs to land together.
    """
    if not blocking_enabled():
        return None
    ip = (ip_address or "").strip()
    if not ip:
        return None
    result = await session.execute(
        _SQL_BLOCK,
        {
            "environment": get_settings().environment,
            "ip": ip[:64],
            "block_seconds": BLOCK_SECONDS,
            "attempts": int(attempts),
            "distinct_emails": int(distinct_emails),
            "pattern": pattern,
            "incident_id": incident_id,
            "success_lookback_seconds": SUCCESS_LOOKBACK_SECONDS,
            "engineer_role": RoleName.ENGINEER.value,
            "lift_grace_seconds": LIFT_GRACE_SECONDS,
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def link_incident(
    session: AsyncSession, *, block_id: int, incident_id: int
) -> None:
    """Record which ``login_abuse_incidents`` row a block was opened alongside.

    Cosmetic — the console can already line the two up by IP — so it is
    deliberately the LAST thing done and never gates anything. It exists so that
    a reader looking at a block can jump straight to the campaign that caused it
    without eyeballing timestamps.
    """
    await session.execute(
        _SQL_LINK_INCIDENT,
        {"block_id": int(block_id), "incident_id": int(incident_id)},
    )


# ------------------------------------------------------- engineer console ---


async def list_blocks(
    session: AsyncSession, *, active_only: bool, limit: int
) -> list[dict]:
    """Blocks for this environment, active ones first. Read-only.

    Carries no attempted email address — only the counts — for the same reason
    the attack table and the Slack alert do not: those addresses are unverified
    strings a stranger typed, some belong to real people, and a list of them is
    an enumeration oracle for anything that reaches this response.
    """
    rows = (
        (
            await session.execute(
                _SQL_LIST,
                {
                    "environment": get_settings().environment,
                    "active_only": bool(active_only),
                    "limit": int(limit),
                },
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def lift(
    session: AsyncSession, *, block_id: int, actor_user_id: int
) -> dict | None:
    """Lift one block by hand. Returns the lifted row, or ``None`` if there was
    no ACTIVE block with that id (already lifted, or never existed).

    Does not commit — the route commits alongside its audit row.

    The lifted source is not automatically re-blocked for
    :data:`LIFT_GRACE_SECONDS` (enforced in :data:`_SQL_BLOCK`). Without that,
    an engineer clearing a false positive would watch it reappear on the next
    failed login and the lift control would be decorative.
    """
    row = (
        (
            await session.execute(
                _SQL_LIFT,
                {
                    "block_id": int(block_id),
                    "environment": get_settings().environment,
                    "actor_id": int(actor_user_id),
                },
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None

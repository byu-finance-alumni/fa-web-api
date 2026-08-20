"""Delete one login-abuse CAMPAIGN — everything one source IP left behind.

WHY THIS EXISTS. Proving the automatic block (#457) actually refuses people on
production meant driving real failed sign-ins at the real API, which is the only
honest way to test a control that reads a Postgres row. That left synthetic rows
in three tables — the per-attempt ``login_failures`` log, the
``login_abuse_incidents`` row the detector opened, and the ``login_ip_blocks``
row the block wrote. The owner wanted them gone from the CONSOLE rather than by
opening a psql session against production, which is the thing every other
control on these screens exists to avoid.

Generalised slightly beyond the one-off: the unit is a campaign — one source IP,
every table it touched — because that is the unit the engineer console already
thinks in (the attack table is one row per source, the block table is one row
per source), and because "clear this test run" and "clear this attacker's noise
after the incident is closed" are the same act.

--------------------------------------------------------------------------------
WHAT THIS IS NOT
--------------------------------------------------------------------------------
It is NOT a retention policy. ``app/api/routes/auth.py`` already purges
``login_failures`` past the retention window on the record route, automatically
and without an actor. This is a deliberate, audited, engineer-initiated deletion
of a NAMED source, and the two must not be confused: one is housekeeping, the
other is someone choosing to remove evidence, which is exactly why every call
lands in ``engineer_action_log``.

It is NOT the lift control. ``login_block.lift`` is the reversible, recorded way
to say "this block was wrong": it leaves the row, stamps who lifted it, and
suppresses automatic re-blocking for 24 hours. Deleting the block row here
removes the record entirely AND drops the lift grace with it — so a source
deleted while it is still misbehaving can be re-blocked by the very next failed
sign-in. That is the right behaviour for "clean up finished noise" and the wrong
one for "this address is a false positive"; the console says so.

--------------------------------------------------------------------------------
THREE PROPERTIES, AND WHERE EACH ONE LIVES
--------------------------------------------------------------------------------
 1. THE COUNTS ARE MEASURED, NOT ASSUMED. Every statement carries ``RETURNING``
    and the caller counts the rows Postgres actually removed. A destructive route
    that reports an assumed success is worse than one that reports nothing: the
    engineer would read "done" and never learn that the address they typed
    matched nothing.

 2. EVERY STATEMENT IS SCOPED TO THIS ENVIRONMENT — where the table has an
    ``environment`` column to scope it by. ``login_abuse_incidents`` and
    ``login_ip_blocks`` both do (dev and prod have separate databases, but
    PREVIEW deployments share the dev one), so both statements filter on it
    exactly like their neighbours in ``login_abuse`` / ``login_block``, and a
    dev deployment can never delete a production row.

    ⚠️ ``login_failures`` HAS NO ``environment`` COLUMN — see
    ``app/models/login_failure.py`` and the ``_SQL_MEASURE`` / ``_SQL_SOURCES``
    statements in ``login_abuse``, which likewise do not scope it. Its scope is
    the database it lives in. Do not "fix" this by adding a filter on a column
    that does not exist; the guard is the same one the detector relies on.

 3. NO ATTEMPTED EMAIL ADDRESS EVER LEAVES. The failures being deleted are the
    one place in this feature where addresses live, and it would be trivially
    easy to ``RETURNING email`` "so the engineer can see what they removed".
    Don't. Those are unverified strings a stranger typed, some of them belong to
    real people, and a list of them is an enumeration oracle for anything that
    reaches the response — the same rule the attack table, the block list and the
    Slack alert already hold. The DELETE returns the primary key and nothing
    else, so there is no address in this module to leak by accident.

--------------------------------------------------------------------------------
⚠️ CASTS
--------------------------------------------------------------------------------
``text()`` does NOT bind ``:name::type`` — SQLAlchemy's placeholder pattern
refuses a name followed by a colon, so a Postgres-style cast swallows the
parameter, the literal text reaches Postgres, and the statement is a syntax error
against a REAL database while every faked test passes. Write ``CAST(:name AS
type)``. Nothing here needs a cast at all (both parameters are compared to
varchar columns), and ``test_login_campaign_delete.py`` pins that every
placeholder written is a placeholder SQLAlchemy bound, exactly like the guard in
``tests/test_login_auto_block.py``.
"""

import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

log = logging.getLogger(__name__)


# --------------------------------------------------------------- statements --

# The per-attempt failures for this source.
#
# NO ``environment`` FILTER because the table has no such column (property 2).
# NO ``email`` IN ``RETURNING`` because of property 3 — the primary key is all
# the caller needs to count rows, and it is the one column here that cannot
# identify a person.
#
# ``ip_address = :ip`` never matches a NULL, so failures recorded without a
# forwarded address (local dev, a client that sent no context) are untouched by
# any campaign delete. They belong to no source and are not this call's to
# remove.
_SQL_DELETE_FAILURES = text(
    """
    DELETE FROM login_failures
     WHERE ip_address = :ip
    RETURNING login_failure_id
    """
)

# The detector's incident row(s). Normally at most one is OPEN per
# (environment, ip_address) — that is the partial unique index
# ``uq_login_abuse_open`` — but a source that has been active on separate
# occasions has one RESOLVED row per past campaign, so this can legitimately
# remove several. Both are the same source's history and both go.
_SQL_DELETE_INCIDENTS = text(
    """
    DELETE FROM login_abuse_incidents
     WHERE environment = :environment
       AND ip_address = :ip
    RETURNING abuse_incident_id
    """
)

# The block row(s). Same shape: at most one UN-LIFTED row per
# (environment, ip_address) via ``uq_login_ip_blocks_active``, plus any lifted or
# lapsed history.
#
# ⚠️ ``was_active`` IS THE FIELD THAT MATTERS TO A HUMAN. Deleting a block row
# un-blocks the source, and "3 blocks removed" does not tell an engineer whether
# anything actually changed for whoever is behind that address. Postgres computes
# it with the same predicate every other read of this table uses
# (``lifted_at IS NULL AND blocked_until > now()``), evaluated at the instant of
# deletion, so the console can say plainly whether that source can sign in again
# NOW rather than inferring it from a row it fetched some seconds ago.
_SQL_DELETE_BLOCKS = text(
    """
    DELETE FROM login_ip_blocks
     WHERE environment = :environment
       AND ip_address = :ip
    RETURNING block_id,
              (lifted_at IS NULL AND blocked_until > now()) AS was_active
    """
)


async def delete_campaign(session: AsyncSession, *, ip_address: str) -> dict:
    """Remove every row this source left behind. Returns what was ACTUALLY deleted.

    The returned dict is::

        {
            "ip_address":            the normalised address the statements used,
            "failures_deleted":      login_failures rows removed,
            "incidents_deleted":     login_abuse_incidents rows removed,
            "blocks_deleted":        login_ip_blocks rows removed,
            "active_blocks_deleted": how many of those were IN FORCE at deletion,
        }

    All counts are the length of each statement's ``RETURNING`` set, i.e. what
    Postgres removed — never an assumed success (property 1). A source that is
    not there at all is a clean set of zeros rather than an error: this is
    idempotent by design, so a double-click, a retry, or a second engineer
    clearing the same address is a harmless no-op instead of a confusing 404.

    ORDER IS DELIBERATE: failures, then the incident, then the block. The block
    is the only row with a live effect on anybody, so it is removed LAST — if the
    call dies halfway (it will not; see the transaction note) the surviving state
    is "still blocked, some history gone", never "un-blocked while the campaign
    is still running".

    DOES NOT COMMIT. The route commits once, so the three deletions and the audit
    row that records them land in a single transaction: there is no window in
    which rows are gone and the forensic trail saying who removed them is not
    yet written.

    The address is stripped and truncated to the column width (64) so it matches
    what ``login_failures``/``login_ip_blocks`` actually store — the same
    normalisation ``login_block.apply`` does on the way in. An empty address
    matches nothing and is refused by the route before it reaches here.
    """
    ip = (ip_address or "").strip()[:64]
    environment = get_settings().environment

    failures = (await session.execute(_SQL_DELETE_FAILURES, {"ip": ip})).all()
    incidents = (
        await session.execute(
            _SQL_DELETE_INCIDENTS, {"environment": environment, "ip": ip}
        )
    ).all()
    blocks = (
        (
            await session.execute(
                _SQL_DELETE_BLOCKS, {"environment": environment, "ip": ip}
            )
        )
        .mappings()
        .all()
    )

    active_blocks = sum(1 for b in blocks if b["was_active"])
    result = {
        "ip_address": ip,
        "failures_deleted": len(failures),
        "incidents_deleted": len(incidents),
        "blocks_deleted": len(blocks),
        "active_blocks_deleted": active_blocks,
    }
    # Deliberately logged: this is a destructive engineer action, and the
    # application log is the one trace that survives the database it is deleting
    # from. Counts and the source only — never an attempted address.
    log.info(
        "login_campaign: deleted campaign for %s in %s (%s failures, "
        "%s incidents, %s blocks, %s of them active)",
        ip,
        environment,
        result["failures_deleted"],
        result["incidents_deleted"],
        result["blocks_deleted"],
        active_blocks,
    )
    return result

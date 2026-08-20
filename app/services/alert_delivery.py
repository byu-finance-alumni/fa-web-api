"""Where an alert goes: Slack only, or Slack AND e-mail (#458).

ONE engineer-settable choice, stored durably, read on the alerting path.

  slack_only       Slack is the channel and e-mail is the BACKSTOP. Normal
                   operation is one message in one place; the mail goes out only
                   when the Slack post did not land. This is the default and it
                   is what the API does today.

  slack_and_email  Both channels, every time. The behaviour from before
                   2026-08-19, kept because "I want a copy in the mailbox" is a
                   legitimate preference and deleting it left no way back.

⚠️ THE E-MAIL BACKSTOP SURVIVES BOTH MODES. This switch chooses whether e-mail
is a COPY or a BACKSTOP. It cannot choose "no e-mail ever", because in
``slack_only`` a Slack post that FAILS — a revoked webhook, a Slack outage, an
unconfigured channel — still falls through to the mail. A setting that could
produce "no channel at all" would let one click turn a monitoring feature into
silence, and silence is the exact failure ``failure_alert`` exists to prevent.
The invariant is implemented and tested in
``app/services/failure_alert.deliver_alert`` (see ``tests/test_alert_delivery.py``,
which asserts it for EVERY mode rather than for the one that looks risky).

-------------------------------------------------------------------------------
WHY THE VALUE LIVES IN POSTGRES AND NOT IN AN ENVIRONMENT VARIABLE
-------------------------------------------------------------------------------
Because the owner asked to change it WITHOUT A REDEPLOY, and every env var on
this stack needs one. And because it has to be a fact about the SERVICE: this
API runs on Vercel serverless, so a module-level variable dies with the
invocation and the instances handling an outage share no memory — the same
argument ``service_incidents``, ``login_ip_blocks`` and ``maintenance_mode``
each make, and the reason this follows ``app/services/maintenance.py`` down to
the shape of its cache.

-------------------------------------------------------------------------------
READING IT CAN NEVER FAIL
-------------------------------------------------------------------------------
:func:`read_mode` NEVER RAISES and never blocks for long. It runs inside
``deliver_alert``, which runs on a request that is ALREADY FAILING and quite
possibly failing BECAUSE the database is down — which is precisely the incident
you most want to hear about. So every error path (table missing because the
migration has not been applied, no database configured at all, a timeout, a
blip) resolves to the default, ``slack_only``, whose e-mail backstop then makes
the alert reach a person anyway.

Note the direction: an unreadable setting degrades to the mode that sends MORE
when Slack is unhealthy, never to one that sends less.

-------------------------------------------------------------------------------
THE CACHE
-------------------------------------------------------------------------------
A read-through process cache with a short TTL, exactly like
``maintenance.read_status`` and for the same two reasons — it keeps a burst of
alerts from becoming a query each, and it keeps a KNOWN-GOOD value available
while the database is the thing that is broken. It is module-level and safe to
lose at any moment: worst case the next alert re-reads it, or falls back to the
default.

A write publishes the new value into the writing process immediately; every
other serverless instance picks it up within :data:`_CACHE_TTL_SECONDS`. An
alerting preference that takes up to a minute to propagate is not a control
anybody is watching a clock over.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.models.alert_delivery import AlertDeliveryConfig
from app.models.audit import AuditLog
from app.models.user import User
from app.schemas.alert_delivery import AlertDeliveryState

logger = logging.getLogger(__name__)

#: Slack is the channel; e-mail fires only when the Slack post did not land.
SLACK_ONLY = "slack_only"

#: Both channels on every alert (the pre-2026-08-19 behaviour).
SLACK_AND_EMAIL = "slack_and_email"

#: Every permitted value, in the order the console offers them.
MODES: tuple[str, ...] = (SLACK_ONLY, SLACK_AND_EMAIL)

#: What an unreadable, missing or unrecognised value resolves to. Deliberately
#: the CURRENT behaviour, so a failure to read this setting changes nothing.
DEFAULT_MODE = SLACK_ONLY

#: The row is a singleton pinned to id 1 (CHECK constraint in the migration).
_ROW_ID = 1

#: How long a read is reused within one process. Longer than the maintenance
#: gate's five seconds because this is NOT on the per-request hot path — it is
#: read once per alert, and alerts are rare — so the TTL is tuned for "keep a
#: good value while the database is down" rather than for freshness.
_CACHE_TTL_SECONDS = 60.0

#: Budget for the read. Short: it sits on a failing request, and a slow answer
#: is worth less than the default answer delivered now. Timing out is just
#: another error path, and every error path is the default.
_READ_TIMEOUT_SECONDS = 2.0

# (monotonic timestamp, mode) or None. Module-level by design: a pure
# read-through cache of one short string, safe to lose at any time.
#
# None means "never read" and must NOT be a float sentinel like 0.0 —
# ``time.monotonic()`` counts from an arbitrary origin (machine boot on Linux),
# so on a freshly started instance it returns a small number that would compare
# as "read moments ago". Same trap as ``failure_alert._degraded_last_alert_at``,
# which CI caught and a long-running laptop did not.
_cached: tuple[float, str] | None = None


def reset_cache() -> None:
    """Drop the process-local cache (used by :func:`set_mode` and by tests)."""
    global _cached
    _cached = None


def _remember(mode: str) -> str:
    global _cached
    _cached = (time.monotonic(), mode)
    return mode


def normalize(mode: str | None) -> str:
    """Map a stored value onto a mode this module knows, defaulting safely.

    The database CHECK constraint already refuses anything outside
    :data:`MODES`, and the API schema refuses it before that. This is the third
    layer, and it exists because the consequence of an unrecognised value on the
    alerting path must be "behave as we did yesterday", never an exception or a
    channel silently switched off.
    """
    return mode if mode in MODES else DEFAULT_MODE


async def _load_mode(session: AsyncSession) -> str:
    """Read the mode straight from the database (no cache, may raise)."""
    row = await session.scalar(
        select(AlertDeliveryConfig).where(AlertDeliveryConfig.id == _ROW_ID)
    )
    return normalize(row.mode if row is not None else None)


async def read_mode() -> str:
    """The current delivery mode, cached per process. NEVER RAISES.

    Takes no session and opens its own: the only caller is
    ``failure_alert.deliver_alert``, which is reached from middleware and from
    background paths that have no session of their own, and which must not hold
    a pooled connection open across a third-party HTTP call.

    Every failure — no database configured, migration not applied, table
    missing, timeout, connection refused — resolves to the last value this
    process read successfully, or to :data:`DEFAULT_MODE` if it has never read
    one. See the module docstring for why that direction is the safe one.
    """
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
        return _cached[1]
    try:
        if database.SessionLocal is None:
            raise RuntimeError("no database configured")
        async with database.SessionLocal() as session:
            return _remember(
                await asyncio.wait_for(
                    _load_mode(session), timeout=_READ_TIMEOUT_SECONDS
                )
            )
    except Exception:  # noqa: BLE001 - the alerting path must never raise
        logger.warning(
            "alert_delivery: could not read the delivery mode; using %s",
            _cached[1] if _cached is not None else DEFAULT_MODE,
        )
        return _cached[1] if _cached is not None else DEFAULT_MODE


def _channels_configured() -> tuple[bool, bool]:
    """``(slack, email)`` — whether each channel has anywhere to send.

    Imported lazily because ``failure_alert`` imports THIS module at module
    level (it is the consumer of the setting), so importing it back at module
    level here would be a cycle. The two predicates are not re-implemented: a
    second copy of "is e-mail configured?" would be a second source of truth for
    the sentence the console uses to promise the backstop still fires.
    """
    from app.services import failure_alert

    return failure_alert.slack_alerting_enabled(), failure_alert.email_alerting_enabled()


async def get_state(session: AsyncSession) -> AlertDeliveryState:
    """The full engineer-console view. UNCACHED, and it may raise.

    Uncached because the console must show the true current value, not one up to
    a minute stale — the same rule ``maintenance.get_state`` follows.

    And it deliberately does NOT swallow errors, unlike :func:`read_mode`. A
    console that cannot read the setting must say so and let the page render its
    load error; quietly displaying "Slack only" because the read failed would
    tell an engineer the system is in a state nobody has verified — which is the
    one thing this screen exists not to do.
    """
    row = await session.scalar(
        select(AlertDeliveryConfig).where(AlertDeliveryConfig.id == _ROW_ID)
    )
    email: str | None = None
    if row is not None and row.updated_by_user_id is not None:
        email = await session.scalar(
            select(User.email).where(User.user_id == row.updated_by_user_id)
        )
    slack_configured, email_configured = _channels_configured()
    return AlertDeliveryState(
        mode=normalize(row.mode if row is not None else None),
        updated_at=row.updated_at if row is not None else None,
        updated_by_email=email,
        slack_configured=slack_configured,
        email_configured=email_configured,
    )


async def set_mode(
    session: AsyncSession, *, mode: str, actor_user_id: int
) -> AlertDeliveryState:
    """Set the delivery mode and record who did it.

    Audited as ``set_alert_delivery_mode`` carrying the old and new values. As
    with every other engineer action, the ``before_flush`` guard in
    ``app/models/audit.py`` reroutes an engineer's ``AuditLog`` to
    ``engineer_action_log`` (#199) — the actor is recorded either way, and this
    module must never write that table directly.

    Upserts the singleton row rather than assuming the migration seeded it, for
    the same reason ``maintenance._upsert_row`` does: a config read must not have
    a "no row yet" branch on the alerting path.
    """
    mode = normalize(mode)

    row = await session.scalar(
        select(AlertDeliveryConfig).where(AlertDeliveryConfig.id == _ROW_ID)
    )
    if row is None:
        row = AlertDeliveryConfig(id=_ROW_ID, mode=DEFAULT_MODE)
        session.add(row)
    previous = normalize(row.mode)
    row.mode = mode
    row.updated_by_user_id = actor_user_id
    # TimestampMixin's ``onupdate`` only fires when a column actually changed;
    # re-selecting the mode already in force is a no-op write, so stamp the time
    # explicitly. "Confirmed at" is real information on a control somebody is
    # about to trust during an incident.
    row.updated_at = datetime.datetime.now(datetime.UTC)

    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="set_alert_delivery_mode",
            entity_type="alert_delivery_config",
            entity_id=None,
            field_name="mode",
            old_value=previous,
            new_value=mode,
        )
    )
    await session.commit()

    # Publish to THIS process immediately; other instances pick it up within
    # _CACHE_TTL_SECONDS.
    _remember(mode)

    email: str | None = await session.scalar(
        select(User.email).where(User.user_id == actor_user_id)
    )
    slack_configured, email_configured = _channels_configured()
    return AlertDeliveryState(
        mode=mode,
        updated_at=row.updated_at,
        updated_by_email=email,
        slack_configured=slack_configured,
        email_configured=email_configured,
    )

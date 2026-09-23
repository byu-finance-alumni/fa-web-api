"""Who gets the 6pm job-posting digest, and what it costs (#567).

Two jobs, both small, both about the digest rather than about sending it (the
sending, and the argument for its shape, are in ``opportunity_link_alert``):

  1. THE RECIPIENTS. The staff addresses the engineer sets in the console,
     stored in ``opportunity_link_digest_config`` and read the way
     ``alert_delivery`` reads its mode: a cached, time-boxed, NEVER-RAISING read
     for the public submission path, and an uncached read that may raise for the
     console. A table and not an env var because the owner asked to manage the
     list from the console, and an env var here needs a redeploy.

  2. THE LEDGER. One ``opportunity_link_digest_send_log`` row per digest e-mail
     handed to Resend, and the read that ``survey_email.get_send_usage`` folds
     into the survey's usage. See "WHY THE SURVEY BUDGET COUNTS THESE" below.

--------------------------------------------------------------------------------
EMPTY MEANS PER-POSTING, NEVER SILENCE
--------------------------------------------------------------------------------
:func:`digest_active` is the ONE predicate both notification paths ask:

  * true  -> the cron sends the digest, and the submission path says nothing;
  * false -> the submission path sends #771's per-posting alert, and the cron
             says nothing.

It is true only when there is someone to send to AND the API can send mail at
all. Recipients set but no Resend key is therefore "per-posting", not "a digest
that never leaves": the list alone must not be able to switch the notification
off.

A read that FAILS on the submission path resolves to the last value this process
read, and to "no recipients" (per-posting) if it has never read one -- the
direction that sends more, the same one ``alert_delivery.read_mode`` takes.

--------------------------------------------------------------------------------
WHY THE SURVEY BUDGET COUNTS THESE
--------------------------------------------------------------------------------
The digest and the survey share ONE Resend account, and Resend's daily quota is
a UTC calendar day. 6pm Mountain is 00:00 UTC in summer and 01:00 UTC in winter,
so every digest spends from the NEXT UTC day -- the day whose noon (18:00 UTC)
survey run would otherwise plan its full ``daily_limit`` and meet a 429 on its
last emails.

A recorded ledger, rather than a flat "reserve N a day", because it is exact
where a reserve is only right on average:

  * it counts what was ACTUALLY sent. Most days no link arrives and no digest
    goes out; a reserve would take those emails away from the survey anyway;
  * a digest always goes out BEFORE that day's survey run (00:00-01:59 UTC vs
    18:00 UTC), so by the time the survey reads its allowance the digest's rows
    are already in it -- nothing needs predicting;
  * it covers the MONTHLY limit too, which a daily reserve does not;
  * it flows through ``get_send_usage``, so the cron's pacing, the gate inside
    ``send_survey_stage`` and the console's usage meter all see the same number
    with no second implementation of "usage".

The two senders also never overlap: the digest takes the survey's own
``send_lock`` for its send, so the survey can never read its allowance while a
digest e-mail is in flight (see ``opportunity_link_alert.send_digest``).

The ledger read is WRAPPED so it cannot break the survey. It runs inside a
SAVEPOINT (a failed statement would otherwise abort the survey send's
transaction) and anything that goes wrong -- the migration not applied yet, a
fake session in a unit test -- counts as zero digest sends: exactly the survey
budget as it was before this feature, never a blocked send.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import time

from sqlalchemy import delete, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.config import get_settings
from app.models.audit import AuditLog
from app.models.opportunity_link_digest import (
    OpportunityLinkDigestConfig,
    OpportunityLinkDigestSend,
)
from app.models.user import User
from app.schemas.opportunity_link_digest import (
    MAX_RECIPIENTS,
    OpportunityLinkDigestState,
    clean_recipients,
)

log = logging.getLogger(__name__)

#: The row is a singleton pinned to id 1 (CHECK constraint in the migration).
_ROW_ID = 1

#: Same TTL as ``alert_delivery``: this is read once per survey submission, and a
#: recipient change taking up to a minute to reach every instance is harmless.
_CACHE_TTL_SECONDS = 60.0

#: Budget for the cached read. It sits on the public submission path after the
#: posting is already committed; a slow answer is worth less than the default.
_READ_TIMEOUT_SECONDS = 2.0

# (monotonic timestamp, recipients) or None = never read. None and not a 0.0
# sentinel: ``time.monotonic()`` counts from boot, see ``alert_delivery._cached``.
_cached: tuple[float, list[str]] | None = None


def reset_cache() -> None:
    """Drop the process-local cache (used by :func:`set_recipients` and tests)."""
    global _cached
    _cached = None


def _remember(recipients: list[str]) -> list[str]:
    global _cached
    _cached = (time.monotonic(), list(recipients))
    return recipients


def normalize(stored: object) -> list[str]:
    """A stored value mapped onto a clean list. NEVER RAISES.

    The API validates before writing and the CHECK caps the count; this is the
    layer that holds when a row was edited by hand. An entry that does not pass
    is dropped (and logged) rather than failing the whole list: one typo in the
    SQL editor must not take the digest away from everyone else on it.
    """
    if not isinstance(stored, list):
        return []
    kept: list[str] = []
    for value in stored:
        try:
            for address in clean_recipients([value]):
                if address not in kept:
                    kept.append(address)
        except ValueError:
            log.warning("opportunity_link_digest: ignoring an invalid stored recipient")
    return kept[:MAX_RECIPIENTS]


def email_ready() -> bool:
    """Whether the API can send the digest at all: a Resend key and a From
    address. The same account and identity the survey uses."""
    settings = get_settings()
    return bool(settings.resend_api_key and sender_address())


def sender_address() -> str | None:
    """The digest's From address. The SURVEY identity first -- this goes to the
    Career Directors, not to the engineer -- then the alert sender."""
    settings = get_settings()
    return settings.survey_from_email or settings.alert_sender


def digest_active(recipients: list[str]) -> bool:
    """THE predicate both notification paths ask. See the module docstring."""
    return bool(recipients) and email_ready()


async def load_row(session: AsyncSession) -> OpportunityLinkDigestConfig | None:
    """The config row straight from the database (no cache, may raise).

    ``populate_existing`` so a caller that re-reads it inside the send lock sees
    the watermark as it is NOW, not as the session's identity map remembers it.
    """
    return await session.scalar(
        select(OpportunityLinkDigestConfig)
        .where(OpportunityLinkDigestConfig.id == _ROW_ID)
        .execution_options(populate_existing=True)
    )


async def read_recipients() -> list[str]:
    """The configured recipients, cached per process. NEVER RAISES.

    For the public submission path, which has already committed the alum's
    postings and must not be failed or held up by this. Opens its own session
    for the same reason ``alert_delivery.read_mode`` does. Every failure
    resolves to the last value read, or to ``[]`` (per-posting) if none.
    """
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _CACHE_TTL_SECONDS:
        return list(_cached[1])
    try:
        if database.SessionLocal is None:
            raise RuntimeError("no database configured")
        async with database.SessionLocal() as session:
            row = await asyncio.wait_for(
                load_row(session), timeout=_READ_TIMEOUT_SECONDS
            )
            return _remember(normalize(row.recipients if row is not None else []))
    except Exception:  # noqa: BLE001 - the submission path must never raise
        log.warning(
            "opportunity_link_digest: could not read the recipients; using %s",
            "the cached list" if _cached is not None else "none (per-posting)",
        )
        return list(_cached[1]) if _cached is not None else []


async def _email_of(session: AsyncSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    return await session.scalar(select(User.email).where(User.user_id == user_id))


async def get_state(session: AsyncSession) -> OpportunityLinkDigestState:
    """The engineer-console view. UNCACHED, and it may raise -- a console that
    cannot read the setting must show its load error, not an empty list that
    looks verified. Same rule as ``alert_delivery.get_state``."""
    row = await load_row(session)
    return OpportunityLinkDigestState(
        recipients=normalize(row.recipients if row is not None else []),
        email_configured=email_ready(),
        reported_through=row.reported_through if row is not None else None,
        updated_at=row.updated_at if row is not None else None,
        updated_by_email=await _email_of(
            session, row.updated_by_user_id if row is not None else None
        ),
    )


async def set_recipients(
    session: AsyncSession, *, recipients: list[str], actor_user_id: int
) -> OpportunityLinkDigestState:
    """Replace the recipient list and record who did it.

    ``recipients`` has already been through ``clean_recipients`` (the schema's
    validator); it is normalised again here so no caller can store an unchecked
    list.

    Audited as ``set_opportunity_link_digest_recipients`` with the old and new
    lists. As with every engineer action the ``before_flush`` guard reroutes an
    engineer's ``AuditLog`` to ``engineer_action_log`` (#199); this module never
    writes that table directly.

    SWITCHING THE DIGEST ON STARTS ITS WATERMARK NOW. While the list was empty
    every posting was announced one by one, so the first digest must not report
    those again: a posting is announced by exactly one of the two paths.
    """
    cleaned = normalize(clean_recipients(recipients))

    row = await load_row(session)
    if row is None:
        row = OpportunityLinkDigestConfig(id=_ROW_ID, recipients=[])
        session.add(row)
    previous = normalize(row.recipients)
    now = datetime.datetime.now(datetime.UTC)
    if not previous and cleaned:
        row.reported_through = now
    row.recipients = cleaned
    row.updated_by_user_id = actor_user_id
    # Stamped explicitly: re-saving the same list is a no-op write that
    # TimestampMixin's ``onupdate`` would not notice, and "confirmed at" is real
    # information on a control somebody is about to trust.
    row.updated_at = now

    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="set_opportunity_link_digest_recipients",
            entity_type="opportunity_link_digest_config",
            entity_id=None,
            field_name="recipients",
            old_value=", ".join(previous),
            new_value=", ".join(cleaned),
        )
    )
    await session.commit()
    _remember(cleaned)

    return OpportunityLinkDigestState(
        recipients=cleaned,
        email_configured=email_ready(),
        reported_through=row.reported_through,
        updated_at=row.updated_at,
        updated_by_email=await _email_of(session, actor_user_id),
    )


# ------------------------------------------------------------------ ledger ---


async def claim_send(session: AsyncSession) -> int:
    """Record one digest e-mail BEFORE it is sent, and commit. May raise.

    Claim-then-send, the same direction as ``survey_send_log``: if the process
    dies between here and Resend's answer the e-mail is counted but maybe not
    sent, which costs the survey one email of budget. The other order would
    count nothing for an e-mail that went out, which is the 429 this exists to
    prevent.
    """
    claim_id = await session.scalar(
        insert(OpportunityLinkDigestSend).returning(
            OpportunityLinkDigestSend.digest_send_id
        )
    )
    await session.commit()
    return int(claim_id)


async def release_send(session: AsyncSession, claim_id: int) -> None:
    """Un-count an e-mail Resend explicitly REFUSED. NEVER RAISES -- a claim that
    cannot be released only makes the survey budget one email more cautious."""
    try:
        await session.execute(
            delete(OpportunityLinkDigestSend).where(
                OpportunityLinkDigestSend.digest_send_id == claim_id
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        log.warning("opportunity_link_digest: could not release claim %s", claim_id)


async def sent_counts(
    session: AsyncSession,
    *,
    start_today: datetime.datetime,
    start_month: datetime.datetime,
    after: datetime.datetime | None = None,
) -> tuple[int, int]:
    """``(today, this_month)`` digest e-mails, for ``survey_email.get_send_usage``.

    NEVER RAISES and never poisons the caller's transaction -- see "WHY THE
    SURVEY BUDGET COUNTS THESE" in the module docstring. ``after`` is the manual
    usage baseline's anchor (#544): the baseline already covers everything up to
    it, digest e-mails included, so only rows strictly after it are added.
    """
    stmt = select(
        func.count().label("month"),
        func.count()
        .filter(OpportunityLinkDigestSend.sent_at >= start_today)
        .label("today"),
    ).where(OpportunityLinkDigestSend.sent_at >= start_month)
    if after is not None:
        stmt = stmt.where(OpportunityLinkDigestSend.sent_at > after)
    try:
        async with session.begin_nested():
            row = (await session.execute(stmt)).first()
    except Exception:  # noqa: BLE001 - the survey send must never fail on this
        log.warning(
            "opportunity_link_digest: could not read the digest send log; "
            "counting zero digest e-mails"
        )
        return (0, 0)
    if not row:
        return (0, 0)
    return (int(row[1] or 0), int(row[0] or 0))

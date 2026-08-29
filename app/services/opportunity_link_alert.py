"""Tell somebody a job posting arrived through the survey (#771).

From the owner's 2026-08-28 note: *"some sort of notification so we never miss
when a job posting is included in someone's survey."* Before this module, a
survey-sourced ``opportunity_links`` row landed ``pending`` and waited for a
staff member to remember to open the Links tab. Nothing fired. This is the thing
that fires.

--------------------------------------------------------------------------------
IT REUSES THE ALERTER; IT IS NOT A SECOND NOTIFICATION PATH
--------------------------------------------------------------------------------
Delivery is ``failure_alert.deliver_alert`` with ``purpose=SUBMISSION``, so this
message inherits, for free and without a second copy of any of it:

  * the Slack + e-mail fan-out and the engineer-settable delivery mode
    (``alert_delivery``: ``slack_only`` / ``slack_and_email``);
  * the E-MAIL BACKSTOP — a Slack post that does not land still reaches a mailbox,
    which for THIS feature is the whole requirement;
  * UNSET ⇒ OFF, per channel. No webhook and no ``ALERT_EMAIL_TO`` means no
    message and no cost, which is what keeps local runs, CI, the test suite and
    preview deployments silent with nothing to remember to switch off;
  * the rule that an alerter never raises and never retries.

The only thing added to that service was a third ``purpose`` and its channel
(``SLACK_SUBMISSION_WEBHOOK_URL``, falling back to the operational webhook so a
forgotten env var is a misfiled message rather than silence).

--------------------------------------------------------------------------------
PER POSTING, IMMEDIATELY — AND HOW TO MAKE IT A DIGEST
--------------------------------------------------------------------------------
:data:`MODE_PER_POSTING` is the default and is what ships: one message per
submission, as it happens, because "never miss" argues against batching.

If campaign volume turns out to be bursty, the fallback is a daily digest, and
that is a CONFIG CHANGE, not a rewrite:

  1. set ``OPPORTUNITY_LINK_NOTIFY_MODE=daily_digest`` on the deployment;
  2. add ``{"path": "/opportunity-links/cron/digest", "schedule": "0 17 * * *"}``
     to ``vercel.json`` (the route already exists and is ``CRON_SECRET``-gated,
     exactly like the survey and headshot crons).

Both renderings and both entry points are written and tested here. In
``daily_digest`` mode :func:`notify_new_links` deliberately does nothing and
:func:`send_digest` does the talking; in ``per_posting`` mode it is the other way
round. Nothing else in the app changes, and the mode is read per call so the two
can never both fire for the same rows.

--------------------------------------------------------------------------------
⚠️ THE ALERT MAY NEVER BREAK THE SUBMISSION
--------------------------------------------------------------------------------
``POST /survey/respond/{token}/links`` is PUBLIC (the signed token is the whole
credential) and an alumnus is sitting in front of it. So:

  * the links are COMMITTED BEFORE anything here is called — the alum's posting is
    already saved when the notification is attempted;
  * every path here swallows every exception and returns None. A Slack outage, a
    revoked webhook, a Resend 4xx, a timeout: the alum still gets their success
    response and the row is still in the queue;
  * the whole attempt is time-boxed by :data:`_DELIVERY_TIMEOUT_SECONDS`, so a
    hanging third party costs seconds, not the request;
  * it is skipped entirely, before any work at all, when no channel is configured.

A missed notification degrades this feature to what it was yesterday (a queue
somebody has to open). A raised exception would lose the posting, which is worse
than the problem being solved.

WHY IT IS AWAITED AND NOT FIRED AND FORGOTTEN. ``asyncio.create_task`` would take
the latency off the alum's request, and on Vercel it would also frequently never
run: the function is frozen once the response is written, so a detached task is a
coin flip. The same reasoning is already recorded in ``login_abuse`` for the login
path. Awaiting a short, bounded, unset-means-skipped call is the honest trade.

--------------------------------------------------------------------------------
⚠️ WHAT THE MESSAGE MAY CONTAIN
--------------------------------------------------------------------------------
This leaves the system into a Slack channel and a mailbox, and the posting it is
about was written by a member of the PUBLIC minutes earlier and has not been
moderated yet. So the message carries only:

  * how many postings arrived, and their role types (a three-value enum);
  * their ``opportunity_link_id``s;
  * when, which environment, and where to go and action them.

It carries NO alumni name, NO e-mail, NO company name, NO details text and NO
URL. Those are either PII or unmoderated attacker-supplied free text, and the
recipient does not need any of them to do the one thing this message asks: open
the Links tab and review the pending rows. The substance stays behind the login,
which is where the moderation controls are anyway.

That is a deliberately stricter line than "escape it and send it". Slack escaping
stops a ``<`` eating the line; it does not stop a channel full of whatever
somebody typed into a public form.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.config import get_settings
from app.models.opportunity_link import OpportunityLink
from app.services import failure_alert

log = logging.getLogger(__name__)

#: One message per submission, as it happens. THE DEFAULT — see the module
#: docstring for the argument.
MODE_PER_POSTING = "per_posting"

#: One message a day summarising everything that arrived. Requires the cron entry
#: in ``vercel.json``; see the module docstring.
MODE_DAILY_DIGEST = "daily_digest"

MODES: tuple[str, ...] = (MODE_PER_POSTING, MODE_DAILY_DIGEST)

#: What an unset or unrecognised ``OPPORTUNITY_LINK_NOTIFY_MODE`` resolves to.
#: Deliberately the noisier of the two: a typo in an env var must not turn the
#: feature off, and this feature exists because nothing was being sent.
DEFAULT_MODE = MODE_PER_POSTING

#: Budget for the whole delivery attempt. Sits on a public request an alum is
#: waiting on, so it is short — shorter than the failing-request budgets in
#: ``failure_alert``, because nothing is wrong here and there is nothing to
#: justify holding the form open.
_DELIVERY_TIMEOUT_SECONDS = 5.0

#: How far back :func:`send_digest` looks. One day plus a margin, so a cron that
#: runs a few minutes late (or a deploy that shifts it) cannot skip a window and
#: silently drop a posting. Overlap costs a repeated line in one digest; a gap
#: costs the thing the issue is about.
DIGEST_LOOKBACK_HOURS = 25


def notify_mode() -> str:
    """The configured mode. Read from the environment PER CALL, never cached.

    An env var and not a database row, unlike ``alert_delivery``'s Slack/e-mail
    choice, and the difference is deliberate: that one is a control the owner
    flips from the console during an incident, this is a fallback nobody expects
    to touch without also editing ``vercel.json`` to add the cron. A setting that
    is only half-usable without a redeploy should not pretend to be live.

    Anything unrecognised is :data:`DEFAULT_MODE` — never an exception, and never
    "off".
    """
    raw = (os.getenv("OPPORTUNITY_LINK_NOTIFY_MODE") or "").strip().lower()
    return raw if raw in MODES else DEFAULT_MODE


def _role_summary(role_types: list[str]) -> str:
    """``internship x2, full_time`` — counts per role type, stable order.

    Enum values only (the column has a CHECK constraint), so nothing free-text
    can reach the message through here.
    """
    if not role_types:
        return "unknown"
    seen: dict[str, int] = {}
    for value in role_types:
        seen[value] = seen.get(value, 0) + 1
    return ", ".join(
        (name if count == 1 else f"{name} x{count}") for name, count in seen.items()
    )


def _ids(link_ids: list[int]) -> str:
    """Render the link ids, truncated so one bulk submission cannot produce an
    unbounded row in an e-mail or a Slack block."""
    shown = [str(i) for i in link_ids[:10]]
    if len(link_ids) > 10:
        shown.append(f"+{len(link_ids) - 10} more")
    return ", ".join(shown) or "unknown"


def render_new_posting(
    *, link_ids: list[int], role_types: list[str], submitted_at: datetime.datetime
) -> tuple[str, str, list[tuple[str, str]], str]:
    """``(subject, intro, rows, slack_summary)`` for ONE survey submission.

    Split out from sending, exactly as ``failure_alert.render_alert`` is, so the
    wording — and above all the assertion that no PII and no unmoderated text can
    appear in it — is unit-testable without a network.
    """
    env = str(get_settings().environment)
    count = len(link_ids)
    noun = "job posting" if count == 1 else "job postings"
    subject = f"[fa-web-api {env}] {count} {noun} submitted through the survey"
    intro = (
        "An alumnus submitted an opportunity through the survey. It is waiting "
        "in the Links tab as pending and needs a staff review."
    )
    rows = [
        ("Environment", env),
        ("Postings", str(count)),
        ("Role type", _role_summary(role_types)),
        ("Received", failure_alert._fmt_ts(submitted_at)),
        ("Link IDs", _ids(link_ids)),
        ("Action", "Links tab -> status Pending -> approve or reject"),
        ("Build", failure_alert._deployment_note()),
    ]
    summary = (
        f"{count} {noun} arrived through the survey and are pending review "
        f"in the Links tab ({_role_summary(role_types)})."
    )
    return subject, intro, rows, summary


def render_digest(
    *,
    link_ids: list[int],
    role_types: list[str],
    pending_total: int,
    since: datetime.datetime,
) -> tuple[str, str, list[tuple[str, str]], str]:
    """``(subject, intro, rows, slack_summary)`` for the DAILY DIGEST form.

    Written and tested even though the shipped default never calls it — that is
    what makes the switch in the module docstring a config change rather than a
    rewrite. It says one thing the per-posting message cannot: how big the queue
    has become, which is the number that matters when you are reading once a day.
    """
    env = str(get_settings().environment)
    count = len(link_ids)
    noun = "job posting" if count == 1 else "job postings"
    subject = f"[fa-web-api {env}] {count} {noun} submitted in the last day"
    intro = (
        "Daily summary of opportunities submitted through the survey. Anything "
        "still pending is waiting in the Links tab for a staff review."
    )
    rows = [
        ("Environment", env),
        ("New since", failure_alert._fmt_ts(since)),
        ("Submitted", str(count)),
        ("Role type", _role_summary(role_types)),
        ("Pending in total", str(pending_total)),
        ("Link IDs", _ids(link_ids)),
        ("Action", "Links tab -> status Pending -> approve or reject"),
        ("Build", failure_alert._deployment_note()),
    ]
    summary = (
        f"{count} {noun} arrived through the survey since "
        f"{failure_alert._fmt_ts(since)}; {pending_total} pending in the Links "
        "tab in total."
    )
    return subject, intro, rows, summary


async def _deliver(
    subject: str, intro: str, rows: list[tuple[str, str]], summary: str
) -> bool:
    """Push one message through the shared alerter. NEVER raises, never retries.

    Returns True when it landed somewhere. The caller does not act on the answer —
    it is returned for the tests, and because a function that swallows everything
    should at least say whether it worked.
    """
    try:
        return bool(
            await asyncio.wait_for(
                failure_alert.deliver_alert(
                    subject,
                    intro,
                    rows,
                    purpose=failure_alert.SUBMISSION,
                    # Slack gets one line; the mail keeps every row. The two
                    # channels are read in different places -- see
                    # ``failure_alert.render_slack``.
                    slack_summary=summary,
                ),
                timeout=_DELIVERY_TIMEOUT_SECONDS,
            )
        )
    except Exception:  # noqa: BLE001 - the alerter must never break the caller
        # Deliberately NOT retried and deliberately NOT re-reported: alerting
        # about a failed alert is the one way to build a loop. The posting is
        # already committed, so this costs one missed message.
        log.error(
            "opportunity_link_alert: could not deliver %r (%d rows)",
            subject,
            len(rows),
        )
        return False


async def notify_new_links(links: list[OpportunityLink]) -> bool:
    """Announce a survey submission. NEVER RAISES — see the module docstring.

    Called by ``opportunity_links.submit_links`` AFTER the commit, so the alum's
    postings are already durable whatever happens here.

    Silent, at zero cost, when:

      * ``links`` is empty (nothing arrived);
      * no channel is configured (``alerting_enabled()`` is false) — the
        unset-means-off rule the whole alerting stack shares, and what keeps the
        test suite and preview deployments quiet;
      * the mode is :data:`MODE_DAILY_DIGEST` — the cron is doing the talking, and
        sending here as well would mean both.
    """
    try:
        if not links:
            return False
        if not failure_alert.alerting_enabled():
            return False
        if notify_mode() != MODE_PER_POSTING:
            return False
        link_ids = [
            link.opportunity_link_id
            for link in links
            if link.opportunity_link_id is not None
        ]
        role_types = [str(link.role_type) for link in links]
        # The DB stamps ``submitted_at`` with its own clock and the rows are not
        # refreshed on this path, so fall back to now rather than rendering None.
        submitted_at = next(
            (link.submitted_at for link in links if link.submitted_at is not None),
            datetime.datetime.now(datetime.UTC),
        )
        subject, intro, rows, summary = render_new_posting(
            link_ids=link_ids, role_types=role_types, submitted_at=submitted_at
        )
        return await _deliver(subject, intro, rows, summary)
    except Exception:  # noqa: BLE001 - a public write must never fail on this
        log.error("opportunity_link_alert: notification failed", exc_info=True)
        return False


async def send_digest(session: AsyncSession) -> bool:
    """Send ONE digest of the survey postings from the last
    :data:`DIGEST_LOOKBACK_HOURS`. NEVER RAISES.

    The other half of the config switch. Not reached in the shipped default mode;
    reached from ``POST /opportunity-links/cron/digest`` once the cron entry
    exists. Silent when nothing arrived — a daily "nothing happened" message is
    how a channel gets muted, and a muted channel is the failure this is
    preventing.

    Reports EVERYTHING that arrived in the window, whatever its status now — the
    question a digest answers is "what did alumni send us yesterday", and
    dropping a posting because somebody was quick enough to approve it before the
    cron ran would make the report depend on staff timing.

    Counts ``pending`` over the WHOLE table, not just the window: the useful
    number in a once-a-day message is how much is waiting, and a queue that grows
    for a week is exactly what "we never miss" is about.
    """
    try:
        if not failure_alert.alerting_enabled():
            return False
        if notify_mode() != MODE_DAILY_DIGEST:
            return False
        since = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            hours=DIGEST_LOOKBACK_HOURS
        )
        rows = (
            (
                await session.execute(
                    select(OpportunityLink)
                    .where(
                        OpportunityLink.source == "survey",
                        OpportunityLink.submitted_at >= since,
                    )
                    .order_by(OpportunityLink.opportunity_link_id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return False
        pending_total = (
            await session.scalar(
                select(func.count(OpportunityLink.opportunity_link_id)).where(
                    OpportunityLink.status == "pending"
                )
            )
            or 0
        )
        subject, intro, alert_rows, summary = render_digest(
            link_ids=[r.opportunity_link_id for r in rows],
            role_types=[str(r.role_type) for r in rows],
            pending_total=int(pending_total),
            since=since,
        )
        return await _deliver(subject, intro, alert_rows, summary)
    except Exception:  # noqa: BLE001 - a cron must never 500 on a missed message
        log.error("opportunity_link_alert: digest failed", exc_info=True)
        return False


async def send_digest_standalone() -> bool:
    """:func:`send_digest` with a session of its own. NEVER RAISES.

    For a caller that has no request session (a cron handler that wants to answer
    the platform before the outbound POST finishes, or a script).
    """
    if database.SessionLocal is None:
        return False
    try:
        async with database.SessionLocal() as session:
            return await send_digest(session)
    except Exception:  # noqa: BLE001
        log.error("opportunity_link_alert: digest could not open a session")
        return False

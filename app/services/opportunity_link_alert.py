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
PER POSTING, OR ONE DAILY DIGEST TO STAFF (#567)
--------------------------------------------------------------------------------
Two ways to announce the same postings, and EXACTLY ONE of them is live at a time:

  :data:`MODE_DAILY_DIGEST`  when an engineer has set digest recipients in the
      console (``opportunity_link_digest``) and the API can send mail. The Career
      Directors get ONE e-mail at about 6pm Mountain, only on days a posting
      arrived, from :func:`send_digest` via the cron. A copy goes to the Slack
      submission channel, which is free. The engineer's alert MAILBOX does not
      get a copy: every e-mail spends from the survey's Resend quota, and the
      Slack line already tells the engineer.
  :data:`MODE_PER_POSTING`   otherwise -- #771's behaviour, one alert per
      submission to the engineer channels, as it happens.

WHY THE RECIPIENT LIST IS THE SWITCH. #771 had an env var for the mode and a
separate place for recipients, which is two things to get right and a state
("digest mode, nobody to send to") that is silence. Deriving the mode from the
list makes that state unrepresentable: empty list, per-posting; recipients set
but no Resend key, per-posting; the digest is live only when it can actually be
delivered. It is also the one thing the owner asked to control from the console.

NEVER BOTH FOR THE SAME ROWS. Both paths ask the same predicate
(``opportunity_link_digest.digest_active``). And the digest reports a WATERMARKED
window -- postings after ``reported_through`` -- which is moved to "now" when the
digest is switched on, so the postings already announced one by one while the
list was empty are not reported a second time. The one residual is a failed
recipient read on the submission path with nothing cached, which resolves to
per-posting (the direction that sends more); that needs the database to fail
between committing the posting and reading one row.

6PM MOUNTAIN, ALL YEAR, ONCE A DAY. Two UTC cron entries fire every evening
(one per UTC offset); :func:`digest_due` lets through only the call that lands in
the 6pm local hour, and ``last_digest_on`` stops a duplicated or retried call
from sending twice. See the cron route for the DST arithmetic.

THE WINDOW HAS NO GAPS AND NO REPEATS, whatever minute of its hour Vercel Hobby
fires the cron. Each run reports ``(reported_through, now - settle]`` and moves
the watermark only once an e-mail has landed, so a late, skipped, doubled or
failed run changes WHEN a posting is reported, never WHETHER -- and a posting is
never repeated into a day when nothing new arrived. The settle margin keeps a
submission whose transaction is still open out of this run and in the next.

THE SURVEY'S EMAIL BUDGET. Every digest e-mail is recorded in
``opportunity_link_digest_send_log`` and counted by
``survey_email.get_send_usage``; the send runs under the survey's own
``send_lock``. See ``opportunity_link_digest`` for the full argument.

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
This leaves the system into a Slack channel and a mailbox (the engineer's, or
the Career Directors' for the digest), and the posting it is about was written by
a member of the PUBLIC minutes earlier and has not been moderated yet. So the
message carries only:

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
import time
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database
from app.core.config import get_settings
from app.models.opportunity_link import OpportunityLink
from app.services import failure_alert, mailer, opportunity_link_digest

log = logging.getLogger(__name__)

#: One alert per submission to the engineer channels, as it happens (#771).
#: What runs whenever the digest is not live -- see the module docstring.
MODE_PER_POSTING = "per_posting"

#: One e-mail a day to the staff recipients set in the console (#567), sent by
#: the ``/opportunity-links/cron/digest`` cron in ``vercel.json``.
MODE_DAILY_DIGEST = "daily_digest"

MODES: tuple[str, ...] = (MODE_PER_POSTING, MODE_DAILY_DIGEST)

#: Budget for the whole delivery attempt. Sits on a public request an alum is
#: waiting on, so it is short — shorter than the failing-request budgets in
#: ``failure_alert``, because nothing is wrong here and there is nothing to
#: justify holding the form open.
_DELIVERY_TIMEOUT_SECONDS = 5.0

#: How far back the FIRST digest looks, when there is no watermark yet (the row
#: was seeded but the digest was never switched on through the console, which
#: sets one). After that the watermark decides the window and this is unused;
#: see "THE WINDOW HAS NO GAPS AND NO REPEATS" in the module docstring. Sized for
#: the longest gap between two 6pm runs -- 25 hours across the autumn fall-back,
#: plus up to an hour of Hobby firing jitter -- so even this fallback cannot open
#: a gap against the day before. Overlap is harmless.
DIGEST_LOOKBACK_HOURS = 27

#: The digest goes out at 6pm MOUNTAIN, all year. Vercel crons are UTC-only, so
#: two entries fire every evening (``0 0`` and ``0 1`` UTC, one per offset) and
#: :func:`digest_due` lets through only the one that lands in this local hour.
#: See the cron route for the arithmetic.
DIGEST_TZ = ZoneInfo("America/Denver")
DIGEST_LOCAL_HOUR = 18

#: Postings submitted in the last few minutes wait for the next run. A
#: submission's ``submitted_at`` is stamped when its transaction starts; a
#: posting whose transaction is still open when the digest reads would otherwise
#: land behind a watermark that has already moved past it.
DIGEST_SETTLE = datetime.timedelta(minutes=5)

#: How long the digest waits for a survey send to finish before giving up on
#: this run. It never sends without the lock (see :func:`send_digest`), and a
#: skipped run loses nothing: the watermark has not moved.
_LOCK_WAIT_SECONDS = 90.0
_LOCK_POLL_SECONDS = 5.0

#: Per-recipient Resend call budget. The cron has minutes, not an alum waiting.
_EMAIL_TIMEOUT_SECONDS = 10.0


async def notify_mode() -> str:
    """Which path announces postings right now. NEVER RAISES.

    :data:`MODE_DAILY_DIGEST` when ``opportunity_link_digest.digest_active`` is
    true for the configured recipients, else :data:`MODE_PER_POSTING`. The
    recipient read is cached and time-boxed, and a failed read is "no
    recipients" -- per-posting, the direction that sends more.
    """
    try:
        recipients = await opportunity_link_digest.read_recipients()
        if opportunity_link_digest.digest_active(recipients):
            return MODE_DAILY_DIGEST
    except Exception:  # noqa: BLE001 - never "off" because a read failed
        log.warning("opportunity_link_alert: mode read failed; per-posting")
    return MODE_PER_POSTING


def digest_due(now: datetime.datetime | None = None) -> bool:
    """Whether a cron call at ``now`` is the evening's real 6pm run.

    True only inside 18:00-18:59 America/Denver. Of the two UTC cron entries
    exactly one lands in that hour on every local day, DST changeover days
    included -- the clocks change at 2am, so every evening sits wholly on one
    offset (see the cron route, and the changeover tests). The other entry's
    call lands at 5pm or 7pm local and is a no-op.
    """
    now = now or datetime.datetime.now(datetime.UTC)
    return now.astimezone(DIGEST_TZ).hour == DIGEST_LOCAL_HOUR


def local_digest_date(now: datetime.datetime | None = None) -> datetime.date:
    """The America/Denver calendar date at ``now`` -- the once-a-day key."""
    now = now or datetime.datetime.now(datetime.UTC)
    return now.astimezone(DIGEST_TZ).date()


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
    """``(subject, intro, rows, slack_summary)`` for the DIGEST's ENGINEER copy.

    Only ``slack_summary`` is posted (the Slack submission channel, see
    :func:`send_digest`); the staff e-mail is :func:`render_staff_digest`. The
    rest is kept so the engineer-facing rendering stays whole and testable. It
    says one thing the per-posting message cannot: how big the queue has become.
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


#: Role types in the words a Career Director would use. The column is a
#: three-value enum (CHECK constraint), so nothing free-text reaches the e-mail
#: through here; an unknown value falls back to a neutral noun.
_ROLE_WORDS: dict[str, tuple[str, str]] = {
    "internship": ("internship", "internships"),
    "full_time": ("full-time job", "full-time jobs"),
    "both": ("internship or full-time role", "internship or full-time roles"),
}


def _friendly_roles(role_types: list[str]) -> str:
    """``2 internships and 1 full-time job`` — counts per role type, stable order."""
    seen: dict[str, int] = {}
    for value in role_types:
        seen[value] = seen.get(value, 0) + 1
    parts = []
    for value, count in seen.items():
        one, many = _ROLE_WORDS.get(value, ("job link", "job links"))
        parts.append(f"{count} {one if count == 1 else many}")
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def links_tab_url() -> str | None:
    """The Links tab filtered to what needs a review, or None when the frontend
    URL is not configured (the e-mail then names the tab without a button)."""
    base = (get_settings().survey_app_base_url or "").strip().rstrip("/")
    return f"{base}/links?status=pending" if base else None


def render_staff_digest(
    *, link_ids: list[int], role_types: list[str], pending_total: int
) -> tuple[str, str, str]:
    """``(subject, html, text)`` for the Career Directors' daily e-mail (#567).

    Plain, friendly wording for non-technical staff -- no environment tags, no
    build line, no "status Pending -> approve". The same PII-free facts as every
    other rendering in this module and nothing more (see "WHAT THE MESSAGE MAY
    CONTAIN"): a count, the role types, where to go, and the link numbers as a
    small reference at the bottom. Split from sending so that rule is testable
    without a network.
    """
    count = len(link_ids)
    subject = (
        "1 new job link from alumni to review"
        if count == 1
        else f"{count} new job links from alumni to review"
    )
    what = _friendly_roles(role_types)
    arrived = (
        "An alum shared a new job or internship link through the survey today"
        if count == 1
        else f"Alumni shared {count} new job or internship links through the "
        "survey today"
    )
    first = f"{arrived}" + (f" ({what})." if what else ".")
    waiting = (
        "It is waiting for your review in the Links tab of the Finance Alumni "
        "Database."
        if count == 1
        else "They are waiting for your review in the Links tab of the Finance "
        "Alumni Database."
    )
    total = (
        "1 link is waiting for review in total."
        if pending_total == 1
        else f"{pending_total} links are waiting for review in total."
    )
    footer = (
        "You get this e-mail once a day, around 6pm, and only on days new links "
        "come in."
    )
    reference = f"Link numbers: {_ids(link_ids)}"
    url = links_tab_url()

    text_lines = ["Hello,", "", first, waiting, total, ""]
    if url:
        text_lines += [f"Review them here: {url}", ""]
    text_lines += [footer, reference]

    p = "<p style='margin:0 0 12px'>"
    button = (
        f"{p}<a href='{escape(url, quote=True)}' style=\"display:inline-block;"
        "background:#1e2a4a;color:#ffffff;text-decoration:none;font-weight:600;"
        f"padding:10px 18px;border-radius:8px\">Review links</a></p>"
        if url
        else ""
    )
    html = (
        "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        'font-size:15px;line-height:1.5;color:#111">'
        f"{p}Hello,</p>"
        f"{p}{escape(first)} {escape(waiting)}</p>"
        f"{p}{escape(total)}</p>"
        f"{button}"
        f"<p style='margin:16px 0 0;font-size:13px;color:#555'>{escape(footer)}"
        f"<br>{escape(reference)}</p>"
        "</div>"
    )
    return subject, html, "\n".join(text_lines)


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
      * the mode is :data:`MODE_DAILY_DIGEST` — the staff digest is doing the
        talking, and sending here as well would mean both. Asked AFTER the
        channel check, so a deployment with no channel pays no database read.
    """
    try:
        if not links:
            return False
        if not failure_alert.alerting_enabled():
            return False
        if await notify_mode() != MODE_PER_POSTING:
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
    """Send the staff digest of survey postings not yet reported. NEVER RAISES.

    Reached from ``/opportunity-links/cron/digest`` (the Vercel cron, about 6pm
    Mountain). Returns True when at least one staff e-mail landed.

    Does nothing, and says nothing, when the digest is not live (no recipients,
    or no way to send mail) -- the per-posting alert is announcing postings
    instead -- and when nothing new arrived: a daily "nothing happened" message
    is how a mailbox rule gets written, and a filtered message is the failure
    this is preventing.

    ⚠️ RUNS UNDER THE SURVEY'S ``send_lock``. The digest and the survey spend
    from one Resend quota, and the survey plans its sends from a budget it reads
    once. Holding the same lock means the survey can never read that budget
    while a digest e-mail is in flight, so the budget it reads always includes
    every digest e-mail (see ``opportunity_link_digest``). It also means two
    deliveries of the same cron cannot both send: the second one waits, then
    finds the watermark already moved. If a survey send holds the lock for
    longer than :data:`_LOCK_WAIT_SECONDS`, this run gives up WITHOUT sending
    and without moving the watermark, so tomorrow's digest carries the postings.
    """
    try:
        from app.services import survey_email  # local: survey_email imports us

        deadline = time.monotonic() + _LOCK_WAIT_SECONDS
        while True:
            async with survey_email.send_lock() as acquired:
                if acquired:
                    return await _send_digest_locked(session)
            if time.monotonic() >= deadline:
                log.warning(
                    "opportunity_link_alert: a survey send held the lock for %ss; "
                    "digest skipped, the postings carry to the next run",
                    int(_LOCK_WAIT_SECONDS),
                )
                return False
            await asyncio.sleep(_LOCK_POLL_SECONDS)
    except Exception:  # noqa: BLE001 - a cron must never 500 on a missed message
        log.error("opportunity_link_alert: digest failed", exc_info=True)
        return False


async def _send_digest_locked(session: AsyncSession) -> bool:
    """The digest itself, with the send lock held. May raise (the caller catches).

    Reports EVERYTHING submitted through the survey in the window, whatever its
    status now -- the question a digest answers is "what did alumni send us",
    and dropping a posting because somebody approved it before 6pm would make
    the report depend on staff timing. Counts ``pending`` over the WHOLE table:
    the useful number once a day is how much is waiting.
    """
    row = await opportunity_link_digest.load_row(session)
    recipients = opportunity_link_digest.normalize(
        row.recipients if row is not None else []
    )
    if row is None or not opportunity_link_digest.digest_active(recipients):
        return False
    # ONCE PER LOCAL DAY. Read under the send lock, so a duplicated or retried
    # cron call -- Vercel can deliver one twice -- sees the first call's date
    # and neither sends nor spends the survey's quota a second time. Without
    # this, a posting that arrived between the two calls would get a second
    # e-mail the same evening.
    now = datetime.datetime.now(datetime.UTC)
    today = local_digest_date(now)
    if row.last_digest_on == today:
        return False

    through = now - DIGEST_SETTLE
    since = row.reported_through or (
        through - datetime.timedelta(hours=DIGEST_LOOKBACK_HOURS)
    )
    if since >= through:
        return False
    links = (
        (
            await session.execute(
                select(OpportunityLink)
                .where(
                    OpportunityLink.source == "survey",
                    OpportunityLink.submitted_at > since,
                    OpportunityLink.submitted_at <= through,
                )
                .order_by(OpportunityLink.opportunity_link_id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not links:
        # Nothing to say. Move the watermark anyway, so the window stays one day
        # wide; there is nothing in the gap to lose.
        row.reported_through = through
        row.last_digest_on = today
        await session.commit()
        return False

    pending_total = int(
        await session.scalar(
            select(func.count(OpportunityLink.opportunity_link_id)).where(
                OpportunityLink.status == "pending"
            )
        )
        or 0
    )
    link_ids = [link.opportunity_link_id for link in links]
    role_types = [str(link.role_type) for link in links]

    subject, html, text = render_staff_digest(
        link_ids=link_ids, role_types=role_types, pending_total=pending_total
    )
    landed = 0
    for address in recipients:
        if await _email_one_recipient(session, address, subject, html, text):
            landed += 1

    # The engineer's copy: ONE Slack line to the submission channel, free, and
    # NOT the alert mailbox -- that would be one more e-mail out of the survey's
    # quota to say what Slack already said. Unconfigured Slack simply skips.
    eng_subject, eng_intro, eng_rows, summary = render_digest(
        link_ids=link_ids,
        role_types=role_types,
        pending_total=pending_total,
        since=since,
    )
    await failure_alert._send_slack(
        eng_subject,
        eng_intro,
        eng_rows,
        purpose=failure_alert.SUBMISSION,
        summary=f"Sent to {landed} of {len(recipients)} staff recipients: {summary}",
    )

    if landed == 0:
        # Not one staff e-mail landed. Leave the watermark AND the date where
        # they are, so a retry this evening or tomorrow's run reports these
        # postings again, rather than recording them as told when nobody was.
        log.error(
            "opportunity_link_alert: digest reached none of %d recipients; "
            "the postings carry to the next run",
            len(recipients),
        )
        return False
    row.reported_through = through
    row.last_digest_on = today
    await session.commit()
    return True


async def _email_one_recipient(
    session: AsyncSession, address: str, subject: str, html: str, text: str
) -> bool:
    """Send the digest to ONE address, counted in the survey's budget.

    ONE E-MAIL PER RECIPIENT, not one e-mail with several ``to``s, so that the
    ledger row per e-mail is exactly one unit of Resend quota whichever way
    Resend counts a multi-recipient message -- and so the Career Directors do not
    see each other's addresses on a message they may forward.

    Claimed in the ledger BEFORE the call. Released only when Resend explicitly
    REFUSES (a non-2xx answer); a transport failure leaves the claim, because
    the e-mail may have gone and an uncounted e-mail is the 429 this prevents.
    """
    settings = get_settings()
    claim_id = await opportunity_link_digest.claim_send(session)
    payload = {
        "from": mailer.from_field(
            opportunity_link_digest.sender_address() or "", settings.survey_from_name
        ),
        "to": [address],
        "subject": subject,
        "html": html,
        "text": text,
    }
    try:
        response = await mailer.post_json(
            mailer.RESEND_SEND_URL,
            api_key=settings.resend_api_key or "",
            payload=payload,
            timeout=_EMAIL_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - outcome unknown: keep the claim
        log.error("opportunity_link_alert: could not reach Resend for the digest")
        return False
    if not response.is_success:
        await opportunity_link_digest.release_send(session, claim_id)
        # The address is not logged: it is a staff member's, and the status is
        # what tells you where to look.
        log.error(
            "opportunity_link_alert: Resend refused a digest e-mail (HTTP %s)",
            response.status_code,
        )
        return False
    return True


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

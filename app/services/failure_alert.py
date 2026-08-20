"""API failure alerting — email the engineer once per incident (#444).

On 2026-08-18 the API returned a server error for every request for several
minutes. Nothing notified anybody; it was found because a person opened the site
and saw no data. This module is the thing that would have sent the email.

--------------------------------------------------------------------------------
WHAT AN "INCIDENT" IS
--------------------------------------------------------------------------------
An incident is ONE CONTIGUOUS PERIOD DURING WHICH THE API IS FAILING — not an
error, not a request, not a deployment, and not a process. Concretely it is one
row in ``service_incidents`` with ``resolved_at IS NULL``, and it has three
transitions:

  open      first server error observed after a healthy period. NO EMAIL. A
            single 500 is a blip: something timed out, one alum's record has bad
            data, a bot hit a broken query string. Paging a person for that
            trains them to ignore the pager, which is worse than no pager.

  alert     the failure is SUSTAINED: at least ``ALERT_MIN_FAILURES`` failures
            reported AND at least ``ALERT_MIN_SECONDS`` elapsed since the first
            one. Both conditions, because either alone is wrong — a burst of 5
            errors in 200ms is one bad request fanned out, and one error still
            failing 60 seconds later might just be one broken endpoint nobody is
            using. ONE email. The incident then stays silent no matter how many
            more errors arrive.

  resolve   the API has gone ``_RECOVERY_QUIET_SECONDS`` without a failure and is
            serving requests again. ONE recovery email — and only if the opening
            one was sent, because there is nothing to clear if nobody was paged.

So today's incident produces exactly two emails: one opening, one clearing. A
flood of 500s produces the same two. That is the entire point of the issue.

--------------------------------------------------------------------------------
WHY THE STATE LIVES IN POSTGRES
--------------------------------------------------------------------------------
"Have we already alerted for this incident?" has to be a fact about the SERVICE.
This API runs on Vercel serverless: there is no long-lived process, a
module-level counter dies with the invocation, and the twenty instances handling
an outage share no memory with each other (the same caveat
``app/core/rate_limit.py`` carries — there it only costs accuracy, here it would
cost one email per instance per burst, which is the flood we are preventing).
Postgres is the only durable store this stack already has a connection to, and
its partial unique index gives the guarantee for free: ``uq_service_incidents_open``
permits ONE open row per environment, so of twenty instances racing to open an
incident, one INSERT lands and nineteen hit ON CONFLICT DO NOTHING. The two
"email claims" work the same way — ``UPDATE ... WHERE alert_sent_at IS NULL
RETURNING`` can be won by exactly one transaction under READ COMMITTED.

THE OBVIOUS OBJECTION: the database may itself be what is broken, and then the
dedup store is unreachable. That case is handled, not ignored — see the DEGRADED
path at the bottom of this module. It falls back to per-process dedup with a long
cooldown, which is bounded (a handful of emails) rather than unbounded (one per
error), and it says so in the subject line so the reader is not misled.

--------------------------------------------------------------------------------
WHY THE REQUEST PATH AND NOT A CRON PROBE
--------------------------------------------------------------------------------
The issue asks for this decision explicitly. Detection happens in a middleware on
the real request path (``app/core/failure_monitor.py``), NOT in a scheduled probe
of ``/health``, because:

1. The crons this project runs are DAILY (see ``vercel.json``). A daily probe has
   a detection latency of up to 24 hours and would not have noticed a
   several-minute outage at all.
2. A cron that runs ON THIS DEPLOYMENT cannot detect this deployment being dead
   either — it is the same function. It looks like it closes the "what if the
   whole thing is down" gap, and it does not.
3. Real traffic already IS the probe, and it is far denser than any cron. The
   2026-08-18 outage was 500s served BY A LIVE FUNCTION (new code against an old
   schema), which is exactly what a middleware sees.

RESIDUAL GAP, stated honestly: if a deployment cannot boot at all (bad import —
which also happened on 2026-08-18, a dependency missing from ``uv.lock``), the
platform returns the error before any of our code runs, and nothing here can
fire. Only an OFF-PLATFORM prober hitting ``/health`` can cover that, and this
repo has nowhere to run one. That is a separate piece of work, not something this
module can fake.

--------------------------------------------------------------------------------
DELIVERY CHANNELS, INDEPENDENTLY OPTIONAL
--------------------------------------------------------------------------------
An alert is rendered ONCE, as a subject plus label/value rows. WHICH CHANNELS IT
GOES TO IS AN ENGINEER SETTING (#458), stored in ``alert_delivery_config`` and
changeable from the console with no redeploy — see
``app/services/alert_delivery.py``:

  slack_only       DEFAULT. Slack is the channel and the email (via Resend,
                   ``ALERT_EMAIL_TO``) is the BACKSTOP: it goes out ONLY if the
                   Slack post did not land -- because the channel was not
                   configured, or the post failed. Normal operation is one
                   message in one place, which is what the owner asked for after
                   the first real alert arrived twice.
  slack_and_email  Both channels on every alert; the behaviour before
                   2026-08-19, kept because "I want a copy in the mailbox" is a
                   legitimate preference.

⚠️ THE BACKSTOP SURVIVES BOTH MODES. There is no setting, and there must never
be one, under which a failed or unconfigured Slack post produces no alert at all:
a revoked webhook or a Slack outage still has to reach a person, which is why the
second channel was not simply deleted. See :func:`deliver_alert`.

Each channel is enabled by its own setting being present and by nothing else, so
a deployment can have email only, Slack only, both, or neither — and "neither" is
the default, which is what keeps local dev, CI and preview deployments silent
with no extra flag.

TWO SLACK CHANNELS, ROUTED BY PURPOSE. This module carries two kinds of news, and
they are read in two different moods:

  :data:`OPERATIONAL`  the API is failing / the API recovered (#444). Goes to
                       ``SLACK_ALERT_WEBHOOK_URL`` — #error-alerts.
  :data:`SECURITY`     login brute-force and credential guessing (#456). Goes to
                       ``SLACK_SECURITY_WEBHOOK_URL`` — #security-alerts.

The routing is deliberately ASYMMETRIC, and the asymmetry lives in
``app/core/config.py`` where the two properties are defined:

  * a SECURITY alert with no security webhook falls back to the error channel,
    because a forgotten env var must never become silence about an attack; and
  * an OPERATIONAL alert NEVER falls into the security channel, because a channel
    someone opens to answer "are we under attack?" must not fill up with 500s.

Because of that fallback the two can share a channel, so each Slack message is
tagged ``SECURITY`` or ``OUTAGE`` up front — see :func:`render_slack`.

Neither channel can raise into the caller — see the amplification rule below,
which covers two third parties instead of one. Both helpers swallow everything
and return a bool, which is what makes "send the email only if Slack failed"
expressible as a plain ``if`` rather than as exception handling.

--------------------------------------------------------------------------------
THE ALERTER MUST NOT AMPLIFY THE FAILURE
--------------------------------------------------------------------------------
Every entry point here is best-effort and NEVER raises: it runs on a request that
is already failing, and an alerter that throws turns one broken request into two.
Every path is time-boxed, a send is never retried, and an alert email is CLAIMED
BEFORE IT IS SENT (mirroring the survey sender's claim-then-send rule) so a send
that dies mid-flight cannot come back as a second email.

Nor can it RECURSE. The two things an alert delivery does are an outbound HTTP
POST and a log line. The outbound POST is not an inbound request, so it is never
seen by ``failure_alert_middleware`` and cannot open an incident about itself;
the log line goes to the platform logger, which nothing in this app monitors. The
one way to build a loop here would be to alert about a failed alert — so a failed
delivery is logged and dropped, never re-reported.

--------------------------------------------------------------------------------
THE WORDING IS EDITABLE; THE FACTS ARE NOT
--------------------------------------------------------------------------------
As of 2026-08-20 the opening and recovery SENTENCES are owner-editable from the
engineer console (``app/services/alert_templates.py``); the subject line and the
label/value rows are not. A template names placeholders and can reach nothing
else, so it can change what the alert SAYS and can never change what it KNOWS —
the rule below still holds whatever anyone types. Reading a template happens only
on this path, once per claimed message, right before the outbound POST, and it
falls back to the built-in wording on any problem. The DEGRADED alert at the
bottom of this module is deliberately NOT templated: it is the message sent when
the database is unreachable, so asking the database how to word it would fail
every single time.

--------------------------------------------------------------------------------
NO PII, NO SECRETS
--------------------------------------------------------------------------------
This content leaves the system and lands in a mailbox AND in a Slack channel. It
carries route TEMPLATES (``/alumni/{alumni_id}``), status codes, and an exception
CLASS NAME. Never a real path with ids, never a query string, never an exception
message (which can quote a row), never a header, never an alumni field, never a
key. The Slack webhook URL is itself a credential and is never logged or echoed.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from dataclasses import dataclass
from html import escape

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.core import database
from app.core.config import get_settings
from app.services import alert_delivery, alert_templates, mailer, slack

log = logging.getLogger(__name__)

# --------------------------------------------------------------------- policy --

# An incident pages a human only when BOTH are true. See the module docstring for
# why either alone is the wrong rule.
ALERT_MIN_FAILURES = 3
ALERT_MIN_SECONDS = 60

# How long the API must go without a failure before an open incident is declared
# over. Long enough that a flapping outage (fails, works, fails) stays ONE
# incident rather than becoming an open/close/open email chain.
_RECOVERY_QUIET_SECONDS = 90

# An open incident whose last failure is this old is over, whether or not anyone
# observed the recovery — the failures stopped. Reaped from the failure path so a
# quiet night (no traffic, so no success to close the incident) cannot swallow
# the alert for the NEXT morning's outage. Its overdue recovery email is sent
# when it is reaped.
_STALE_INCIDENT_SECONDS = 600

# Time budgets. These sit on a request that is already failing, so they are
# deliberately short: better a missed alert than a request held open.
_DB_TIMEOUT_SECONDS = 3.0
_EMAIL_TIMEOUT_SECONDS = 6.0
# Slack's own budget. Shorter than the email one because the two are dispatched
# CONCURRENTLY, so the delivery step costs max(email, slack) rather than their
# sum, and a webhook POST that has not answered in four seconds is not going to.
_SLACK_TIMEOUT_SECONDS = 4.0

# ------------------------------------------------------------ alert purposes --
#
# What KIND of news an alert is. This picks the Slack channel and the tag on the
# message, and nothing else — it does not change the email, the dedup, or any
# threshold. Two plain strings rather than an Enum: they are only ever compared
# here and passed as a keyword, and a string keeps the call sites readable in a
# module whose whole contract is "never do anything clever on a failing request".
OPERATIONAL = "operational"
SECURITY = "security"

# The word each kind wears at the front of its Slack message.
#
# TEXT, NOT AN ICON, and that is a judgement call worth recording. The two kinds
# normally land in different channels, so the channel name already separates
# them — except in exactly the case where separation matters most, the fallback
# where a missing security webhook sends attack alerts into #error-alerts. A
# leading uppercase word is unambiguous (a shield glyph could as easily read
# "protected, all fine"), is greppable, survives every client and every
# notification preview, and needs no exception to this project's text-only
# instinct. It is also the first thing rendered in a large bold header block, so
# it is read at a glance rather than read carefully — which was the requirement.
_SLACK_TAG = {OPERATIONAL: "OUTAGE", SECURITY: "SECURITY"}

# DEGRADED path only: minimum gap between two per-process alerts when the
# database (the real dedup store) is unreachable. 30 minutes bounds the worst
# case to a few emails per hour per warm instance.
_DEGRADED_COOLDOWN_SECONDS = 1800

# Module state for the degraded path ONLY. Deliberately the sole piece of memory
# in this module, and it is never trusted while the database is answering.
#
# None means "has never alerted", and it MUST NOT be 0.0. `time.monotonic()` is
# measured from an arbitrary origin -- on Linux, machine boot -- so on a freshly
# started instance it returns a small number. Against a 0.0 initial value the
# cooldown check below then reads as "we alerted moments ago" and swallows the
# alert for the first half hour of that instance's life. On Vercel every cold
# start is a fresh instance, so the alert most likely to be suppressed is the
# first one after a deploy, or the one where the database went down and took
# the durable dedup store with it. CI caught this because its runners boot
# seconds before the tests run; it passed locally only because that machine had
# been up for 28 hours.
_degraded_last_alert_at: float | None = None


def reset_degraded_state() -> None:
    """Clear the degraded-path cooldown. For tests (see tests/conftest.py)."""
    global _degraded_last_alert_at
    _degraded_last_alert_at = None


@dataclass(frozen=True)
class FailureSignal:
    """One observed server failure, reduced to facts that cannot carry PII.

    ``path`` is a route TEMPLATE (``/alumni/{alumni_id}``) — see
    ``app/core/failure_monitor.py`` for how it is derived and why the raw path is
    never used. ``error_kind`` is an exception class name or ``http_500``.
    """

    path: str
    status_code: int
    error_kind: str


# ----------------------------------------------------------------- statements --
#
# Raw statements rather than ORM writes on purpose: the whole guarantee is that
# POSTGRES evaluates these conditions. `ON CONFLICT ... DO NOTHING` and
# `UPDATE ... WHERE <col> IS NULL RETURNING` are the two primitives that make
# "exactly one of N concurrent instances proceeds" true, and neither survives
# being re-expressed as read-then-write in Python.

# Reap/close an open incident that has gone quiet. The recovery email is CLAIMED
# in the same statement (`recovery_sent_at`) so the transaction that wins the
# close is the one — and the only one — that owes the email.
_SQL_CLOSE_IF_QUIET = text(
    """
    UPDATE service_incidents
       SET resolved_at = now(),
           recovery_sent_at = CASE
               WHEN alert_sent_at IS NOT NULL THEN now()
               ELSE recovery_sent_at
           END,
           updated_at = now()
     WHERE environment = :environment
       AND resolved_at IS NULL
       AND last_failure_at < now() - (:quiet_seconds * interval '1 second')
    RETURNING incident_id, environment, started_at, last_failure_at, resolved_at,
              failure_count, first_path, last_path, status_code, error_kind,
              alert_sent_at
    """
)

# Open a new incident. The partial unique index means at most one open row per
# environment exists, so a loser here simply gets no row back and falls through
# to the bump below.
_SQL_OPEN = text(
    """
    INSERT INTO service_incidents
        (environment, started_at, last_failure_at, failure_count,
         first_path, last_path, status_code, error_kind)
    VALUES (:environment, now(), now(), 1,
            :path, :path, :status_code, :error_kind)
    ON CONFLICT (environment) WHERE resolved_at IS NULL DO NOTHING
    RETURNING incident_id
    """
)

# Another failure on the incident that is already open.
_SQL_BUMP = text(
    """
    UPDATE service_incidents
       SET failure_count = failure_count + 1,
           last_failure_at = now(),
           last_path = :path,
           status_code = :status_code,
           error_kind = :error_kind,
           updated_at = now()
     WHERE environment = :environment
       AND resolved_at IS NULL
    RETURNING incident_id
    """
)

# Claim the OPENING email. The sustained-failure thresholds are evaluated INSIDE
# the claim, against the committed row, so the decision and the claim are one
# atomic act — there is no window in which two instances both read "sustained,
# not yet alerted".
_SQL_CLAIM_ALERT = text(
    """
    UPDATE service_incidents
       SET alert_sent_at = now(),
           updated_at = now()
     WHERE incident_id = :incident_id
       AND alert_sent_at IS NULL
       AND resolved_at IS NULL
       AND failure_count >= :min_failures
       AND started_at <= now() - (:min_seconds * interval '1 second')
    RETURNING incident_id, environment, started_at, last_failure_at,
              failure_count, first_path, last_path, status_code, error_kind
    """
)


# ------------------------------------------------------------- state machine --


async def record_failure(session: AsyncSession, signal: FailureSignal) -> dict | None:
    """Fold one observed failure into the durable incident state.

    Returns the incident row when THIS caller won the right to send the opening
    email, and ``None`` in every other case (incident merely opened, incident
    merely bumped, threshold not met, someone else already claimed it).

    Commits before returning: the claim must be durable before an email goes out,
    never after. If the commit fails, no email is sent — which is the correct
    direction to fail, because a claim is repeatable and an email is not.
    """
    environment = get_settings().environment
    params = {
        "environment": environment,
        "path": signal.path[:200],
        "status_code": signal.status_code,
        "error_kind": signal.error_kind[:100],
    }

    opened = (await session.execute(_SQL_OPEN, params)).mappings().first()
    if opened is not None:
        # First failure of a new incident. Never alerts — one error is a blip,
        # and the thresholds below cannot be met by a row that was just created.
        await session.commit()
        return None

    bumped = (await session.execute(_SQL_BUMP, params)).mappings().first()
    if bumped is None:
        # Raced: the open incident was resolved between our INSERT and our
        # UPDATE. Nothing to alert on; the next failure re-opens.
        await session.commit()
        return None

    claimed = (
        (
            await session.execute(
                _SQL_CLAIM_ALERT,
                {
                    "incident_id": bumped["incident_id"],
                    "min_failures": ALERT_MIN_FAILURES,
                    "min_seconds": ALERT_MIN_SECONDS,
                },
            )
        )
        .mappings()
        .first()
    )
    await session.commit()
    return dict(claimed) if claimed is not None else None


async def close_if_quiet(session: AsyncSession, *, quiet_seconds: int) -> dict | None:
    """Resolve the open incident if it has gone ``quiet_seconds`` without a failure.

    Returns the incident row when THIS caller won the close AND that incident had
    actually paged someone (so a recovery email is owed); ``None`` otherwise. A
    blip that never alerted is closed silently — there is nothing to clear.
    """
    row = (
        (
            await session.execute(
                _SQL_CLOSE_IF_QUIET,
                {
                    "environment": get_settings().environment,
                    "quiet_seconds": quiet_seconds,
                },
            )
        )
        .mappings()
        .first()
    )
    await session.commit()
    if row is None or row["alert_sent_at"] is None:
        return None
    return dict(row)


# ------------------------------------------------------------------- emailing --


def _deployment_note() -> str:
    """Which build is running, from Vercel's build-time env. No secrets here —
    a commit sha and a deployment hostname are public facts about the deploy, and
    they are the first thing anyone asks during an incident."""
    sha = (os.getenv("VERCEL_GIT_COMMIT_SHA") or "")[:7]
    host = os.getenv("VERCEL_URL") or ""
    parts = [f"version {__version__}"]
    if sha:
        parts.append(f"commit {sha}")
    if host:
        parts.append(host)
    return ", ".join(parts)


def _fmt_ts(value: object) -> str:
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value or "unknown")


def _fmt_duration(start: object, end: object) -> str:
    if not isinstance(start, datetime.datetime) or not isinstance(end, datetime.datetime):
        return "unknown"
    seconds = max(0, int((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def render_alert(incident: dict) -> tuple[str, list[tuple[str, str]]]:
    """Build the OPENING email's subject and its label/value rows.

    Kept separate from sending so the exact wording is unit-testable — including
    the assertion that no PII can appear in it.
    """
    env = str(incident.get("environment") or "unknown")
    subject = (
        f"[fa-web-api {env}] API failing since {_fmt_ts(incident.get('started_at'))}"
    )
    rows = [
        ("Environment", env),
        ("Started", _fmt_ts(incident.get("started_at"))),
        ("Latest failure", _fmt_ts(incident.get("last_failure_at"))),
        # "Observed" and not "total": each instance throttles its own reporting so
        # a flood cannot become a write storm, so this is a floor on the real
        # number. Saying so stops anyone reading it as a rate.
        ("Failures observed", f"{incident.get('failure_count')} (reported; a floor, not a total)"),
        ("First failing route", str(incident.get("first_path") or "unknown")),
        ("Latest failing route", str(incident.get("last_path") or "unknown")),
        ("Status code", str(incident.get("status_code") or "unknown")),
        ("Error type", str(incident.get("error_kind") or "unknown")),
        ("Build", _deployment_note()),
        ("Incident", f"#{incident.get('incident_id')}"),
    ]
    return subject, rows


def render_recovery(incident: dict) -> tuple[str, list[tuple[str, str]]]:
    """Build the RECOVERY email's subject and rows."""
    env = str(incident.get("environment") or "unknown")
    # Both ends come from the DATABASE clock (`now()` in the statements above),
    # never from a serverless instance's own clock, so the duration is real.
    duration = _fmt_duration(incident.get("started_at"), incident.get("resolved_at"))
    subject = f"[fa-web-api {env}] API recovered after {duration}"
    rows = [
        ("Environment", env),
        ("Started", _fmt_ts(incident.get("started_at"))),
        ("Last failure", _fmt_ts(incident.get("last_failure_at"))),
        ("Duration", duration),
        ("Failures observed", str(incident.get("failure_count"))),
        ("Last failing route", str(incident.get("last_path") or "unknown")),
        ("Build", _deployment_note()),
        ("Incident", f"#{incident.get('incident_id')}"),
    ]
    return subject, rows


def _render_body(intro: str, rows: list[tuple[str, str]]) -> tuple[str, str]:
    """Return ``(html, text)`` for a label/value alert body."""
    text_lines = [intro, ""] + [f"{label}: {value}" for label, value in rows]
    html_rows = "".join(
        f"<tr><td style='padding:2px 12px 2px 0;color:#555'>{escape(label)}</td>"
        f"<td style='padding:2px 0'><strong>{escape(value)}</strong></td></tr>"
        for label, value in rows
    )
    html = (
        "<div style=\"font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;"
        'font-size:14px;color:#111">'
        f"<p>{escape(intro)}</p>"
        f"<table cellpadding='0' cellspacing='0'>{html_rows}</table>"
        "</div>"
    )
    return html, "\n".join(text_lines)


async def _send_email(subject: str, intro: str, rows: list[tuple[str, str]]) -> bool:
    """Send one operational email through the shared Resend transport.

    NEVER raises and NEVER retries. Returns True when Resend accepted it.

    No retry is a deliberate choice, not an omission: this runs while the service
    is already unhealthy, and a retry loop against a third party during an outage
    is how a monitoring feature becomes part of the outage. A failed send is
    logged at ERROR — the platform logs are the fallback channel, and the claim
    has already been made, so a failed send costs one missed email rather than a
    stream of duplicates.
    """
    settings = get_settings()
    recipients = settings.alert_recipients
    sender = settings.alert_sender
    key = settings.resend_api_key
    if not recipients or not sender or not key:
        return False

    html, plain = _render_body(intro, rows)
    payload = {
        "from": mailer.from_field(sender, settings.alert_from_name),
        "to": recipients,
        "subject": subject,
        "html": html,
        "text": plain,
    }
    try:
        response = await mailer.post_json(
            mailer.RESEND_SEND_URL,
            api_key=key,
            payload=payload,
            timeout=_EMAIL_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - the alerter must never raise
        # Includes the dev-only "Resend domain unverified" transport failures.
        log.error("failure_alert: could not reach Resend to send %r", subject)
        return False
    if not response.is_success:
        log.error(
            "failure_alert: Resend rejected the alert (HTTP %s) for %r",
            response.status_code,
            subject,
        )
        return False
    log.warning("failure_alert: emailed %r", subject)
    return True


def render_slack(
    subject: str,
    intro: str,
    rows: list[tuple[str, str]],
    *,
    purpose: str = OPERATIONAL,
    summary: str | None = None,
) -> dict:
    """Build the Slack incoming-webhook payload for an alert.

    Kept separate from sending, exactly like :func:`render_alert`, so the exact
    wording is unit-testable — including the assertion that nothing PII-shaped
    can appear in it.

    ⚠️ ``summary`` IS THE SLACK MESSAGE WHEN IT IS GIVEN, and the rows are then
    deliberately dropped. The two channels are read differently and the first
    real security alert proved it: the email's fourteen labelled rows are what
    you want when you sit down to work out what happened, and they are noise in a
    channel someone glances at on a phone. Slack answers one question — who is
    doing what to us, and what already happened about it — in a line or two. The
    detail is one click away in the mailbox and in the engineer console, and this
    function is the only place that difference lives.

    Callers that pass no summary keep the old intro-plus-rows rendering, so a new
    alert type is verbose rather than silent until someone writes it a line.

    THE TAG. The headline is prefixed with ``SECURITY`` or ``OUTAGE`` so the two
    kinds are told apart at a glance — first word, large bold header — without
    reading the rest. It matters most when the security webhook is unset and both
    kinds share #error-alerts (see ``Settings.slack_security_webhook``), and it
    costs nothing when they do not.

    The tag is applied HERE and not to :func:`render_alert`, so the EMAIL subject
    is byte-for-byte what it was before this change. Mail is already filed by
    rules people wrote against the old subjects; a channel scanned by eye is the
    thing that needed the marker.

    ``text`` is set as well as ``blocks`` deliberately: Slack uses it for the
    notification preview on a phone's lock screen and in the channel list, and a
    payload carrying only blocks shows up there as "This content can't be
    displayed". The tagged subject is the right preview — it names the kind, the
    environment and what happened.

    Every value goes through :func:`slack.escape_mrkdwn`. The values here are ours
    (route templates, counts, timestamps), but an exception CLASS NAME and a route
    template are both derived from code that changes, and a single ``<`` in a
    rendered value would silently eat the rest of that line in the channel.
    """
    esc = slack.escape_mrkdwn
    headline = f"{_SLACK_TAG.get(purpose, _SLACK_TAG[OPERATIONAL])} \u2014 {subject}"
    if summary is not None:
        # THE SUMMARY IS THE ENTIRE MESSAGE -- no header block, no subject line.
        # The owner's note on the first real one was that
        # "SECURITY - [fa-web-api production] Login abuse from 203.0.113.77:
        # 8 addresses, 8 failed attempts" is four facts he already has from the
        # sentence underneath it. A header that restates the body is not a
        # headline; it is the same message twice.
        #
        # The tag goes with it. It existed to tell attacks apart from 500s when
        # the two shared a channel; they are separate channels now, and a line
        # opening "You are being attacked by" cannot be mistaken for an outage.
        # If the fallback ever puts them back together, the sentence still says
        # which it is.
        return {
            "text": summary,
            "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": esc(summary)}}
            ],
        }
    lines = "\n".join(f"*{esc(label)}:* {esc(value)}" for label, value in rows)
    body = f"{esc(intro)}\n\n{lines}"
    return {
        "text": headline,
        "blocks": [
            {
                # A header block is plain_text, not mrkdwn — no escaping applies,
                # but Slack rejects one over 150 characters, so it is cut here
                # rather than losing the whole message to a 400. The tag is at the
                # FRONT, so it is the one thing truncation can never remove.
                "type": "header",
                "text": {"type": "plain_text", "text": headline[:150], "emoji": False},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": body},
            },
        ],
    }


def slack_target(purpose: str) -> str | None:
    """The webhook a ``purpose`` posts to, or None when that channel is off.

    The routing table, in one place and in three lines. The fallback that makes a
    SECURITY alert land in the error channel when no security webhook is set lives
    in ``Settings.slack_security_webhook``, along with the argument for why it
    exists in that direction and not the other.
    """
    settings = get_settings()
    if purpose == SECURITY:
        return settings.slack_security_webhook
    return settings.slack_webhook


async def _send_slack(
    subject: str,
    intro: str,
    rows: list[tuple[str, str]],
    *,
    purpose: str = OPERATIONAL,
    summary: str | None = None,
) -> bool:
    """Post one alert to the Slack channel this ``purpose`` routes to.

    NEVER raises and NEVER retries, for the same reasons as the email path — plus
    one that is specific to having several channels: if a Slack failure could
    raise, it would take the email down with it, and the whole point of a second
    channel is that either one alone still gets the message out.

    Returns True when Slack accepted it. A failure is logged WITHOUT the webhook
    URL: that URL is the entire credential for posting to the channel and must
    never reach the platform logs. The log line names the PURPOSE instead, which
    is what tells you which webhook to go and check.
    """
    url = slack_target(purpose)
    if not url:
        return False
    try:
        response = await slack.post_webhook(
            url,
            payload=render_slack(
                subject, intro, rows, purpose=purpose, summary=summary
            ),
            timeout=_SLACK_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - the alerter must never raise
        log.error(
            "failure_alert: could not reach Slack (%s channel) to post %r",
            purpose,
            subject,
        )
        return False
    if not response.is_success:
        # Slack answers a dead or revoked webhook with 404 `no_service` / 403
        # `invalid_token`. The response body can echo the request, so it is not
        # logged either.
        log.error(
            "failure_alert: Slack rejected the alert (HTTP %s, %s channel) for %r",
            response.status_code,
            purpose,
            subject,
        )
        return False
    log.warning("failure_alert: posted to Slack (%s channel) %r", purpose, subject)
    return True


async def deliver_alert(
    subject: str,
    intro: str,
    rows: list[tuple[str, str]],
    *,
    purpose: str = OPERATIONAL,
    slack_summary: str | None = None,
) -> bool:
    """Deliver one alert. True if it landed anywhere at all.

    WHICH CHANNELS ARE USED IS AN ENGINEER SETTING, not an env var and not a
    constant — see ``app/services/alert_delivery.py`` for why it is a row in
    Postgres and how it is read without ever raising:

      slack_only       (default) Slack is the channel and e-mail is the BACKSTOP.
                       One message in one place, which is what the owner asked
                       for after the first real alert arrived twice.
      slack_and_email  Both, every time — the behaviour before 2026-08-19.

    ⚠️ THE E-MAIL BACKSTOP SURVIVES BOTH MODES, AND THAT IS THE POINT. The ONLY
    branch below that does not send the e-mail is reached when Slack actually
    LANDED. A failed post, a rejected post, a revoked webhook, an unconfigured
    channel — every one of those returns False from :func:`_send_slack` and
    falls through to the mail, in either mode. The setting chooses whether
    e-mail is a copy or a backstop; it can never choose silence, because a
    single channel that breaks IS silence and silence is the failure this module
    exists to prevent. ``tests/test_alert_delivery.py`` asserts it for every mode
    in :data:`alert_delivery.MODES`, not just for the one that looks risky, so a
    third mode added later cannot quietly opt out of it.

    ``slack_summary`` is the SHORT form, and only Slack gets it: the mail keeps
    every row. See :func:`render_slack` for why the two channels deliberately say
    different amounts.

    ``purpose`` selects which Slack channel the message routes to and which tag it
    wears (see :data:`OPERATIONAL` / :data:`SECURITY`). It does not affect email:
    there is one alert mailbox, and a mailbox is already searchable.

    Renders once and fans out. The sends run CONCURRENTLY with
    ``return_exceptions=True`` so that:

      * the delivery step costs max(email, slack) rather than their sum — this
        sits on a request that is already failing, so wall time matters; and
      * one channel being down cannot suppress the other. That independence is
        the entire reason for having a second channel, and an
        ``await email(); await slack()`` sequence would quietly lose it the first
        time the leading call raised.

    ``return_exceptions=True`` is belt-and-braces: both helpers already swallow
    everything. It is here so that a future edit forgetting that rule degrades
    into a missed alert rather than an exception raised on the failure path of an
    already-broken request.
    """
    # Reading the setting can never fail and never raises -- an unreadable value
    # is the default, which is the mode that sends MORE when Slack is unhealthy.
    mode = await alert_delivery.read_mode()

    if mode == alert_delivery.SLACK_AND_EMAIL:
        # BOTH CHANNELS, EVERY TIME. Dispatched CONCURRENTLY with
        # ``return_exceptions=True`` so that the delivery step costs
        # max(email, slack) rather than their sum -- this sits on a request that
        # is already failing -- and so that one channel being down cannot
        # suppress the other. An ``await email(); await slack()`` sequence would
        # quietly lose that independence the first time the leading call raised,
        # and independence is the entire reason for having a second channel.
        results = await asyncio.gather(
            _send_slack(subject, intro, rows, purpose=purpose, summary=slack_summary),
            _send_email(subject, intro, rows),
            return_exceptions=True,
        )
        slack_ok, email_ok = (r is True for r in results)
        return slack_ok or email_ok

    # SLACK ONLY: Slack is the channel, e-mail is the BACKSTOP. Both used to be
    # sent every time, which is why one attack produced one Slack message AND one
    # e-mail. The owner asked for it all in Slack, and the honest way to do that
    # is not to delete the second channel.
    #
    # So: post to Slack, and send the e-mail ONLY if that did not land. It also
    # costs less on the failing request than the fan-out above in the common
    # case -- one call, not two -- and pays for both only when the first has
    # already failed.
    slack_ok = await _send_slack(
        subject, intro, rows, purpose=purpose, summary=slack_summary
    )
    if slack_ok:
        # ⚠️ THE ONE BRANCH THAT SKIPS THE E-MAIL, and it is reachable only when
        # Slack returned True -- i.e. Slack accepted the message. Nothing else in
        # this function may ever return without having tried a channel that
        # worked. Do not add a condition here.
        return True
    # Slack was unconfigured, unreachable, or rejected the post. The backstop is
    # why there are two channels at all.
    return await _send_email(subject, intro, rows) is True


# --------------------------------------------------------------- entry points --


async def deliver_test_alert(*, purpose: str, requested_by: str | None) -> dict:
    """Push one clearly-marked TEST message through the REAL delivery path.

    WHY THIS EXISTS. Until now "is alerting actually wired up?" could only be
    answered by breaking something: an outage alert needs three sustained
    failures, so proving the operational channel meant deliberately 5xx-ing
    production for a minute. That is a bad trade for a question worth asking
    routinely — after rotating a webhook, after moving a channel, after a deploy
    that touched the env vars.

    It exercises the same renderer, the same fan-out and the same webhooks a real
    alert uses. What it deliberately does NOT do is touch ``service_incidents`` or
    ``login_abuse_incidents``: nothing is opened, claimed or resolved, so a test
    cannot suppress the alert for a real incident that starts a second later.

    ⚠️ REPORTS PER CHANNEL, and that is the point. "Nothing arrived" has two very
    different causes — the channel is not configured, or it is configured and the
    send failed — and a single boolean cannot tell them apart. That distinction is
    exactly what was missing on 2026-08-19, when a security alert landed in
    #error-alerts and the reason (no ``SLACK_SECURITY_WEBHOOK_URL``, so the
    documented one-way fallback fired) was not visible from anywhere.
    """
    tag = _SLACK_TAG.get(purpose, _SLACK_TAG[OPERATIONAL])
    subject = f"[fa-web-api {get_settings().environment}] TEST — {tag.lower()} alerting"
    intro = (
        "This is a test. Nothing is wrong and no incident was opened — an "
        "engineer pressed the button that checks this channel is reachable."
    )
    rows = [
        ("Environment", str(get_settings().environment)),
        ("Channel", purpose),
        ("Requested by", requested_by or "an engineer"),
        ("Build", _deployment_note()),
    ]
    # The Slack line is a line, exactly like a real alert's.
    summary = f"Test message — {purpose} alerting is reaching this channel."

    slack_configured = slack_target(purpose) is not None
    email_configured = email_alerting_enabled()
    slack_ok, email_ok = False, False
    if slack_configured or email_configured:
        results = await asyncio.gather(
            _send_slack(subject, intro, rows, purpose=purpose, summary=summary),
            _send_email(subject, intro, rows),
            return_exceptions=True,
        )
        slack_ok, email_ok = (r is True for r in results)

    return {
        "purpose": purpose,
        "slack_configured": slack_configured,
        "slack_delivered": slack_ok,
        "email_configured": email_configured,
        "email_delivered": email_ok,
        # The fallback that surprised everyone the first time, stated up front so
        # the console can say "this went to the error channel" rather than the
        # reader working it out from which Slack channel pinged.
        "fell_back_to_error_channel": (
            purpose == SECURITY
            and slack_configured
            and get_settings().slack_security_webhook == get_settings().slack_webhook
        ),
    }



def email_alerting_enabled() -> bool:
    """Email alerting is ON only when there is somewhere to send and something to
    send with. Unset recipients is its single off switch."""
    settings = get_settings()
    return bool(settings.alert_recipients and settings.alert_sender and settings.resend_api_key)


def slack_alerting_enabled() -> bool:
    """Slack alerting is ON iff AT LEAST ONE webhook URL is configured. A URL is
    both the destination and the credential, so there is nothing else to check —
    and one setting per channel means one off switch each, exactly matching
    ``ALERT_EMAIL_TO``.

    Written as an explicit OR even though ``slack_security_webhook`` currently
    falls back to ``slack_webhook`` and so subsumes it: this must keep reading as
    "any channel at all" if that fallback is ever narrowed."""
    settings = get_settings()
    return bool(settings.slack_webhook or settings.slack_security_webhook)


def alerting_enabled() -> bool:
    """Alerting is ON when AT LEAST ONE channel is configured.

    Unset-means-off, per channel, is what keeps local runs, the test suite, CI and
    preview deployments silent with no second flag to remember — and OR-ing the
    channels is what lets a deployment page Slack without a mailbox, or the
    reverse. Every piece of detection work in this module and in
    ``failure_monitor`` is gated on this, so "nothing configured" still costs
    nothing on the hot path of every request.

    ONE GATE FOR ALL PURPOSES, on purpose. It is deliberately not split per
    channel: the only configuration it is imprecise for is "security webhook set,
    nothing else", where outage incidents would still be tracked in
    ``service_incidents`` with nowhere to deliver — a few rows, no messages, and
    no wrong behaviour. Splitting it would put a purpose argument through the
    request middleware to save that, which is a worse trade on the hot path of
    every request."""
    return email_alerting_enabled() or slack_alerting_enabled()


async def note_failure(signal: FailureSignal, *, process_sustained: bool) -> None:
    """Record one observed server failure and, if it opens an incident, alert.

    Best-effort in every direction: never raises, never blocks longer than the
    budgets above. ``process_sustained`` is the CALLING PROCESS's own opinion
    that failure has persisted, used only when the database is unreachable and
    cannot be asked.
    """
    if not alerting_enabled():
        return
    try:
        await asyncio.wait_for(
            _note_failure_durable(signal),
            timeout=_DB_TIMEOUT_SECONDS + _EMAIL_TIMEOUT_SECONDS + 1.0,
        )
    except Exception:  # noqa: BLE001 - never let monitoring break a request
        # The dedup store is unreachable, which very often means the database IS
        # the outage. Fall back to bounded per-process alerting rather than
        # staying silent about the failure we are most likely to be having.
        log.warning("failure_alert: durable path unavailable; trying degraded alert")
        await _degraded_alert(signal, process_sustained=process_sustained)


async def _note_failure_durable(signal: FailureSignal) -> None:
    if database.SessionLocal is None:
        raise RuntimeError("no database configured")
    # All database work first, session closed, THEN email. A connection is a
    # scarce resource on this stack (see the pooler notes in core/database.py)
    # and must not be held open across a third-party HTTP call.
    stale: dict | None = None
    incident: dict | None = None
    try:
        async with database.SessionLocal() as session:
            # Reap an incident whose failures stopped long ago but which nobody
            # was around to close (a quiet night with no successful request to
            # notice). Without this, one unclosed incident silently swallows the
            # alert for the NEXT outage.
            stale = await asyncio.wait_for(
                close_if_quiet(session, quiet_seconds=_STALE_INCIDENT_SECONDS),
                timeout=_DB_TIMEOUT_SECONDS,
            )
            incident = await asyncio.wait_for(
                record_failure(session, signal), timeout=_DB_TIMEOUT_SECONDS
            )
    finally:
        # A reaped incident's recovery email is CLAIMED and committed by the time
        # we get here, so it will never be offered again. Send it even if the
        # statement after it blew up, or an unresolved alert sits in the mailbox
        # forever. The session is already closed — the `finally` runs after the
        # `async with` exits — so no connection is held across the send.
        if stale is not None:
            await _send_recovery(stale)
    if incident is None:
        return
    subject, rows = render_alert(incident)
    await deliver_alert(subject, await _outage_line(incident, opening=True), rows)


async def note_success() -> None:
    """Close the open incident if the API has been quiet of failures long enough,
    and send the single recovery email. Best-effort; never raises.

    Called on a SAMPLED successful response (see ``failure_monitor``), not on
    every one — recovery has to be prompt, because an incident that stays open
    blocks the alert for the next one, but it does not have to cost a query per
    request.
    """
    if not alerting_enabled():
        return
    try:
        await asyncio.wait_for(
            _note_success_durable(),
            timeout=_DB_TIMEOUT_SECONDS + _EMAIL_TIMEOUT_SECONDS + 1.0,
        )
    except Exception:  # noqa: BLE001 - never let monitoring break a request
        log.warning("failure_alert: recovery check failed", exc_info=False)


async def _note_success_durable() -> None:
    if database.SessionLocal is None:
        return
    async with database.SessionLocal() as session:
        incident = await asyncio.wait_for(
            close_if_quiet(session, quiet_seconds=_RECOVERY_QUIET_SECONDS),
            timeout=_DB_TIMEOUT_SECONDS,
        )
    if incident is not None:
        await _send_recovery(incident)


def outage_template_values(incident: dict) -> dict:
    """The facts an OUTAGE template may name, and nothing else.

    The counterpart of ``login_abuse._template_values`` and the same boundary: a
    stored template reaches exactly what is in this dict, narrowed further by the
    placeholders its kind declares. Everything here is already in the alert's
    label/value rows and is subject to the same NO PII, NO SECRETS rule at the top
    of this module — a route TEMPLATE, never a real path; an exception CLASS NAME,
    never a message.
    """
    return {
        "environment": str(incident.get("environment") or "unknown"),
        "started": _fmt_ts(incident.get("started_at")),
        "failures": str(incident.get("failure_count") or 0),
        "route": str(
            incident.get("last_path") or incident.get("first_path") or "unknown"
        ),
        "status_code": str(incident.get("status_code") or "unknown"),
        "error_kind": str(incident.get("error_kind") or "unknown"),
        "duration": _fmt_duration(
            incident.get("started_at"),
            incident.get("resolved_at") or incident.get("last_failure_at"),
        ),
    }


async def _outage_line(incident: dict, *, opening: bool) -> str:
    """The editable opening / recovery sentence for an outage alert.

    ⚠️ THE READ HAPPENS HERE, ON THE ALERTING PATH, AND NOWHERE ELSE. By the time
    this runs the email has already been CLAIMED — exactly one instance in the
    whole outage reaches this line, and it is about to spend seconds on an
    outbound POST. Nothing in ``failure_monitor``'s middleware, and no request
    that is merely failing, ever touches the template table. ``load`` never
    raises and never blocks for long; ``{}`` means "say what the code has always
    said".

    Unlike the two security lines this one heads the EMAIL as well as the Slack
    message (an outage alert has no short-form summary — see ``render_slack``), so
    editing it changes both. That is the honest behaviour: it is one sentence with
    one meaning, and having the mail and the channel disagree about it would be
    worse than either wording.
    """
    kind = (
        alert_templates.OUTAGE_OPENING if opening else alert_templates.OUTAGE_RECOVERED
    )
    return alert_templates.render(
        kind,
        outage_template_values(incident),
        templates=await alert_templates.load(),
    )


async def _send_recovery(incident: dict) -> None:
    subject, rows = render_recovery(incident)
    await deliver_alert(subject, await _outage_line(incident, opening=False), rows)


async def _degraded_alert(signal: FailureSignal, *, process_sustained: bool) -> None:
    """Alert without the durable dedup store, because it is unreachable.

    THE TRADE, stated plainly: dedup falls back to this process's memory, so N
    warm serverless instances can each send one email per cooldown. That is
    bounded (a handful) rather than unbounded (one per error), and it is the only
    way to hear about the failure mode where the DATABASE is the outage — which
    is precisely the one that took the site down on 2026-08-18. The subject says
    the dedup was degraded so the reader knows why they may see it twice.

    Still requires sustained failure: a single blip plus a flaky connection must
    not page anyone.
    """
    global _degraded_last_alert_at
    if not process_sustained:
        return
    now = time.monotonic()
    if (
        _degraded_last_alert_at is not None
        and now - _degraded_last_alert_at < _DEGRADED_COOLDOWN_SECONDS
    ):
        return
    # Claim before sending, same rule as the durable path.
    _degraded_last_alert_at = now
    env = get_settings().environment
    await deliver_alert(
        f"[fa-web-api {env}] API failing (degraded alerting: dedup store unreachable)",
        "The API is failing AND the incident store could not be reached, which "
        "usually means the database itself is the problem. This alert is "
        "de-duplicated per instance only, so you may receive it more than once.",
        [
            ("Environment", env),
            ("Failing route", signal.path),
            ("Status code", str(signal.status_code)),
            ("Error type", signal.error_kind),
            ("Build", _deployment_note()),
        ],
    )

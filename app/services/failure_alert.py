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
THE ALERTER MUST NOT AMPLIFY THE FAILURE
--------------------------------------------------------------------------------
Every entry point here is best-effort and NEVER raises: it runs on a request that
is already failing, and an alerter that throws turns one broken request into two.
Every path is time-boxed, a send is never retried, and an alert email is CLAIMED
BEFORE IT IS SENT (mirroring the survey sender's claim-then-send rule) so a send
that dies mid-flight cannot come back as a second email.

--------------------------------------------------------------------------------
NO PII, NO SECRETS
--------------------------------------------------------------------------------
This content leaves the system and lands in a mailbox. It carries route TEMPLATES
(``/alumni/{alumni_id}``), status codes, and an exception CLASS NAME. Never a
real path with ids, never a query string, never an exception message (which can
quote a row), never a header, never an alumni field, never a key.
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
from app.services import mailer

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

# DEGRADED path only: minimum gap between two per-process alerts when the
# database (the real dedup store) is unreachable. 30 minutes bounds the worst
# case to a few emails per hour per warm instance.
_DEGRADED_COOLDOWN_SECONDS = 1800

# Module state for the degraded path ONLY. Deliberately the sole piece of memory
# in this module, and it is never trusted while the database is answering.
_degraded_last_alert_at: float = 0.0


def reset_degraded_state() -> None:
    """Clear the degraded-path cooldown. For tests (see tests/conftest.py)."""
    global _degraded_last_alert_at
    _degraded_last_alert_at = 0.0


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


async def send_alert_email(subject: str, intro: str, rows: list[tuple[str, str]]) -> bool:
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
    log.warning("failure_alert: sent %r", subject)
    return True


# --------------------------------------------------------------- entry points --


def alerting_enabled() -> bool:
    """Alerting is ON only when there is somewhere to send and something to send
    with. Unset recipients is the single off switch — it keeps local runs, the
    test suite, CI and preview deployments silent with no second flag."""
    settings = get_settings()
    return bool(settings.alert_recipients and settings.alert_sender and settings.resend_api_key)


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
    await send_alert_email(
        subject,
        "The API has been failing for long enough to be an incident. "
        "You will get one more email when it clears.",
        rows,
    )


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


async def _send_recovery(incident: dict) -> None:
    subject, rows = render_recovery(incident)
    await send_alert_email(
        subject, "The API is serving requests again. This incident is closed.", rows
    )


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
    if now - _degraded_last_alert_at < _DEGRADED_COOLDOWN_SECONDS:
        return
    # Claim before sending, same rule as the durable path.
    _degraded_last_alert_at = now
    env = get_settings().environment
    await send_alert_email(
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

"""Survey-email send service (Resend).

Sends the annual "confirm your info" survey to every eligible alum in a
graduation year. Each recipient gets a personalized email with a UNIQUE,
HMAC-signed link to `<SURVEY_APP_BASE_URL>/survey/<token>`; the token carries the
alum's id so the landing page can (later) load their record and save edits.

Design notes:
- There is ONE way to send: `send_survey_stage`. It owns choosing the stage,
  working out who is owed it, claiming them in `survey_send_log`, calling Resend,
  writing the audit row and committing — for BOTH the console's manual send and
  the daily cron. `_send_batch` is private; nothing outside this module can email
  an alum without recording it. The manual send previously called the raw sender
  without the send-log callback, which is what let a whole cohort be emailed with
  no record and then emailed again by the cron two days later (2026-08-02).
- Delivery is CLAIMED BEFORE IT IS SENT (insert + commit, then Resend). Emailing
  is irreversible and a log row is not, so this fails toward "possibly missed"
  rather than "sent twice".
- Only ONE send runs at a time, cron or manual: `send_lock` is a Postgres
  advisory lock held for the whole send (#358). The claim already stops two
  runners emailing the same alum; the lock is what stops them each spending the
  full daily budget from independent reads of it.
- `survey_send_log` is the usage ledger (`get_send_usage`), not the audit trail —
  an engineer actor's audit row is rerouted to `engineer_action_log` and would
  read as zero usage.
- The Resend API key lives ONLY in backend config (`RESEND_API_KEY`); it is never
  exposed to the frontend.
- `dry_run=True` (the endpoint default) builds and counts everything but sends
  nothing — safe to run before the domain/key are live.
- Outbound HTTP mirrors `supabase_admin.py`: a short-timeout `httpx.AsyncClient`
  that raises `ServiceError` on misconfig / transport / non-2xx, never leaking
  upstream bodies.
- The token copy here is the Career Directors' default wording. A staff-editable
  message lives in the frontend prototype only; wiring that through the backend
  is a later step.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from html import escape

import httpx
from sqlalchemy import and_, case, delete, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import database, email_reach
from app.core.config import get_settings
from app.core.dropdowns import SUPPRESSED_CONTACT_STATUS_LABELS, holds_designation
from app.core.errors import ConflictError, ServiceError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.survey_reset import SurveyResetLog
from app.models.survey_response import SurveyResponse
from app.models.survey_retirement import SurveyCampaignRetirement
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.repositories.alumni import build_alumni_query, has_status_label_exists
from app.schemas.survey import (
    GraduationYearCount,
    SurveyHeldOutAlum,
    SurveyHeldOutPage,
    SurveyRecipientBreakdown,
    SurveyRespondInfo,
    SurveySendResult,
    SurveySendSample,
    SurveyUnreachableAlum,
    SurveyUsage,
)

log = logging.getLogger(__name__)

_RESEND_BATCH_URL = "https://api.resend.com/emails/batch"

# The survey is ANNUAL: once an alum replies, they're not re-surveyed for a year.
# A response submitted on/after this cutoff counts as "already surveyed this
# cycle" — used both to exclude them from a send and to count real replies.
_RESURVEY_INTERVAL_DAYS = 365

# Which `survey_responses.status` values count as "they replied this cycle".
#
# `rejected` is DELIBERATELY absent. A rejected response is one staff THREW AWAY
# (spam, junk, someone else's data) — nothing was written to the record, so the
# alum has effectively not replied and must stay surveyable. Counting it both
# silenced them for 365 days and reported them to the console as "replied", i.e.
# complete. This tuple is the single definition, shared by the send exclusion
# (:func:`_load_recipients`) and the console's responded tally
# (:func:`list_graduation_years`) — they must never drift.
RESPONDED_STATUSES: tuple[str, ...] = ("pending", "applied")


# --------------------------------------------------- engineer reset (#395) ----
#
# A per-alumnus reset makes ONE person surveyable again. It used to do that by
# DELETING their responses and send-log rows; it now records an event in
# `survey_reset_log` and DELETES NOTHING (Jake, 2026-08-05: "when you reset the
# campaign the responses should not be reset, they should still be in the db").
#
# The two predicates below are how that stays true. EVERY query that asks "has
# this person already replied?" or "have we already emailed them?" must apply the
# matching one, or the console and the sender end up with different populations —
# the standing bug class in this area. The full list of callers is in
# `services/survey_reset.py`.
#
# They are asymmetric, on purpose:
#
# * responses are compared by TIME, because the requirement is that those rows
#   are not written to at all — there is no column on them to compare;
# * send-log rows are compared by `reset_seq`, which they had to grow anyway so
#   the UNIQUE (year, alumni, stage, cycle, reset_seq) would admit the re-send.
#   Exact integer identity, no clock involved.


def response_not_superseded():
    """Correlated NOT EXISTS: no reset has happened since this response.

    Compares `survey_responses.submitted_at` against the alum's reset times, so
    the response row itself is never touched. A response submitted AFTER the
    reset counts normally again — answering post-reset re-blocks them, which is
    the point.

    Clock note: `reset_at` is stamped by the API and `submitted_at` by Postgres.
    A skew of a second could leave a response submitted a moment before a reset
    still counting as a reply, i.e. the alum stays blocked and the engineer sees
    it and resets again — the safe direction to be wrong in.
    """
    return ~(
        select(SurveyResetLog.survey_reset_id)
        .where(
            SurveyResetLog.alumni_id == SurveyResponse.alumni_id,
            SurveyResetLog.reset_at >= SurveyResponse.submitted_at,
        )
        .exists()
    )


def send_not_superseded():
    """Correlated NOT EXISTS: this delivered email predates no later reset.

    A send-log row records the alum's reset count at the moment it was claimed,
    so "superseded" is simply "a reset with a higher sequence exists" — no
    timestamps, and it agrees with the unique key by construction.
    """
    return ~(
        select(SurveyResetLog.survey_reset_id)
        .where(
            SurveyResetLog.alumni_id == SurveySendLog.alumni_id,
            SurveyResetLog.reset_seq > SurveySendLog.reset_seq,
        )
        .exists()
    )


async def reset_seq_for(session: AsyncSession, alumni_ids: list[int]) -> dict[int, int]:
    """How many times each of these alumni has been reset (absent = never).

    The value a new `survey_send_log` row must carry. Bulk, because it is
    resolved for a whole cohort inside `_load_recipients` and must not become
    one round trip per recipient.
    """
    if not alumni_ids:
        return {}
    stmt = (
        select(SurveyResetLog.alumni_id, func.max(SurveyResetLog.reset_seq))
        .where(SurveyResetLog.alumni_id.in_(alumni_ids))
        .group_by(SurveyResetLog.alumni_id)
    )
    return {
        alumni_id: int(seq or 0)
        for alumni_id, seq in (await session.execute(stmt)).all()
    }

# --------------------------------------------------------------- send stages --
#
# A campaign sends in three stages: the initial email, then a 1-week and a 2-week
# reminder to whoever has not replied. `survey_send_log` is UNIQUE on
# (graduation_year, alumni_id, stage, cycle_seq), so the stage is what makes
# "have we already emailed this person?" answerable WITHIN a campaign, and
# `cycle_seq` is what keeps that question about THIS campaign rather than all
# time (#357) — and it is why a manual send must
# pick a REAL stage rather than a synthetic "manual" one: a `stage = -1` would
# satisfy the unique constraint alongside a stage-0 cron row and let the same
# alum be emailed twice, which is exactly the incident this all exists to stop.
#
# These live here, not in `survey_schedule`, because they describe the send log —
# which both the scheduled and the manual sender now write. `survey_schedule`
# re-exports them so its own callers/tests are unaffected.
#
# THE CADENCE IS SETTLED (Jake, 2026-08-03 — #359). Reminders go out one week and
# two weeks after the initial. Issue #151's original text said two weeks then
# three; the shipped 7/14 is what was chosen, and #151 is amended to match. So
# these offsets are a DECISION, not an accident of implementation — do not
# "correct" them toward #151's older wording.
STAGE_INITIAL = 0
STAGE_REMINDER_1 = 1
STAGE_REMINDER_2 = 2
_STAGE_WINDOW_DAYS = 7  # each stage covers a 7-day window from start_date


def stage_for(elapsed_days: int) -> int | None:
    """Which stage's WINDOW ``elapsed_days`` after the start falls in.

    0 for the first week, 1 for the second, 2 for the third; ``None`` once the
    2-week-reminder window has passed.

    ``None`` means "the calendar has run out of windows" — it does NOT mean the
    campaign is finished. Completion is decided from what has actually been
    delivered (see :func:`select_stage_targets` and
    ``survey_schedule.run_due_schedules``); this function only ever CAPS which
    stage may go out. Treating its ``None`` as "done" is what silently completed
    cohorts that had never been emailed at all."""
    if elapsed_days < 0:
        return None  # not started yet
    stage = elapsed_days // _STAGE_WINDOW_DAYS
    return stage if stage <= STAGE_REMINDER_2 else None


def ceiling_stage_for(elapsed_days: int) -> int:
    """The HIGHEST stage that may be sent ``elapsed_days`` after the start.

    Same windows as :func:`stage_for`, but total: before the start only the
    initial is permitted, and once every window has gone by all three stages are
    permitted so stragglers from any stage can still be finished."""
    if elapsed_days < 0:
        return STAGE_INITIAL
    window = stage_for(elapsed_days)
    return STAGE_REMINDER_2 if window is None else window


def _resurvey_cutoff() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        days=_RESURVEY_INTERVAL_DAYS
    )


FIRST_CYCLE = 1
"""The cycle a year is on before anyone has started a second campaign for it —
and the cycle a send belongs to when the year has no schedule row at all (a
manual console send for an unscheduled year). Matches the migration's backfill,
so pre-#357 log rows and a fresh manual send agree."""


async def retired_cycle_seq(session: AsyncSession, graduation_year: int) -> int:
    """The highest cycle DELETED for this year, or 0 if none has been (#398).

    A deleted campaign's schedule row is gone, so the only trace of the cycle it
    held is its ``survey_campaign_retirement`` row. That number is what keeps a
    later campaign for the year from landing back on top of the retired sends."""
    seq = (
        await session.execute(
            select(func.max(SurveyCampaignRetirement.cycle_seq)).where(
                SurveyCampaignRetirement.graduation_year == graduation_year
            )
        )
    ).scalar()
    return int(seq or 0)


async def next_cycle_seq(session: AsyncSession, graduation_year: int) -> int:
    """The cycle a BRAND-NEW campaign for this year must start on (#398).

    One above the highest retired cycle, or 1 for a year that has never had a
    campaign deleted. Split out from :func:`current_cycle_seq` so creating a
    schedule — which already knows there is no existing row — asks exactly one
    question instead of two, and so the "start above the retired sends" rule has
    a single implementation both callers share."""
    retired = await retired_cycle_seq(session, graduation_year)
    return retired + 1 if retired else FIRST_CYCLE


async def current_cycle_seq(session: AsyncSession, graduation_year: int) -> int:
    """The campaign cycle a send for this year belongs to right now (#357, #398).

    THE one resolver. Everything that has to pick a cycle for a year without a
    schedule row in hand comes through here — the manual console send, and the
    creation of a new schedule (``survey_schedule._upsert_schedule``) — so the
    two cannot answer differently and leave a send stranded in a cycle no
    campaign is on.

    Three cases, in order:

    * **The year has a schedule** — its ``cycle_seq``, which is the only thing
      ``start_new_cycle`` ever advances.
    * **The year's campaign was DELETED** (#398) — one above the highest retired
      cycle. The retired campaign's send-log rows keep their own cycle number and
      become history: the cycle-scoped double-send guard no longer sees them, so
      the alumni it emailed are eligible again, and the send log's UNIQUE
      (year, alumni, stage, cycle, reset) cannot collide with them, so the new
      claims are really inserted rather than swallowed by ON CONFLICT DO NOTHING.
      Resolving to 1 here instead is exactly the #357 failure the delete used to
      be refused over: every alum reads as already emailed and the campaign
      completes having sent nothing.
    * **Neither** — cycle 1, the value the #357 migration backfilled onto
      existing rows, so a manual send for a never-scheduled year lands in the
      same bucket before and after that change. Repeated manual sends for such a
      year stay in that one cycle, which is what makes their stage dedupe work.
    """
    seq = (
        await session.execute(
            select(SurveySchedule.cycle_seq).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()
    if seq is not None:
        return seq
    return await next_cycle_seq(session, graduation_year)


async def sent_cycle_and_stage(
    session: AsyncSession, graduation_year: int | None, alumni_id: int
) -> tuple[int | None, int | None]:
    """The (cycle, stage) of the LAST survey email this alum was actually sent,
    or ``(None, None)`` if there is no such row (#497).

    This is the read `survey_responses.submit_response` stamps a response from,
    so a reply can be attributed to the campaign that asked for it. Every count
    in the console joins the year's CURRENT cycle, so once a year runs its second
    campaign the first one's responses become unreportable — and unrecoverable,
    because nothing on the row says which cycle it was.

    OBSERVED, NOT INFERRED. The cycle returned is READ OFF a send-log row; it is
    never computed from a date and never guessed from the year's current
    schedule. That matters in the two cases where those disagree:

    * A campaign that was DELETED (#398) keeps its send-log rows with their own
      cycle number, but has no schedule row — :func:`current_cycle_seq` answers
      one ABOVE the retired cycle for such a year, which is the cycle of the NEXT
      campaign, not the one that sent the email being replied to.
    * A year with no campaign at all has no rows here, so the answer is "unknown"
      rather than the ``FIRST_CYCLE`` fallback a schedule read would invent.

    "No row" is a real and expected answer — a hand-issued link, a dev token, or
    an alum whose ``graduation_year`` changed after they were emailed. The caller
    stores NULL for it. A wrong stamp is worse than a missing one: NULL is
    excludable in a report, a plausible-looking wrong number is not.

    The year is part of the lookup so the stored ``(graduation_year, cycle_seq)``
    pair stays coherent — a cycle number is only meaningful against the year it
    counts for, and pairing this year with another year's cycle would be exactly
    the kind of wrong stamp above.

    Ordering is by ``sent_at`` (newest first, id as the tie-break) purely to pick
    the most recent ROW; the value returned is that row's stored ``cycle_seq``.
    Rows superseded by an engineer reset (#395) are deliberately NOT filtered
    out: they still record an email that really went out, and the newest row is
    at the current reset generation anyway whenever one exists.
    """
    if graduation_year is None:
        return (None, None)
    row = (
        await session.execute(
            select(SurveySendLog.cycle_seq, SurveySendLog.stage)
            .where(
                SurveySendLog.graduation_year == graduation_year,
                SurveySendLog.alumni_id == alumni_id,
            )
            .order_by(
                SurveySendLog.sent_at.desc(),
                SurveySendLog.survey_send_log_id.desc(),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return (None, None)
    return (row[0], row[1])


async def logged_alumni_ids(
    session: AsyncSession, graduation_year: int, stage: int, cycle_seq: int
) -> set[int]:
    """alumni_ids already emailed for (year, stage, cycle) — the double-send guard.

    ``cycle_seq`` is NOT optional and must not be defaulted: an unscoped read
    here is exactly the #357 bug (the guard becomes an all-time question, so a
    second campaign for the year finds everyone already logged and emails
    nobody)."""
    stmt = select(SurveySendLog.alumni_id).where(
        SurveySendLog.graduation_year == graduation_year,
        SurveySendLog.stage == stage,
        SurveySendLog.cycle_seq == cycle_seq,
        # An engineer reset makes the rows that predate it inert history (#395):
        # they still record a real email, but they no longer hold the person out
        # of this stage. Without this the reset would unblock the 365-day
        # response window and nothing else, and the alum would stay unsendable.
        send_not_superseded(),
    )
    return set((await session.execute(stmt)).scalars().all())


async def get_send_usage(session: AsyncSession) -> SurveyUsage:
    """Real send usage for the console tallies: emails actually sent today and
    this calendar month, counted from ``survey_send_log``.

    The ledger is the send log, NOT the audit trail. Audit rows are still written
    for every send, but they cannot be counted on: an ENGINEER actor's
    ``AuditLog`` is rerouted into ``engineer_action_log`` by the audit hook
    (#199), so an engineer's manual send left the meter reading zero and the
    scheduler handed out a budget that was already spent. The send log has no
    such hole — one row per email, inserted and committed in the same
    transaction as the delivery claim, by both senders.

    Day/month boundaries are UTC, matching the app's other date filtering. Dry
    runs never claim, so they never appear here."""
    settings = get_settings()
    anchor = settings.survey_usage_baseline_at
    now = datetime.datetime.now(datetime.UTC)
    start_today = datetime.datetime.combine(
        now.date(), datetime.time.min, tzinfo=datetime.UTC
    )
    start_month = start_today.replace(day=1)
    stmt = select(
        func.count().label("month"),
        func.count()
        .filter(SurveySendLog.sent_at >= start_today)
        .label("today"),
    ).where(SurveySendLog.sent_at >= start_month)
    if anchor is not None:
        # Baseline set: the baseline covers everything up to the anchor, so only
        # count sends recorded strictly AFTER it (avoids double-counting).
        stmt = stmt.where(SurveySendLog.sent_at > anchor)
    row = (await session.execute(stmt)).first()
    sent_this_month, sent_today = (row[0] or 0, row[1] or 0) if row else (0, 0)
    if anchor is not None:
        # Add the baseline only while we're still in the anchor's day / month.
        if now.date() == anchor.date():
            sent_today += settings.survey_usage_baseline_today
        if (now.year, now.month) == (anchor.year, anchor.month):
            sent_this_month += settings.survey_usage_baseline_month
    return SurveyUsage(sent_today=sent_today, sent_this_month=sent_this_month)


_TIMEOUT_SECONDS = 20.0
# Resend caps a batch call at 100 messages.
_BATCH_MAX = 100

_SUBJECT = "Your BYU Finance alumni information — a quick update"

_INTRO = (
    "Our BYU Finance alumni are one of the greatest strengths of our program. "
    "We are working to strengthen our alumni community by staying connected with "
    "you throughout your career. To do that, we're reaching out to ensure we have "
    "your most current information.\n\n"
    "Please take a moment to review the information below and update or replace "
    "any information in our alumni survey that is wrong or missing."
)
_CLOSING = (
    "If everything above is correct, please confirm that your information is up "
    "to date at the bottom of the survey. If anything has changed, please update "
    "the applicable questions in the survey. We have also included a few optional "
    "questions that will help us better connect with and support our alumni "
    "community.\n\n"
    "Thank you for being an important part of the BYU Finance family. We look "
    "forward to staying connected with you in the years ahead!\n\n"
    "Warmest regards,\nTanya Harmon & Amy Densley\nBYU Finance Career Directors"
)

_BTN_STYLE = (
    "display:inline-block;background:#1e2a4a;color:#ffffff;text-decoration:none;"
    "font-size:15px;font-weight:600;padding:11px 20px;border-radius:8px;"
)


# ------------------------------------------------------------------ tokens ---


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _token_secret() -> str:
    secret = get_settings().survey_token_secret
    if not secret:
        raise ServiceError("Survey token secret is not configured (SURVEY_TOKEN_SECRET).")
    return secret


# How long a minted survey link stays usable (#360).
#
# SEVEN DAYS, because that is exactly the reminder cadence: stage 0 goes out on
# day 0, stage 1 on day 7, stage 2 on day 14 (``_STAGE_WINDOW_DAYS``). A token is
# minted at the moment its email is built (``_build_survey_email`` ->
# ``_survey_link``), i.e. at send time, so "7 days from issue" means each link
# stays valid right up to the moment the next reminder issues a fresh one that
# supersedes it. That alignment IS the design (Jake, 2026-08-03) — do not raise
# this number without moving the cadence with it.
#
# Before this, the token was a bare HMAC over ``alumni_id.graduation_year`` with
# no time component at all: a forwarded email, a mailing-list archive or a shared
# browser history handed the holder a PERMANENT read of that alum's employment,
# both emails, phone, spouse, birth date and citizenship — plus the matching
# write — and the only revocation was rotating SURVEY_TOKEN_SECRET, which kills
# every outstanding link at once.
SURVEY_TOKEN_TTL_SECONDS = 7 * 24 * 60 * 60

# Tokens minted BEFORE this change carry no issued-at (payload is two fields, not
# three). They cannot be dated, so they are given one fixed, shared deadline
# rather than an unbounded life: the same 7 days, measured from the day the fix
# ships. After it passes, every legacy link is dead for good.
#
# A hardcoded instant is deliberate. Anchoring the grace to process start would
# be worse than useless on a serverless deploy — every cold start would restart
# the 7 days, so legacy tokens would live forever. If the deploy slips past this
# date the grace is simply already over, which is the safe direction to fail.
_LEGACY_TOKEN_VALID_UNTIL = datetime.datetime(2026, 8, 11, tzinfo=datetime.UTC)


# The ONE message every token failure gets — malformed, forged, or simply past
# its 7 days. Deliberately does NOT say which: telling a prober "expired" rather
# than "invalid" confirms the token was once a real alum's credential.
#
# It still has to be useful to the person it is actually shown to, so it names
# the lifetime, says a replacement is coming, and gives a way out that does not
# depend on waiting. Lives here, next to the rule it describes, and is used by
# every public entry point (`api/routes/survey.py`, `survey_responses.py`) so the
# three routes can never drift into telling an alum three different stories.
LINK_DEAD_MESSAGE = (
    "This survey link is no longer usable. Survey links expire seven days after "
    "they are sent. If our survey is still running you will receive a fresh link "
    "in your next reminder email — please use the most recent email we sent you. "
    "If you would rather not wait, contact the BYU Finance department and we will "
    "send you a new one."
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def make_survey_token(
    alumni_id: int,
    graduation_year: int,
    *,
    issued_at: datetime.datetime | None = None,
) -> str:
    """A stateless, tamper-evident, EXPIRING token: `<b64(payload)>.<b64(hmac)>`.

    The payload is ``alumni_id.graduation_year.issued_at`` (issued_at = whole
    UTC seconds). The expiry rides INSIDE the signed payload rather than beside
    it, because there is no token row anywhere — the link is the whole credential
    — so anything not covered by the HMAC would be editable by the recipient.
    Signing the issue time makes "when was this minted?" as unforgeable as "whose
    record is this?".

    ``issued_at`` is injectable for tests only; callers mint at send time.
    """
    issued = int((issued_at or _now()).timestamp())
    payload = f"{alumni_id}.{graduation_year}.{issued}".encode()
    sig = hmac.new(_token_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify_survey_token(token: str, *, now: datetime.datetime | None = None) -> int | None:
    """Return the alumni_id if the token is valid, untampered AND unexpired.

    ``None`` covers every failure — malformed, wrong signature, expired — and the
    callers turn all of them into the SAME 404 message. That is deliberate:
    distinguishing "expired" from "never existed" would confirm to a prober that
    a given token was once a real alum's credential.

    Editing the issued-at is not a way in: it is inside the signed payload, so a
    changed timestamp fails ``compare_digest`` and looks identical to garbage.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except Exception:  # noqa: BLE001 - any decode failure means an invalid token
        return None
    expected = hmac.new(_token_secret().encode("utf-8"), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        parts = payload.decode("utf-8").split(".")
    except UnicodeDecodeError:
        return None

    moment = now or _now()
    if len(parts) == 2:
        # Legacy (pre-#360) token: signed, but undatable. Honour it only until the
        # shared cutoff, so a link already sitting in an alum's inbox still works
        # for its final week instead of dying the second this deploys.
        if moment >= _LEGACY_TOKEN_VALID_UNTIL:
            log.info("Survey token rejected: legacy token past the cutover grace")
            return None
    elif len(parts) == 3:
        try:
            issued = datetime.datetime.fromtimestamp(int(parts[2]), datetime.UTC)
        except (ValueError, OverflowError, OSError):
            return None
        age = (moment - issued).total_seconds()
        if age > SURVEY_TOKEN_TTL_SECONDS:
            # Age only — never the token, the alumni_id or the email address.
            log.info("Survey token rejected: expired (age %.1f days)", age / 86400)
            return None
    else:
        return None

    try:
        return int(parts[0])
    except ValueError:
        return None


# ---------------------------------------------------------------- template ---


@dataclass(frozen=True)
class Recipient:
    alumni_id: int
    first_name: str
    # The ONE address this person is emailed at. Resolved once, by
    # `email_reach.resolve_email` — personal preferred, work as fallback (#392).
    # Singular by construction: there is nowhere to put a second address, so no
    # caller can accidentally send to both.
    email: str
    # The full "here's what we have on file" list (label, value) shown in the
    # email — the Career Directors' field list, empties as "—".
    on_file: tuple[tuple[str, str], ...]
    # Which column `email` came from ("personal" / "work") — surfaced in the send
    # preview and the audit trail so staff can see when a campaign leaned on work
    # addresses, which is a data-quality signal worth acting on.
    email_source: str = email_reach.SOURCE_PERSONAL
    # How many times an engineer has reset this alumnus (#395). Carried on the
    # recipient because it is part of the send-log unique key, so the claim needs
    # it per person; 0 for everyone who has never been reset.
    reset_seq: int = 0


def _on_file_rows(r: Recipient) -> list[tuple[str, str]]:
    return list(r.on_file)


def _dash(value: object) -> str:
    """Display value, or an em dash when we have nothing on file."""
    text = "" if value is None else str(value).strip()
    return text or "—"


def _build_on_file(alum, contact, job) -> tuple[tuple[str, str], ...]:
    """The Career Directors' full field list, in order, for the email preview."""
    spouse = " ".join(
        p for p in (alum.spouse_first_name, alum.spouse_last_name) if p
    ).strip()
    g = lambda o, n: getattr(o, n, None)  # noqa: E731
    return (
        ("Current employment status", _dash(alum.employment_status)),
        ("Company", _dash(g(job, "current_employer"))),
        ("Title", _dash(g(job, "current_title"))),
        ("Industry", _dash(g(job, "current_industry"))),
        ("Secondary industry", _dash(g(job, "current_industry_secondary"))),
        ("Employment city", _dash(g(job, "current_city"))),
        ("Employment state", _dash(g(job, "current_state"))),
        ("Employment country", _dash(g(job, "current_country"))),
        ("Residence city", _dash(g(contact, "city"))),
        ("Residence state", _dash(g(contact, "state"))),
        ("Residence country", _dash(g(contact, "country"))),
        ("Spouse name", _dash(spouse)),
        # "Personal email" everywhere (#392) — see survey_responses._FIELDS.
        ("Personal email", _dash(g(contact, "personal_email"))),
        ("Work email", _dash(g(contact, "work_email"))),
        ("LinkedIn profile", _dash(alum.linkedin_url)),
        ("Graduate school program", _dash(alum.graduate_degree)),
        ("Graduate school name", _dash(alum.graduate_school)),
        ("Projected graduation year", _dash(alum.graduate_graduation_year)),
        ("Finance designations", _dash(alum.other_designations)),
    )


def render_survey_email(r: Recipient, link: str) -> tuple[str, str, str]:
    """Return (subject, html, text) for one recipient."""
    rows = _on_file_rows(r)

    # Plain-text part.
    text_lines = [f"Hello {r.first_name},", "", _INTRO, ""]
    if rows:
        text_lines.append("Here's what we have on file:")
        text_lines += [f"  {label}: {value}" for label, value in rows]
        text_lines.append("")
    text_lines += [f"Confirm or update your info: {link}", "", _CLOSING]
    text = "\n".join(text_lines)

    # HTML part (inline styles for email clients).
    info_html = ""
    if rows:
        cells = "".join(
            f'<tr><td style="padding:4px 16px 4px 0;color:#6b7280;font-size:14px;">'
            f"{escape(label)}</td>"
            f'<td style="padding:4px 0;color:#111827;font-size:14px;font-weight:600;">'
            f"{escape(value)}</td></tr>"
            for label, value in rows
        )
        info_html = (
            '<div style="margin:16px 0;padding:16px;border:1px solid #e5e7eb;'
            'border-radius:8px;background:#f9fafb;">'
            '<div style="font-size:13px;font-weight:600;color:#111827;'
            'margin-bottom:8px;">Here\'s what we have on file</div>'
            f'<table style="border-collapse:collapse;">{cells}</table></div>'
        )

    intro_html = escape(_INTRO).replace("\n\n", "</p><p style=\"margin:0 0 12px;\">")
    closing_html = escape(_CLOSING).replace("\n", "<br>")
    html = f"""\
<div style="margin:0;padding:0;background:#f3f4f6;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;">
    <div style="background:#1e2a4a;padding:16px 24px;">
      <span style="color:#ffffff;font-size:16px;font-weight:600;">BYU Finance Alumni Update</span>
    </div>
    <div style="padding:24px;font-family:Arial,sans-serif;color:#374151;line-height:1.55;">
      <p style="margin:0 0 12px;font-size:15px;">Hello {escape(r.first_name)},</p>
      <p style="margin:0 0 12px;font-size:15px;">{intro_html}</p>
      {info_html}
      <div style="margin:20px 0;">
        <a href="{escape(link)}" style="{_BTN_STYLE}">Confirm or update my info</a>
      </div>
      <p style="margin:0;font-size:14px;color:#4b5563;">{closing_html}</p>
    </div>
    <div style="padding:16px 24px;border-top:1px solid #e5e7eb;font-family:Arial,sans-serif;
         font-size:12px;color:#9ca3af;">BYU Marriott School of Business</div>
  </div>
</div>"""
    return _SUBJECT, html, text


# ----------------------------------------------------- respondent (public) ---


def _held(value: str | None) -> str | None:
    """'Yes' when a designation column says the alum HOLDS it, else None (so `put`
    drops the key and the survey renders an unticked box).

    Uses the shared :func:`holds_designation` predicate, so an alum whose column
    was imported as "No" (or any other negative) is NOT pre-ticked — presence
    alone is not the question."""
    return "Yes" if holds_designation(value) else None


async def get_respondent(
    session: AsyncSession, token: str
) -> SurveyRespondInfo | None:
    """Resolve a survey token to the alum's current on-file info for the public
    confirm page. Returns None if the token is invalid/tampered or the alum is
    gone/archived. The token is the credential (no login), so only the fields the
    survey confirms are returned — keyed by the frontend's SURVEY_FIELDS keys."""
    alumni_id = verify_survey_token(token)
    if alumni_id is None:
        return None
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None or alum.archived:
        return None

    contact = (
        await session.execute(
            select(AlumniContactInfo).where(AlumniContactInfo.alumni_id == alumni_id)
        )
    ).scalar_one_or_none()
    job = (
        await session.execute(
            select(CurrentEmployment).where(CurrentEmployment.alumni_id == alumni_id)
        )
    ).scalar_one_or_none()
    # Only for the CFA/CFP tickboxes (#529) — without it the confirm page would
    # show an alum who already holds the CFA an empty box, and they'd have to
    # re-tick something we already know.
    eng = (
        await session.execute(
            select(AlumniProgramEngagement).where(
                AlumniProgramEngagement.alumni_id == alumni_id
            )
        )
    ).scalar_one_or_none()

    fields: dict[str, str] = {}

    def put(key: str, value: object) -> None:
        if value is not None and str(value).strip() != "":
            fields[key] = str(value)

    # Employment (current_employment)
    put("employment.current_employer", getattr(job, "current_employer", None))
    put("employment.current_title", getattr(job, "current_title", None))
    put("employment.current_industry", getattr(job, "current_industry", None))
    put(
        "employment.current_industry_secondary",
        getattr(job, "current_industry_secondary", None),
    )
    put("employment.current_city", getattr(job, "current_city", None))
    put("employment.current_state", getattr(job, "current_state", None))
    put("employment.current_country", getattr(job, "current_country", None))
    put("employment.current_zip", getattr(job, "current_zip", None))
    put("employment.seniority_level", getattr(job, "seniority_level", None))
    # Contact (alumni_contact_info)
    put("contact.personal_email", getattr(contact, "personal_email", None))
    put("contact.work_email", getattr(contact, "work_email", None))
    put("contact.phone", getattr(contact, "phone", None))
    put("contact.city", getattr(contact, "city", None))
    put("contact.state", getattr(contact, "state", None))
    put("contact.country", getattr(contact, "country", None))
    # Profile (alumni)
    put("profile.employment_status", alum.employment_status)
    put("profile.linkedin_url", alum.linkedin_url)
    put("profile.graduate_degree", alum.graduate_degree)
    put("profile.graduate_school", alum.graduate_school)
    put("profile.graduate_graduation_year", alum.graduate_graduation_year)
    put("profile.spouse_first_name", alum.spouse_first_name)
    put("profile.spouse_last_name", alum.spouse_last_name)
    put("profile.other_designations", alum.other_designations)
    # Designations (alumni_program_engagement). The columns hold a marker string
    # ('CFA'/'CFP') when held and NULL when not, but the survey asks a
    # held/not-held question — so send the tickbox's own vocabulary and omit the
    # key entirely when it isn't held, which renders as an unticked box. "Not
    # held" is decided by `holds_designation`, not by presence: an imported "No"
    # is a stored value but must NOT pre-tick the box.
    put("program.cfa_designation", _held(getattr(eng, "cfa_designation", None)))
    put("program.cfp_designation", _held(getattr(eng, "cfp_designation", None)))
    put("program.cpa_designation", _held(getattr(eng, "cpa_designation", None)))
    # Name block (#646). Pre-filling these is not a convenience — it is what makes
    # them safe to collect: the survey's name fields refuse to write a blank
    # (`survey_responses._Field.blankable`), so a box that arrived empty is a box
    # the alum cleared, not a name we never had. Omitting them here would render
    # four empty boxes over an alum who has a perfectly good name on file.
    put("profile.first_name", alum.first_name)
    put("profile.middle_name", alum.middle_name)
    put("profile.last_name", alum.last_name)
    put("profile.preferred_first_name", alum.preferred_first_name)
    put("profile.gender", alum.gender)
    # Sent VERBATIM, including a value that is not one of the four options (#647):
    # a stored "Separated" has to be visible to the alum, and the frontend re-adds
    # whatever is on file to the dropdown the same way the staff employment-status
    # dropdown does. The constraint is on the write, not the read.
    put("profile.marital_status", alum.marital_status)
    # A date column — emit as an ISO "YYYY-MM-DD" string for the survey date input.
    put("profile.birth_date", alum.birth_date)
    put("profile.citizenship", alum.citizenship)
    put("profile.home_country", alum.home_country)

    first = (alum.preferred_first_name or alum.first_name or "there").strip()
    full = " ".join(p for p in (alum.first_name, alum.last_name) if p).strip() or first
    return SurveyRespondInfo(first_name=first, full_name=full, fields=fields)


# --------------------------------------------------------- graduation years --


async def list_graduation_years(session: AsyncSession) -> list[GraduationYearCount]:
    """Every graduation year present among eligible alumni (is_alumni, not
    archived), with a count — newest first. Drives the console's year picker so
    it reflects the real DB (including the 1900 test cohort)."""
    stmt = (
        select(Alumni.graduation_year, func.count().label("n"))
        .where(
            Alumni.is_alumni.is_(True),
            Alumni.archived.is_(False),
            Alumni.graduation_year.is_not(None),
        )
        .group_by(Alumni.graduation_year)
        .order_by(Alumni.graduation_year.desc())
    )
    rows = (await session.execute(stmt)).all()

    # How many DISTINCT alumni have replied WITHIN THE LAST YEAR for each grad
    # year. Uses the SAME status filter as the re-survey exclusion in
    # _load_recipients (:data:`RESPONDED_STATUSES`), so "responded" really does
    # mean "already surveyed this cycle". A REJECTED response is neither — staff
    # threw it away, so the alum is still surveyable and must not be reported to
    # the console as complete.
    responded_stmt = (
        select(
            SurveyResponse.graduation_year,
            func.count(func.distinct(SurveyResponse.alumni_id)).label("responded"),
        )
        .where(
            SurveyResponse.graduation_year.is_not(None),
            SurveyResponse.submitted_at >= _resurvey_cutoff(),
            SurveyResponse.status.in_(RESPONDED_STATUSES),
            # A reset alum is surveyable again, so they are not "responded" for
            # this campaign — the same rule `_replied_recently_exists` applies to
            # the send. The reply itself is still in the table and still on their
            # profile; it just stopped counting toward this year's tally (#395).
            response_not_superseded(),
        )
        .group_by(SurveyResponse.graduation_year)
    )
    responded_by_year = {
        year: responded for year, responded in (await session.execute(responded_stmt)).all()
    }

    # How many of each year the campaign cannot reach at all (#392) — resolved
    # for every year in ONE grouped query, like `responded_by_year`, so the
    # picker stays a fixed number of round trips.
    unreachable_by_year = await unreachable_counts_by_year(session)

    return [
        GraduationYearCount(
            graduation_year=year,
            total_alumni=count,
            responded=responded_by_year.get(year, 0),
            unreachable=unreachable_by_year.get(year, 0),
        )
        for year, count in rows
    ]


# ------------------------------------------------------------- send service --


# The deliverability gate and the personal→work recipient rule now live in
# `app.core.email_reach`, shared with the dashboard's missing-email KPI so the
# survey and the KPI can no longer answer "has an email" differently (#392).
# Re-exported under the original private names because tests and call sites here
# reference them.
_UNSENDABLE_DOMAINS = email_reach.UNSENDABLE_DOMAINS
_is_sendable_email = email_reach.is_sendable_email


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def eligible_alumni_query(graduation_year: int):
    """The SINGLE definition of "who may be emailed the survey for this year".

    Everything is a SQL-level predicate (correlated EXISTS / NOT EXISTS) so it
    runs in Postgres over the whole 8,000+ row table — nothing is filtered in
    Python. Exposed (not private) so a future "who would receive this?" preview
    can render the identical population rather than re-deriving it.

    On top of ``build_alumni_query``'s defaults (is_alumni, not archived):

    * **Not deceased** — ``alumni.deceased`` is the flag; the column is NOT NULL
      so ``deceased=False`` is total.
    * **Not suppressed** — no ``Deceased`` / ``Do Not Contact`` status label
      (:data:`SUPPRESSED_CONTACT_STATUS_LABELS`). The deceased flag and the
      Deceased LABEL are separate columns that are set independently, so both
      must be checked: on dev, alumni carry both and only the ``@example.com``
      placeholder filter was accidentally stopping them. On prod, with real
      addresses, a "confirm your information" email listing a dead person's full
      record goes to a live inbox — in practice, their spouse's.
      ``Lost Contact`` / ``Retired`` / ``Inactive`` stay ELIGIBLE by design
      (Jake, 2026-08-03): "lost contact" means we want to reconnect, and this
      survey is the tool for it.
    * **Is reachable by email** — a usable PERSONAL email, or failing that a
      usable WORK email (#392). Personal is preferred but no longer required:
      requiring it silently excluded every alumnus who had only a work address
      from every survey ever sent — no send, no error, no trace. "Usable" is the
      full deliverability gate (:func:`email_reach.sendable_email_sql`), not
      ``IS NOT NULL``: a blank string or an ``@example.com`` placeholder is not
      an address, and counting it as one is how the console came to promise more
      recipients than the sender would actually take. Which of the two addresses
      is used is :func:`email_reach.resolve_email`'s decision, made once, in
      :func:`_load_recipients`.
    * **Has not replied this cycle** — a `pending` or `applied` response within
      365 days. A `rejected` one does NOT count (see
      :data:`RESPONDED_STATUSES`).

    Ordered by ``alumni_id`` so that ANY truncation (a ``limit``, or the daily
    send budget) takes a stable, reproducible prefix. ``build_alumni_query`` has
    no ORDER BY of its own, so without this an interrupted send resumed on an
    arbitrary subset and "run Send again to continue" was not a true statement.
    """
    return (
        _survey_cohort_query(graduation_year)
        .where(_has_reachable_email())
        .order_by(Alumni.alumni_id)
    )


def _suppressed_label_exists():
    """Correlated EXISTS: the alumnus carries a Deceased / Do Not Contact label.

    The exact predicate ``build_alumni_query`` negates to exclude them, reused
    (un-negated) to COUNT them — so the number the console reports as suppressed
    is by construction the number the send excluded."""
    return has_status_label_exists(SUPPRESSED_CONTACT_STATUS_LABELS)


def _suppressed_from_send():
    """The WHOLE suppression test: the deceased FLAG or a suppressing LABEL.

    Two separate columns, set independently — an alum can carry the Deceased
    label with the flag unset, or the reverse — so "is this person suppressed?"
    is only ever this disjunction. It gets one definition because the NEGATION of
    it is what scopes every other bucket (``already_responded`` and
    ``unreachable`` both mean "…and not suppressed"), and a bucket that spelled
    that negation out for itself could quietly disagree with the count beside it.

    ``alumni.deceased`` is NOT NULL and an EXISTS never yields NULL, so ``~`` of
    this is total: everyone falls on exactly one side of it.
    """
    return or_(Alumni.deceased.is_(True), _suppressed_label_exists())


def _recent_reply_criteria():
    """The WHERE of "this alumnus has replied this cycle", as criteria.

    Factored out so the two questions asked of that population — *is there one?*
    (:func:`_replied_recently_exists`, the send exclusion and its count) and
    *when was it?* (:func:`_last_reply_at`, the held-out list's reply date) —
    are answered over literally the same rows. Written out twice, the list could
    show a date for a reply that was not the one holding the alum back, or show
    none at all for someone the count says is blocked.
    """
    return (
        SurveyResponse.alumni_id == Alumni.alumni_id,
        SurveyResponse.submitted_at >= _resurvey_cutoff(),
        SurveyResponse.status.in_(RESPONDED_STATUSES),
        response_not_superseded(),
    )


def _replied_recently_exists():
    """Correlated EXISTS: replied within the 365-day re-survey window. A
    ``rejected`` response does not count (:data:`RESPONDED_STATUSES`) — staff
    threw it away, so the alum is surveyable again.

    Nor does a reply an engineer reset has since superseded (#395): the row
    stays in the database and on the alum's profile, but it no longer silences
    them. That is the whole of what a reset now does to this table — it writes
    nothing to it."""
    return (
        select(SurveyResponse.survey_response_id)
        .where(*_recent_reply_criteria())
        .exists()
    )


def _last_reply_at():
    """Correlated scalar: WHEN this alumnus last replied in a way that holds them
    out of a send — the same rows :func:`_replied_recently_exists` tests for,
    aggregated instead of tested.

    NULL for anyone that predicate is false for, which is what lets the held-out
    list carry one date column across all three buckets: a suppressed or
    unreachable alum simply has no qualifying reply, so the column is empty for
    them without a second query or a Python branch.

    ``max``, not ``min``: someone can have answered more than once inside the
    window (a pending submission, then another after a correction), and the
    question the engineer is deciding is how recently they were last asked
    something they answered — the newest reply is the one that makes re-asking
    look unreasonable."""
    return (
        select(func.max(SurveyResponse.submitted_at))
        .where(*_recent_reply_criteria())
        .scalar_subquery()
    )


def _has_reachable_email():
    """Correlated EXISTS: this alumnus has a personal OR work address we could
    actually send to. The SQL twin of :func:`email_reach.resolve_email` returning
    something — so the count and the send agree by construction."""
    return (
        select(AlumniContactInfo.contact_info_id)
        .where(
            AlumniContactInfo.alumni_id == Alumni.alumni_id,
            email_reach.reachable_email_sql(
                AlumniContactInfo.personal_email, AlumniContactInfo.work_email
            ),
        )
        .exists()
    )


# ------------------------------------------------------- who was held out ----
#
# The console reports the send's exclusions as COUNTS
# (:class:`SurveyRecipientBreakdown`), and a count is not something anyone can
# act on. Jake deleted a campaign, re-sent to the cohort, and was told "1 already
# replied within the last year" — which was CORRECT (deleting a campaign retires
# its cycle so the alumni it emailed are sendable again, but it deliberately does
# not clear the 365-day annual window for the ones who actually answered). Right
# and useless in the same breath: there was no way to learn who the 1 was, so the
# cohort was searched by hand until she turned up and the per-alumnus reset could
# be run on her (2026-08-06, #658).
#
# So: the same three exclusions, by name. The buckets below are the ONLY
# definition of them — :func:`recipient_breakdown` counts these very expressions
# and :func:`held_out_alumni_query` lists the rows they match, so a person can
# appear in the list if and only if they were counted, and the drill-down cannot
# quietly report a different population than the number it drills into. Deriving
# "who replied recently?" a second way is the standing bug in this area: two
# figures disagree and nobody can tell which one is lying.

HELD_OUT_SUPPRESSED = "suppressed"
HELD_OUT_ALREADY_RESPONDED = "already_responded"
HELD_OUT_UNREACHABLE = "unreachable"

HELD_OUT_REASON_LABELS: dict[str, str] = {
    HELD_OUT_SUPPRESSED: "Deceased or Do Not Contact",
    HELD_OUT_ALREADY_RESPONDED: "Already replied within the last year",
    HELD_OUT_UNREACHABLE: "No usable email address",
}


def _held_out_buckets() -> dict:
    """The three reasons a send skips someone, as mutually exclusive predicates.

    Together with "eligible" they PARTITION the year's alumni, which is the
    property :class:`SurveyRecipientBreakdown` states and its tests pin::

        cohort_total = suppressed + already_responded + unreachable + eligible

    Exclusivity is what makes that arithmetic true, and it is bought by scoping
    each bucket with the negation of the ones before it — suppression first
    (a Do Not Contact alum who also has no address is suppressed, not a gap to
    chase), then the annual window, then reachability. That order is not
    cosmetic: it is why a Do Not Contact name can never reach a worklist.

    ``unreachable`` is the same set as :func:`unreachable_alumni_query` — same
    cohort, same suppression, same annual window, same inverted email predicate,
    the suppression written here as one negated disjunction rather than
    ``build_alumni_query``'s two arguments. They are pinned equal by test rather
    than by sharing a call, because that query returns whole ORM entities and
    this one returns columns plus a computed reason.

    Fresh expressions each call: SQLAlchemy elements are immutable, but a bucket
    is used in several statements and building them per call keeps the
    ``_resurvey_cutoff()`` inside them evaluated at query time rather than frozen
    at import.
    """
    suppressed = _suppressed_from_send()
    return {
        # Deceased / Do Not Contact — never emailed, by decision.
        HELD_OUT_SUPPRESSED: suppressed,
        # Answered inside the 365-day window. THE bucket #658 is about: it is the
        # one a retired campaign does not clear, and the only way out of it is
        # `POST /survey/alumni/{id}/reset`.
        HELD_OUT_ALREADY_RESPONDED: and_(~suppressed, _replied_recently_exists()),
        # Wanted, due, and we have no usable address for them (#392).
        HELD_OUT_UNREACHABLE: and_(
            ~suppressed, ~_replied_recently_exists(), ~_has_reachable_email()
        ),
    }


def held_out_alumni_query(graduation_year: int, *, reason: str | None = None):
    """WHO this year's send is holding out, with the reason, as ONE query (#658).

    Columns, not entities: the reason is computed in SQL and the reply date comes
    from a correlated aggregate, so the whole answer arrives in a single pass over
    the year's alumni. Loading the cohort and bucketing it in Python would be the
    obvious alternative and is exactly what this codebase cannot afford — 8,000+
    rows, and a second implementation of the eligibility rules to drift.

    ``reason`` narrows to one bucket; ``None`` returns all three, which is the
    same rows the breakdown's three counts add up to.

    Ordered by name (then id, to break ties) so it reads as a worklist and pages
    deterministically — an unstable sort under LIMIT/OFFSET silently repeats and
    skips people, and this list exists to be worked through. NOT ordered by
    reason: the UI asks for one bucket at a time, and a mixed page grouped by
    reason would make "page 2" mean something different depending on the mix.
    """
    buckets = _held_out_buckets()
    # The buckets are mutually exclusive and the WHERE below admits nothing
    # outside their union, so falling past the first two branches IS the third —
    # this is the same layering `_held_out_buckets` scopes them with, written
    # once more as a projection.
    reason_col = case(
        (buckets[HELD_OUT_SUPPRESSED], literal(HELD_OUT_SUPPRESSED)),
        (buckets[HELD_OUT_ALREADY_RESPONDED], literal(HELD_OUT_ALREADY_RESPONDED)),
        else_=literal(HELD_OUT_UNREACHABLE),
    ).label("reason")
    wanted = buckets[reason] if reason is not None else or_(*buckets.values())
    return (
        select(
            Alumni.alumni_id,
            Alumni.first_name,
            Alumni.preferred_first_name,
            Alumni.last_name,
            reason_col,
            _last_reply_at().label("last_reply_at"),
        )
        .where(
            # The same cohort the breakdown's `cohort_total` counts, so the
            # buckets stay a partition OF something.
            Alumni.is_alumni.is_(True),
            Alumni.archived.is_(False),
            Alumni.graduation_year == graduation_year,
            wanted,
        )
        .order_by(Alumni.last_name, Alumni.first_name, Alumni.alumni_id)
    )


# A cohort is a graduation year, so a few hundred people at most today — but the
# `already_responded` bucket grows for the life of a campaign and an unbounded
# list is a promise this endpoint would eventually break. The default page is
# generous enough that the console's normal case (one bucket of one year) arrives
# whole, and `total` always describes the FULL set, so a UI that never pages
# still shows an honest number next to a partial list.
HELD_OUT_PAGE_DEFAULT = 200
HELD_OUT_PAGE_MAX = 1000


async def list_held_out(
    session: AsyncSession,
    graduation_year: int,
    *,
    reason: str | None = None,
    limit: int = HELD_OUT_PAGE_DEFAULT,
    offset: int = 0,
) -> SurveyHeldOutPage:
    """The held-out count made actionable — names, reasons, and reply dates (#658).

    Two statements, both aggregate/SQL-level: one COUNT over the filtered set (so
    `total` is the whole set regardless of paging, and is comparable to the
    matching :class:`SurveyRecipientBreakdown` field) and one paged SELECT. The
    count reuses :func:`held_out_alumni_query`'s own WHERE — it is that query with
    its columns swapped for a count — so the total can never describe a different
    population than the rows.

    Read-only. Nothing here changes who is sendable; the only control that does
    is the per-alumnus reset, and this exists so the engineer can decide whether
    to run it on a real person rather than on a number.
    """
    filtered = held_out_alumni_query(graduation_year, reason=reason)
    total = int(
        await session.scalar(
            select(func.count()).select_from(
                # Columns dropped before counting: the reason CASE and the
                # correlated reply-date aggregate are per-row work a COUNT has no
                # use for. The WHERE — the part that decides WHO is counted — is
                # the query's own, untouched.
                filtered.order_by(None)
                .with_only_columns(Alumni.alumni_id)
                .subquery()
            )
        )
        or 0
    )
    rows = (await session.execute(filtered.limit(limit).offset(offset))).all()
    items = [
        SurveyHeldOutAlum(
            alumni_id=row.alumni_id,
            # Preferred name first, matching `list_unreachable` and the rest of
            # the console — staff look these people up by what they're called.
            name=" ".join(
                p
                for p in (row.preferred_first_name or row.first_name, row.last_name)
                if p
            ).strip()
            or f"Alum #{row.alumni_id}",
            reason=row.reason,
            reason_label=HELD_OUT_REASON_LABELS[row.reason],
            last_reply_at=row.last_reply_at,
        )
        for row in rows
    ]
    return SurveyHeldOutPage(
        graduation_year=graduation_year,
        reason=reason,
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


def _survey_cohort_query(graduation_year: int):
    """The year's cohort MINUS suppression and recent responders — everything the
    eligible and unreachable sets have in common.

    Factored out so those two sets cannot drift: they are the same query with
    opposite email predicates, which is what makes
    ``eligible + unreachable == surveyable cohort`` an identity rather than a
    hope. Suppressed alumni (deceased / Do Not Contact) are excluded from BOTH —
    someone we deliberately never email is not an "unreachable" problem for staff
    to chase, and merging the two would put Do Not Contact names on a call sheet.
    """
    # Already replied within the last year -> skip (the survey is annual).
    return build_alumni_query(
        graduation_year=graduation_year,
        deceased=False,
        suppress_labels=SUPPRESSED_CONTACT_STATUS_LABELS,
    ).where(~_replied_recently_exists())


def unreachable_alumni_query(graduation_year: int):
    """The mirror of :func:`eligible_alumni_query`: alumni this campaign WANTS to
    email and cannot, because there is no usable address on either column (#392).

    Same cohort, same suppression, same "hasn't replied this cycle" rule — only
    the email predicate is inverted. That shared base is deliberate: these people
    used to be indistinguishable from everyone else the query dropped, so a
    campaign that reached 180 of 200 alumni looked exactly like one that reached
    all 180 it had. Staff can now be handed the other 20 by name and chase them
    another way.

    NOT the same thing as suppression. Deceased / Do Not Contact alumni are
    filtered out of this list too — they are not a gap to close.
    """
    return (
        _survey_cohort_query(graduation_year)
        .where(~_has_reachable_email())
        .order_by(Alumni.last_name, Alumni.first_name, Alumni.alumni_id)
    )


async def count_unreachable(session: AsyncSession, graduation_year: int) -> int:
    """How many of this year's surveyable alumni have no usable address (#392)."""
    total = await session.scalar(
        select(func.count()).select_from(
            unreachable_alumni_query(graduation_year).order_by(None).subquery()
        )
    )
    return int(total or 0)


async def unreachable_counts_by_year(session: AsyncSession) -> dict[int, int]:
    """:func:`count_unreachable` for EVERY graduation year in one query.

    Drives the console's year picker, which must not fan out into one round trip
    per year the way a per-year call would.
    """
    stmt = (
        select(Alumni.graduation_year, func.count().label("n"))
        .where(
            Alumni.is_alumni.is_(True),
            Alumni.archived.is_(False),
            Alumni.graduation_year.is_not(None),
            Alumni.deceased.is_(False),
            ~_suppressed_label_exists(),
            ~_replied_recently_exists(),
            ~_has_reachable_email(),
        )
        .group_by(Alumni.graduation_year)
    )
    return {year: int(n) for year, n in (await session.execute(stmt)).all()}


async def list_unreachable(
    session: AsyncSession, graduation_year: int
) -> list[SurveyUnreachableAlum]:
    """WHO this campaign cannot email, by name — the count made actionable (#392).

    The same argument as the non-responder call sheet: a number tells staff there
    is a gap, this tells them whose. Each row carries the reason
    (:data:`email_reach.REASON_LABELS`) and whatever is in the two email columns,
    because "there is a typo in the work email" and "we have never had an address
    for this person" are different jobs — the first is fixable on the spot from
    this very list.

    Ordered by name so it reads like a worklist and is stable between refreshes.
    Contains NO suppressed alumni: see :func:`unreachable_alumni_query`.
    """
    alumni = (
        (await session.execute(unreachable_alumni_query(graduation_year)))
        .scalars()
        .all()
    )
    if not alumni:
        return []
    ids = [a.alumni_id for a in alumni]
    contacts = {
        c.alumni_id: c
        for c in (
            await session.execute(
                select(AlumniContactInfo).where(AlumniContactInfo.alumni_id.in_(ids))
            )
        )
        .scalars()
        .all()
    }
    items: list[SurveyUnreachableAlum] = []
    for a in alumni:
        contact = contacts.get(a.alumni_id)
        personal = getattr(contact, "personal_email", None)
        work = getattr(contact, "work_email", None)
        name = " ".join(
            p for p in (a.preferred_first_name or a.first_name, a.last_name) if p
        ).strip()
        reason = email_reach.unreachable_reason(personal, work)
        items.append(
            SurveyUnreachableAlum(
                alumni_id=a.alumni_id,
                name=name or f"Alum #{a.alumni_id}",
                reason=reason,
                reason_label=email_reach.REASON_LABELS[reason],
                personal_email=personal,
                work_email=work,
            )
        )
    return items


async def recipient_breakdown(
    session: AsyncSession,
    graduation_year: int,
    *,
    resolved: tuple[list[Recipient], list[Recipient]] | None = None,
) -> SurveyRecipientBreakdown:
    """The ONE account of who this year's survey reaches, and who it does not.

    Every consumer of "how many will this send to" reads this — the console's
    year picker, the send confirmation, and the send RESULT itself. That is the
    whole point: this codebase has a standing bug class where a count is derived
    one way and the send another, and the console then reports numbers that never
    went out. Here the buckets are not independently computed opinions — they are
    the same queries the send uses, so they cannot disagree with it.

    The buckets PARTITION the year's alumni (is_alumni, not archived):

        cohort_total = suppressed + already_responded + unreachable + eligible

    and then ``recipients = eligible - duplicate_emails``, the dedupe being a
    Python step over the loaded rows rather than a SQL one.

    ``suppressed`` and ``unreachable`` are reported as SEPARATE numbers and never
    summed into a single "not emailed" figure. Deceased / Do Not Contact is a
    decision the institution made and wants kept; no usable address is a gap it
    wants closed. A UI that merges them either puts Do Not Contact names on a
    chase list or hides real gaps behind a suppression total.

    ``resolved`` lets a caller that has ALREADY loaded and deduped the cohort
    (a send, which must do so anyway) hand those lists over instead of paying for
    a second full load of an 8,000-row table. It is not a shortcut around the
    shared definition — it is the very same ``_load_recipients`` +
    :func:`dedupe_by_email` output, so the reported figure is literally the
    population that was sent to, not a recomputation that could differ from it.
    """
    cohort_total = int(
        await session.scalar(
            select(func.count())
            .select_from(Alumni)
            .where(
                Alumni.is_alumni.is_(True),
                Alumni.archived.is_(False),
                Alumni.graduation_year == graduation_year,
            )
        )
        or 0
    )
    suppressed = int(
        await session.scalar(
            select(func.count())
            .select_from(Alumni)
            .where(
                Alumni.is_alumni.is_(True),
                Alumni.archived.is_(False),
                Alumni.graduation_year == graduation_year,
                _held_out_buckets()[HELD_OUT_SUPPRESSED],
            )
        )
        or 0
    )
    already_responded = int(
        await session.scalar(
            select(func.count())
            .select_from(Alumni)
            .where(
                Alumni.is_alumni.is_(True),
                Alumni.archived.is_(False),
                Alumni.graduation_year == graduation_year,
                _held_out_buckets()[HELD_OUT_ALREADY_RESPONDED],
            )
        )
        or 0
    )
    unreachable = await count_unreachable(session, graduation_year)

    # Loaded, not counted: the dedupe is a Python step, so the only honest way to
    # report `recipients` is the very same load the send runs — either handed to
    # us by that send, or run here for a standalone preview.
    if resolved is None:
        kept, dropped = dedupe_by_email(
            await _load_recipients(session, graduation_year)
        )
    else:
        kept, dropped = resolved

    return SurveyRecipientBreakdown(
        graduation_year=graduation_year,
        cohort_total=cohort_total,
        suppressed=suppressed,
        already_responded=already_responded,
        unreachable=unreachable,
        eligible=len(kept) + len(dropped),
        duplicate_emails=len(dropped),
        recipients=len(kept),
        work_email_fallback=_work_fallback_count(kept),
    )


def dedupe_by_email(
    recipients: list[Recipient],
) -> tuple[list[Recipient], list[Recipient]]:
    """Split ``recipients`` into (one per address, the collisions dropped).

    Two alumni rows can carry the same personal email — spouses sharing a
    household address, an address reassigned to a new owner, a data-entry slip,
    or a genuine duplicate record. Without this, EACH of them is emailed
    separately, and every message contains ~19 fields of that alum's record
    (both emails, spouse names, residence, employer, title, LinkedIn) plus a
    live signed token that lets the holder EDIT that record. One inbox would
    receive several people's profiles with write access to all of them.

    The keeper is chosen deterministically — the input is ordered by
    ``alumni_id``, so it is the lowest id — which makes a re-run pick the same
    person rather than rotating whose record leaks. The dropped ones are
    returned, not silently discarded, so the caller can surface the collision.
    """
    seen: dict[str, Recipient] = {}
    kept: list[Recipient] = []
    dropped: list[Recipient] = []
    for r in recipients:
        key = (r.email or "").strip().lower()
        if key in seen:
            dropped.append(r)
            continue
        seen[key] = r
        kept.append(r)
    return kept, dropped


async def _load_recipients(session: AsyncSession, graduation_year: int) -> list[Recipient]:
    """Eligible alumni for a grad year (see :func:`eligible_alumni_query`), with
    the on-file fields the email previews. Side tables are bulk-loaded once (no
    N+1), mirroring the CSV export."""
    alumni = (await session.execute(eligible_alumni_query(graduation_year))).scalars().all()
    ids = [a.alumni_id for a in alumni]
    if not ids:
        return []

    contacts = {
        c.alumni_id: c
        for c in (
            await session.execute(
                select(AlumniContactInfo).where(AlumniContactInfo.alumni_id.in_(ids))
            )
        )
        .scalars()
        .all()
    }
    employ = {
        e.alumni_id: e
        for e in (
            await session.execute(
                select(CurrentEmployment).where(CurrentEmployment.alumni_id.in_(ids))
            )
        )
        .scalars()
        .all()
    }
    # One bulk read, like the side tables — the claim needs each person's reset
    # count to write a send-log row that does not collide with the pre-reset one.
    resets = await reset_seq_for(session, ids)

    recipients: list[Recipient] = []
    for a in alumni:
        contact = contacts.get(a.alumni_id)
        # Personal preferred, work as the fallback, exactly once per alumnus
        # (#392). The query above already required one of these to be usable, so
        # this returns an address for everyone it loaded — the guard below is a
        # belt-and-braces against the SQL and Python rules ever drifting apart
        # again, not a filter the population depends on. Anyone it did drop would
        # be a bug, so it is logged rather than skipped silently, which is how
        # the personal-email-only exclusion stayed invisible for so long.
        email, source = email_reach.resolve_email(
            getattr(contact, "personal_email", None),
            getattr(contact, "work_email", None),
        )
        if email is None:
            log.warning(
                "Survey %s: alumni_id=%s passed the eligibility query but has no "
                "usable email — SQL and Python reachability rules disagree",
                graduation_year,
                a.alumni_id,
            )
            continue
        job = employ.get(a.alumni_id)
        recipients.append(
            Recipient(
                alumni_id=a.alumni_id,
                first_name=(a.preferred_first_name or a.first_name or "there"),
                email=email,
                on_file=_build_on_file(a, contact, job),
                email_source=source or email_reach.SOURCE_PERSONAL,
                reset_seq=resets.get(a.alumni_id, 0),
            )
        )
    return recipients


class ResendRateLimited(Exception):
    """Resend returned 429 — we've hit its request/volume limit. ``retry_after``
    is the seconds Resend told us to wait (from its ``retry-after`` /
    ``ratelimit-reset`` header) before sending more. The LIMIT COMES FROM RESEND:
    we never hardcode a daily/monthly cap — we send until Resend says stop.

    A 429 is a DEFINITIVE rejection: Resend refused the call, so not one email in
    that batch was queued. The claim for it is therefore released (see
    :func:`_release_claim`) and those recipients go out on the next run."""

    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"Resend rate limit hit; retry after {retry_after}s")


class ResendDeliveryUnknown(ServiceError):
    """The batch left us but we never learned its fate — a transport error or a
    timeout, which can fire AFTER Resend accepted and queued the emails.

    This is the one failure we cannot undo, and it is why the sender claims
    before it sends: the claim stays, so those recipients are treated as
    possibly-delivered and are never emailed a second time. A non-2xx RESPONSE is
    different — Resend answered, and answered no — so that raises a plain
    :class:`ServiceError` and the claim is released."""


def _int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# Small pause between batches to stay under Resend's request rate limit
# (default ~10 req/s per team); the authoritative pace still comes from the
# ratelimit-* headers below.
_INTER_BATCH_DELAY_SECONDS = 0.15
_MAX_PACE_SLEEP_SECONDS = 5.0


async def _send_batch(emails: list[dict]) -> tuple[int | None, int | None]:
    """Send one ≤100-email batch. Returns ``(ratelimit_remaining, ratelimit_reset)``
    from Resend's response headers so the caller can pace itself. Raises
    :class:`ResendRateLimited` on 429 (honoring ``retry-after``) and
    :class:`ServiceError` on other failures. All pacing/stopping is driven by
    Resend's own headers — nothing here is a configured limit."""
    settings = get_settings()
    key = settings.resend_api_key
    if not key:
        raise ServiceError("Resend API key is not configured (RESEND_API_KEY).")
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            response = await client.post(
                _RESEND_BATCH_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=emails,
            )
    except httpx.HTTPError as exc:
        # AMBIGUOUS: the request may have reached Resend and been queued before
        # the connection died. Distinct exception type so the caller keeps the
        # claim rather than releasing it (see :class:`ResendDeliveryUnknown`).
        raise ResendDeliveryUnknown("Could not reach the email service.") from exc
    if response.status_code == 429:
        retry_after = (
            _int_header(response, "retry-after")
            or _int_header(response, "ratelimit-reset")
            or 1
        )
        log.warning("Resend rate limit (429); retry after %ss", retry_after)
        raise ResendRateLimited(retry_after)
    if not response.is_success:
        # Surface Resend's own reason (e.g. domain-not-verified, free-tier
        # recipient restriction) so the operator can fix it — these messages
        # are actionable and non-sensitive.
        detail = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = str(body.get("message") or body.get("error") or "").strip()
        except Exception:  # noqa: BLE001 - non-JSON error body
            detail = ""
        log.error(
            "Resend batch send failed: status=%s detail=%s",
            response.status_code,
            detail,
        )
        raise ServiceError(
            f"Resend rejected the send (HTTP {response.status_code})"
            + (f": {detail}" if detail else ".")
        )
    return (
        _int_header(response, "ratelimit-remaining"),
        _int_header(response, "ratelimit-reset"),
    )


def _survey_link(base_url: str, alumni_id: int, graduation_year: int) -> str:
    """The recipient's unique, signed confirm link.

    Minted here, at send time, so the token's issued-at IS its sent-at and the
    7-day life (:data:`SURVEY_TOKEN_TTL_SECONDS`) runs out exactly as the next
    reminder issues its replacement."""
    token = make_survey_token(alumni_id, graduation_year)
    return f"{base_url.rstrip('/')}/survey/{token}"


def _build_survey_email(
    r: Recipient, *, graduation_year: int, base_url: str, from_field: str
) -> dict:
    """One Resend batch entry for a recipient (unique link + rendered content)."""
    link = _survey_link(base_url, r.alumni_id, graduation_year)
    subject, html, text = render_survey_email(r, link)
    return {
        "from": from_field,
        "to": [r.email],
        "subject": subject,
        "html": html,
        "text": text,
    }


# ------------------------------------------------ stage + target selection ---


async def select_stage_targets(
    session: AsyncSession,
    *,
    graduation_year: int,
    recipients: list[Recipient],
    max_stage: int,
    cycle_seq: int,
) -> tuple[int | None, list[Recipient]]:
    """Pick the stage this send should cover, and who still needs it.

    Returns the LOWEST stage ``s <= max_stage`` that still has recipients with no
    ``survey_send_log`` row, together with those recipients — or ``(None, [])``
    when every permitted stage has been fully delivered.

    ``max_stage`` is a CEILING derived from the calendar
    (:func:`ceiling_stage_for`), never a target: a stage is sent because someone
    is owed it, not because today is its window.

    Two bugs this shape fixes:

    * The initial used to be the only stage with a "finish it regardless of the
      window" rule. A reminder window that could not drain — cap-throttled, or a
      cron run missed inside its 7 days — abandoned its stragglers permanently,
      because the next run only ever looked at the CURRENT window. Scanning
      upward from 0 means an unfinished stage is always picked up.
    * ``(None, [])`` is the only honest basis for completing a campaign. Deciding
      it from ``elapsed >= 21`` alone completed cohorts that had received zero
      emails.

    Reminders reach only initial-recipients without needing a separate check: we
    only reach the loop when stage 0 has no targets left, which means every
    remaining recipient is already logged for stage 0.

    Every log read here is scoped to ``cycle_seq`` (#357), so "already emailed"
    means "already emailed IN THIS CAMPAIGN". Unscoped, a year's second campaign
    saw last year's rows, found no targets at any stage, and completed having
    emailed nobody.
    """
    logged_initial = await logged_alumni_ids(
        session, graduation_year, STAGE_INITIAL, cycle_seq
    )
    initial_targets = [r for r in recipients if r.alumni_id not in logged_initial]
    if initial_targets:
        return STAGE_INITIAL, initial_targets
    for stage in range(STAGE_REMINDER_1, max_stage + 1):
        already = await logged_alumni_ids(session, graduation_year, stage, cycle_seq)
        targets = [r for r in recipients if r.alumni_id not in already]
        if targets:
            return stage, targets
    return None, []


async def schedule_start_date(
    session: AsyncSession, graduation_year: int
) -> datetime.date | None:
    """The year's campaign start date, or None when it has no campaign at all.

    ONE read answering two questions the manual send has to ask together (#405):
    which stage it may record, and whether it must leave a campaign behind. Two
    separate reads would be one extra round trip and one chance for a campaign
    appearing in between to make the send's two halves disagree about whether
    there was one.
    """
    return (
        await session.execute(
            select(SurveySchedule.start_date).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    ).scalar_one_or_none()


def max_stage_for_start(start_date: datetime.date | None) -> int:
    """The ceiling stage a MANUAL send may record, given the campaign's start.

    A manual send has no stage of its own — there is one email template — but
    ``survey_send_log`` is UNIQUE on (year, alumni, stage), so it must record a
    REAL one. It is resolved exactly the way the cron resolves it: from the
    year's ``survey_schedule`` row if there is one, else stage 0 only. Inventing
    a synthetic ``stage = -1`` would let the unique constraint hold while the
    same alum received both a manual email and a stage-0 cron email — the very
    incident this is fixing.

    ``None`` (no campaign) therefore means stage 0 — which is also why a campaign
    created from such a send is anchored to its stage-0 rows: there is no other
    stage it could have written.
    """
    if start_date is None:
        return STAGE_INITIAL
    elapsed = (datetime.datetime.now(datetime.UTC).date() - start_date).days
    return ceiling_stage_for(elapsed)


async def campaign_max_stage(session: AsyncSession, graduation_year: int) -> int:
    """:func:`max_stage_for_start` for a year, reading the schedule itself."""
    return max_stage_for_start(await schedule_start_date(session, graduation_year))


# --------------------------------------------------------------- send lock ----
#
# ONE send at a time, account-wide (#358).
#
# `_claim_batch` already makes it impossible for two runners to email the same
# alum — the claim is an atomic `ON CONFLICT DO NOTHING ... RETURNING`. What it
# cannot do is stop them OVERSHOOTING THE DAILY BUDGET: each reads
# `survey_schedule._run_allowance` before either has written a claim, so both
# believe the whole day's allowance is theirs and jointly spend twice it. That
# pushes the account past the Resend plan limit, which comes back as the 429s the
# scheduler then has to absorb.
#
# A concurrent run is not hypothetical: both GET and POST `/survey/cron/run` are
# live, Vercel Cron is at-least-once, and the likeliest collision of all is an
# admin pressing "Send now" while the daily cron is mid-run.
#
# Why a Postgres advisory lock and not a module-level flag: the API runs as many
# independent serverless instances, so nothing in Python memory is shared between
# the two runners that need to see each other.
#
# Why a DEDICATED connection holding an open transaction, rather than taking the
# lock on the caller's session:
#
#   * A send commits repeatedly as it goes (every batch is claimed and committed
#     before it is sent). `pg_try_advisory_xact_lock` on the caller's session
#     would therefore be RELEASED by the first batch's commit, roughly a second
#     into a run that can last minutes — the guard would cover the budget read
#     and nothing else.
#   * Session-scoped locks (`pg_advisory_lock`) survive commits but not the
#     pooler: on the transaction pooler (:6543) a connection is only pinned to a
#     backend FOR THE DURATION OF A TRANSACTION, so a lock taken outside one can
#     be left behind on a backend we never see again — an un-releasable lock that
#     would wedge the cron permanently. Under NullPool (what both the serverless
#     and transaction-pooler paths use) the connection is closed at commit
#     anyway.
#
# A transaction-scoped lock held inside one long-lived transaction on a
# connection of its own is correct under both poolers, and it CANNOT leak: if the
# process dies the transaction dies with it and Postgres drops the lock.
#
# The lock is a `try`, never a wait. A run that cannot get it returns cleanly and
# says so; it never blocks and never raises. Skipping is the right outcome —
# sending is irreversible and the whole send path deliberately fails toward
# "possibly missed" rather than "sent twice" (see `_claim_batch`) — and whatever
# this run would have done is still owed, so the next cron does it.

# Arbitrary but STABLE 64-bit key. Anything else taking a Postgres advisory lock
# in this app must not reuse it. (Date + issue number, so its origin is legible.)
_SEND_LOCK_KEY = 20260803358


@asynccontextmanager
async def send_lock() -> AsyncIterator[bool]:
    """Hold the exclusive survey-send lock for the duration of the block.

    Yields True when this caller owns it, False when another send already does.
    Never blocks and never raises on contention — the caller decides what a
    declined send means (the cron reports a skipped run; the manual send 409s).

    Yields True when no database is configured (unit tests, a DB-less boot):
    there is no second runner to race, and no connection to take a lock on.
    """
    engine = database.engine
    if engine is None:
        yield True
        return
    # A connection of our own, so the caller's per-batch commits cannot end the
    # transaction this lock lives in. See the section notes above.
    async with engine.connect() as conn, conn.begin():
        acquired = bool(
            await conn.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"),
                {"key": _SEND_LOCK_KEY},
            )
        )
        yield acquired


# ----------------------------------------------------- claim-then-send core ---


async def _claim_batch(
    session: AsyncSession,
    *,
    graduation_year: int,
    stage: int,
    cycle_seq: int,
    batch: list[Recipient],
) -> list[Recipient]:
    """Reserve ``batch`` in ``survey_send_log`` and COMMIT, before sending.

    ``ON CONFLICT DO NOTHING ... RETURNING alumni_id`` makes the reservation
    atomic: only rows this statement actually inserted come back, so anyone
    already logged (an earlier run, a concurrent one) is simply not ours to send
    to and drops out of the batch.

    Claiming BEFORE the Resend call is deliberate. Emailing is irreversible and
    the log row is not, so the two orderings fail in opposite directions:
    send-then-log loses the record on any failure in between and RE-EMAILS those
    people on the next run (this is what produced the unscheduled Sunday send);
    claim-then-send can at worst skip someone. For an irreversible side effect,
    "possibly missed" is the correct way to fail — a missed alum can be found and
    re-sent deliberately, a duplicate cannot be recalled.

    ``reset_seq`` comes from the recipient (#395) and is what lets a reset alum
    be claimed again for a (year, stage, cycle) they already have a row for,
    WITHOUT that row being deleted. It is read when the cohort is loaded, so a
    reset landing mid-send leaves this insert conflicting with the pre-reset row
    and the recipient simply drops out of this batch — "possibly missed" again,
    and the next run picks them up with the new sequence.
    """
    stmt = (
        pg_insert(SurveySendLog)
        .values(
            [
                {
                    "graduation_year": graduation_year,
                    "alumni_id": r.alumni_id,
                    "stage": stage,
                    "cycle_seq": cycle_seq,
                    "reset_seq": r.reset_seq,
                }
                for r in batch
            ]
        )
        .on_conflict_do_nothing(
            constraint="uq_survey_send_log_year_alumni_stage"
        )
        .returning(SurveySendLog.alumni_id)
    )
    claimed = set((await session.execute(stmt)).scalars().all())
    await session.commit()
    return [r for r in batch if r.alumni_id in claimed]


async def _release_claim(
    session: AsyncSession,
    *,
    graduation_year: int,
    stage: int,
    cycle_seq: int,
    batch: list[Recipient],
) -> None:
    """Undo a claim Resend DEFINITIVELY refused (a 429, or a non-2xx answer).

    Only rows :func:`_claim_batch` itself inserted are ever passed here — an
    ``ON CONFLICT DO NOTHING`` never returns a pre-existing row — so this can
    never delete the record of a genuinely delivered email.

    ``cycle_seq`` is part of that guarantee, not decoration (#357). Without it
    this DELETE matches (year, alumni, stage) across EVERY cycle, so releasing a
    throttled claim in cycle 2 would silently delete the cycle-1 row recording an
    email that really was delivered last year — destroying send history and
    making that alum re-sendable in the earlier cycle's accounting.

    ``reset_seq`` is in the same guarantee for the same reason (#395): after an
    engineer reset the alum has TWO rows for one (year, stage, cycle), and a
    release that did not name the generation would delete the pre-reset one —
    erasing the very history the reset was rewritten to preserve. Recipients are
    grouped by their sequence so each DELETE names exactly the row that was just
    inserted.
    """
    if not batch:
        return
    by_seq: dict[int, list[int]] = {}
    for r in batch:
        by_seq.setdefault(r.reset_seq, []).append(r.alumni_id)
    for reset_seq, alumni_ids in by_seq.items():
        await session.execute(
            delete(SurveySendLog).where(
                SurveySendLog.graduation_year == graduation_year,
                SurveySendLog.stage == stage,
                SurveySendLog.cycle_seq == cycle_seq,
                SurveySendLog.reset_seq == reset_seq,
                SurveySendLog.alumni_id.in_(alumni_ids),
            )
        )
    await session.commit()


async def _send_and_log(
    session: AsyncSession,
    recipients: list[Recipient],
    *,
    graduation_year: int,
    stage: int,
    cycle_seq: int,
    base_url: str,
    from_field: str,
) -> tuple[int, int | None, ServiceError | None]:
    """Claim, send and durably record ``recipients`` in ``_BATCH_MAX`` chunks.

    Returns ``(sent, retry_after, error)``. Every chunk is claimed and committed
    before its Resend call, so what has been delivered is already durable when
    anything goes wrong — a crash, a throttle or a transport failure can no
    longer lose the send log and cause a re-send.

    Stops at the first failure, and unwinds according to what the failure tells
    us: a 429 or a non-2xx answer means Resend queued nothing, so the claim is
    released and those recipients go out next run; a transport failure means we
    do not know, so the claim STAYS and they are treated as delivered.

    This is the only place either sender reaches Resend — ``_send_batch`` is
    private and nothing outside this module can send without logging again.
    """
    sent = 0
    retry_after: int | None = None
    error: ServiceError | None = None
    for chunk in _chunks(recipients, _BATCH_MAX):
        claimed = await _claim_batch(
            session,
            graduation_year=graduation_year,
            stage=stage,
            cycle_seq=cycle_seq,
            batch=chunk,
        )
        if not claimed:
            continue  # someone else already owns this (year, alumni, stage, cycle)
        emails = [
            _build_survey_email(
                r,
                graduation_year=graduation_year,
                base_url=base_url,
                from_field=from_field,
            )
            for r in claimed
        ]
        try:
            remaining, reset = await _send_batch(emails)
        except ResendRateLimited as exc:
            await _release_claim(
                session,
                graduation_year=graduation_year,
                stage=stage,
                cycle_seq=cycle_seq,
                batch=claimed,
            )
            retry_after = exc.retry_after
            break
        except ResendDeliveryUnknown as exc:
            # Ambiguous — keep the claim. See :class:`ResendDeliveryUnknown`.
            log.error(
                "Survey send outcome unknown for %s recipients "
                "(grad_year=%s stage=%s); claim kept, they will NOT be retried",
                len(claimed),
                graduation_year,
                stage,
            )
            error = exc
            break
        except ServiceError as exc:
            # Resend answered, and answered no — nothing was queued.
            await _release_claim(
                session,
                graduation_year=graduation_year,
                stage=stage,
                cycle_seq=cycle_seq,
                batch=claimed,
            )
            error = exc
            break
        sent += len(claimed)
        # Pace from Resend's own headers: if the window is exhausted wait for
        # its reset, otherwise a small gap to stay under the req/s cap.
        if remaining is not None and remaining <= 0 and reset:
            await asyncio.sleep(min(reset, _MAX_PACE_SLEEP_SECONDS))
        else:
            await asyncio.sleep(_INTER_BATCH_DELAY_SECONDS)
    return sent, retry_after, error


# ----------------------------------------------------- the account-wide budget -
#
# THE SEND BUDGET IS ENFORCED HERE, NOT BY THE CALLER (#417).
#
# `survey_schedule._run_allowance` — the configured daily/monthly limits minus
# what `survey_send_log` says has already gone out — used to be read in exactly
# ONE place: the cron body. The console's manual send never asked. Its `limit`
# query param defaults to None, the console sends none, and `send_campaign`
# passed that straight through — so pressing "Send now" on a large cohort emailed
# the WHOLE eligible stage in a single call: past the configured cap, past
# whatever the cron had already spent that day, into real alumni inboxes, and
# unrecallable. The console's own daily/monthly tallies sat beside the button
# describing a limit that only one of the two senders obeyed.
#
# Patching `send_campaign` would have fixed that one caller and left the same
# shape of hole for the next one — which is the identical mistake the 2026-08-02
# incident was made of (the manual path skipped the send-log callback, so the fix
# was to make logging impossible to skip rather than to pass it at that call
# site). So the gate lives where the sending lives: :func:`send_survey_stage`
# reads the budget itself and clamps its own recipient list. A caller can LOWER
# the ceiling with `limit`; there is no way for one to raise it.
#
# WHY THIS DOES NOT DOUBLE-COUNT THE CRON'S BUDGET. The cron reads the allowance
# once per run and decrements it locally as each year sends, passing what is left
# as `limit`. This module re-reads it, and the two agree by construction: every
# delivered email's claim is COMMITTED before the next year is considered, so
# `get_send_usage` has already absorbed exactly the emails the cron subtracted.
# `min(limit, allowance)` over two equal numbers is that number — the clamp is
# idempotent, not cumulative. Where they can differ, the re-read is the LOOSER
# one (a claim released after a 429 un-spends budget), so `min` keeps the cron's
# figure and the run still stops where it meant to.
#
# WHY NO LOCK IS TAKEN HERE. `send_lock` is a transaction-scoped advisory lock
# held on a CONNECTION OF ITS OWN, so it is not re-entrant: taking it again
# inside a send whose caller already holds it would come back false and every
# real send would decline itself. The lock therefore stays at the two entry
# points (:func:`send_campaign` and ``survey_schedule.run_due_schedules``), both
# of which already hold it around this call — which is what keeps the
# read-budget-then-claim sequence below safe from a second runner (#358).


async def remaining_send_allowance(session: AsyncSession) -> int | None:
    """Emails the account may still send right now; ``None`` = cap disabled.

    Delegates to ``survey_schedule._run_allowance`` rather than re-deriving it.
    The console's cap screen, the cron's pacing and this gate have to be ONE
    number: a second implementation of "daily and monthly, minus usage, whichever
    is tighter" is precisely how the meter and the sender come to disagree, and
    this subsystem has paid for that class of split twice already.

    Imported locally because ``survey_schedule`` imports this module at module
    level — the same cycle :func:`send_campaign` works around, for the same
    reason. Reached through the module rather than bound at import so the one
    implementation stays the one implementation.
    """
    from app.services import survey_schedule

    return await survey_schedule._run_allowance(session)


def _send_ceiling(limit: int | None, allowance: int | None) -> int | None:
    """How many recipients this send may take: the TIGHTEST stated ceiling.

    ``limit`` is the caller's own (the console's explicit number, or the cron's
    remaining run budget) and ``allowance`` is the account-wide one. ``None``
    from either means "that side is not capping"; ``None`` from both means
    uncapped, which is only ever true when the send cap is switched off.

    Deliberately ``min`` and not "the caller wins": an explicit limit is honoured
    when it asks for LESS than the budget and clamped when it asks for more.
    Sending is irreversible, so the ceiling has to be the lowest thing anyone
    said, never the most recent.
    """
    stated = [c for c in (limit, allowance) if c is not None]
    return min(stated) if stated else None


# ------------------------------------------------- the one send entry point ---


def _work_fallback_count(recipients: list[Recipient]) -> int:
    """How many of these are reached at their WORK address (#392)."""
    return sum(1 for r in recipients if r.email_source == email_reach.SOURCE_WORK)


@dataclass(frozen=True)
class SendOutcome:
    """What one call to :func:`send_survey_stage` did."""

    graduation_year: int
    # The stage sent, or None when nothing was owed at any permitted stage.
    stage: int | None
    eligible: list[Recipient]  # everyone eligible this year, deduped
    targets: list[Recipient]  # of those, not yet logged for `stage`
    prepared: list[Recipient]  # targets after the limit / send budget
    sent: int
    # The account-wide send budget as it stood when this call started, and what
    # is left of it now (None = the cap is switched off). Both, not one: the
    # console needs "how much was there" to explain a send that took nothing, and
    # "how much is left" to say what a retry would do.
    allowance: int | None = None
    budget_remaining: int | None = None
    # True when the BUDGET — not the caller's own `limit` — is what truncated
    # this send. An operator who typed a small limit must not be told the account
    # is out of emails, and an operator who typed nothing must not be left
    # guessing why a cohort of 300 got 12.
    budget_limited: bool = False
    retry_after: int | None = None
    duplicate_emails: int = 0
    # The dropped-for-a-shared-address recipients themselves, so the caller can
    # report the cohort breakdown without re-loading and re-deduping the whole
    # year (`eligible` + these two lists ARE the resolved population).
    duplicates: tuple[Recipient, ...] = ()


async def send_survey_stage(
    session: AsyncSession,
    *,
    graduation_year: int,
    max_stage: int,
    actor_user_id: int | None,
    dry_run: bool = False,
    limit: int | None = None,
    recipients: list[Recipient] | None = None,
    scheduled: bool = False,
    cycle_seq: int | None = None,
) -> SendOutcome:
    """Send one stage of a year's survey — the ONLY way either caller sends.

    Owns the whole irreversible step end to end: choose the stage, work out who
    is owed it, claim them, send, record the audit trail and commit. Both the
    manual console send (:func:`send_campaign`) and the daily cron
    (``survey_schedule.run_due_schedules``) go through here.

    That is the point. The unscheduled second send of 2026-08-02 happened
    because the manual path called the raw sender WITHOUT the callback that
    writes ``survey_send_log`` — it emailed a whole cohort and recorded nothing,
    so the cron saw a cohort that had never had its initial and sent it again.
    The fix is not to pass the callback at that one call site (which leaves the
    same trap for the next caller) but to make it impossible to send without
    recording: the raw sender is private, and logging is not optional here.

    ``recipients`` may be pre-loaded by the caller purely to avoid re-running the
    cohort query the cron already ran; when omitted they are loaded here. Either
    way they come from :func:`_load_recipients`, so "who is eligible" has exactly
    one implementation.

    ``limit`` is the caller's own ceiling (the console's explicit limit, or the
    scheduler's remaining run budget). It can only ever LOWER the send: the
    account-wide budget is read here (:func:`remaining_send_allowance`) and
    applied whether or not a caller passed anything, so a caller that says
    nothing — which is what the console does — is capped rather than uncapped
    (#417). See the section notes above for why the check is here rather than in
    the two callers, why it does not double-count the cron's budget, and why it
    takes no lock. Resend's 429 is still the real brake.

    A zero budget sends NOTHING and says so on the outcome (``budget_limited``
    with ``allowance == 0``) rather than reporting a clean "sent 0" — the two are
    indistinguishable to a caller otherwise, and one of them means a cohort is
    still owed its email. ``dry_run`` is clamped by the same ceiling, on purpose:
    a preview that promised more recipients than the real send would take is the
    standing bug class in this area.
    """
    settings = get_settings()
    base_url = settings.survey_app_base_url
    from_email = settings.survey_from_email
    if not base_url:
        raise ServiceError("SURVEY_APP_BASE_URL is not configured.")
    if not from_email:
        raise ServiceError("SURVEY_FROM_EMAIL is not configured.")
    from_field = f"{settings.survey_from_name} <{from_email}>"

    if recipients is None:
        recipients = await _load_recipients(session, graduation_year)
    eligible, duplicates = dedupe_by_email(recipients)
    if duplicates:
        # Visible to staff, not silent: each of these alumni shares an inbox with
        # someone we ARE emailing, and the email carries an edit token.
        log.warning(
            "Survey %s: %s alumni share an email address with another recipient "
            "and were skipped (alumni_ids=%s)",
            graduation_year,
            len(duplicates),
            ",".join(str(r.alumni_id) for r in duplicates[:20]),
        )

    # Which campaign this send belongs to (#357). Resolved ONCE and threaded
    # through target selection, claiming and any release, so a single send can
    # never straddle two cycles — re-reading it per batch would let a concurrent
    # "start new cycle" split one send across both.
    #
    # The cron passes the cycle it already read off the schedule row it is
    # iterating; re-resolving it here would make the completion check and the
    # send that follows it two independent reads that can disagree. Only the
    # manual path, which has no schedule in hand, looks it up.
    if cycle_seq is None:
        cycle_seq = await current_cycle_seq(session, graduation_year)

    stage, targets = await select_stage_targets(
        session,
        graduation_year=graduation_year,
        recipients=eligible,
        max_stage=max_stage,
        cycle_seq=cycle_seq,
    )
    # THE BUDGET GATE (#417). Read after the targets are known and before a
    # single claim is written, so what goes out is bounded by what the account
    # may still send — for the cron, for the console, and for whatever calls this
    # next. `eligible_alumni_query` orders by alumni_id precisely so this
    # truncation takes a stable prefix and the remainder is picked up next run.
    allowance = await remaining_send_allowance(session)
    prepared = [] if stage is None else targets
    ceiling = _send_ceiling(limit, allowance)
    if ceiling is not None:
        prepared = prepared[: max(ceiling, 0)]
    # Whether the budget is what did the cutting. `allowance <= limit` is the
    # test, not `len(prepared) < limit`: a caller asking for exactly the budget
    # gets everything it asked for and is still budget-bound for the remainder.
    budget_limited = (
        allowance is not None
        and len(prepared) < len(targets)
        and (limit is None or allowance <= limit)
    )

    sent = 0
    retry_after: int | None = None
    error: ServiceError | None = None
    if not dry_run and prepared and stage is not None:
        sent, retry_after, error = await _send_and_log(
            session,
            prepared,
            graduation_year=graduation_year,
            stage=stage,
            cycle_seq=cycle_seq,
            base_url=base_url,
            from_field=from_field,
        )

    # The audit row is the TRAIL, not the ledger — usage is counted from
    # `survey_send_log` (see `get_send_usage`), which an engineer actor's
    # rerouted audit row cannot silently zero out.
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="send_survey_dry_run" if dry_run else "send_survey",
            entity_type="survey_campaign",
            entity_id=graduation_year,
            new_value=(
                f"grad_year={graduation_year} stage={stage} cycle={cycle_seq} "
                f"scheduled={scheduled} recipients={len(eligible)} "
                f"targets={len(targets)} prepared={len(prepared)} "
                f"sent={sent} dry_run={dry_run}"
                + (
                    # How many of this send leaned on the work-email fallback
                    # (#392) — a permanent record of who was reached at which
                    # address, and a standing data-quality signal.
                    f" work_email_fallback={_work_fallback_count(prepared)}"
                    if _work_fallback_count(prepared)
                    else ""
                )
                + (f" duplicate_emails={len(duplicates)}" if duplicates else "")
                # The budget this send was measured against, on the permanent
                # record (#417). "Why did only 12 of that cohort go out?" is
                # answerable months later from the trail alone, and a send that
                # was cut short by the cap is distinguishable from one that had
                # nothing left to do.
                + (f" allowance={allowance}" if allowance is not None else "")
                + (" budget_limited=True" if budget_limited else "")
                + (f" throttled_retry_after={retry_after}" if retry_after else "")
                + (" failed=True" if error is not None else "")
            ),
        )
    )
    await session.commit()

    if error is not None and sent == 0:
        # Nothing went out and something is genuinely wrong (bad key, unverified
        # domain, network). The trail is committed; surface it rather than
        # reporting a clean "sent 0". When SOME batches did land we stay quiet
        # and report the partial send — that accounting is now durable.
        raise error

    return SendOutcome(
        graduation_year=graduation_year,
        stage=stage,
        eligible=eligible,
        targets=targets,
        prepared=prepared,
        sent=sent,
        allowance=allowance,
        # What a retry right now would have to spend. A dry run spent nothing, so
        # this is simply the allowance — which is exactly what makes a preview
        # able to say "a real send would email 0 of these people".
        budget_remaining=(None if allowance is None else max(0, allowance - sent)),
        budget_limited=budget_limited,
        retry_after=retry_after,
        duplicate_emails=len(duplicates),
        duplicates=tuple(duplicates),
    )


async def send_campaign(
    session: AsyncSession,
    *,
    graduation_year: int,
    actor_user_id: int | None,
    dry_run: bool = True,
    limit: int | None = None,
) -> SurveySendResult:
    """The console's manual send for a graduation year.

    A thin shell over :func:`send_survey_stage`: it resolves the stage ceiling
    from the year's schedule (:func:`campaign_max_stage`) and translates the
    outcome into the console's result shape. Everything that matters — who is
    eligible, who has already had this stage, the send log, the audit row, the
    commit boundary — is the shared helper's, identical to the cron's.

    A REAL send takes :func:`send_lock` first, so it cannot run alongside the
    daily cron (#358) — the two spending the same daily budget from independent
    reads of it is how the account gets pushed past the Resend plan limit, and
    "admin presses Send now while the 18:00 cron is mid-run" is the likeliest way
    that happens. A declined send is a 409: the caller is a person waiting on an
    answer, so it says so rather than reporting a silent "sent 0". A DRY RUN
    takes no lock — it sends nothing, spends no budget, and staff must be able to
    preview a cohort at any time, including while the cron is running.

    A REAL send to a year with NO campaign leaves one behind (#405), so the two
    reminders the cadence owes actually go out and the send is visible in the
    console. See ``survey_schedule.create_campaign_for_send`` for why that is
    automatic rather than a refusal, and why it cannot re-send the initial.

    THE SEND CAP APPLIES HERE TOO (#417). It is enforced inside
    :func:`send_survey_stage`, so this function neither reads nor passes it —
    ``limit`` stays what the operator typed, and the budget clamps it. What this
    function adds is the ANSWER A PERSON GETS: a real send with a spent budget is
    a 409 rather than a silent "sent 0", for the same reason a send that cannot
    take the lock is (the caller is waiting, and "0" reads as "the cohort had
    nothing owed"). A truncated send returns normally, carrying the numbers the
    console needs to say "N of M sent, budget exhausted".
    """
    # Local, because `survey_schedule` imports THIS module: the send belongs here
    # and the campaign row belongs there, and the manual send is the one caller
    # that needs both. A module-level import would be a cycle.
    from app.services import survey_schedule

    settings = get_settings()
    base_url = settings.survey_app_base_url
    # One read, two answers: the stage ceiling, and whether this year has a
    # campaign at all. Taken BEFORE the send, so the question being answered is
    # "was there a campaign when the operator pressed Send?" rather than "is
    # there one now?" — this call is about to create one.
    start_date = await schedule_start_date(session, graduation_year)
    max_stage = max_stage_for_start(start_date)
    # Resolved here rather than inside `send_survey_stage`, so the campaign
    # created below can be given the SAME cycle the send claimed under — passed,
    # not re-derived, exactly how the cron threads it (#357).
    cycle_seq = await current_cycle_seq(session, graduation_year)

    async def _send() -> SendOutcome:
        return await send_survey_stage(
            session,
            graduation_year=graduation_year,
            max_stage=max_stage,
            actor_user_id=actor_user_id,
            dry_run=dry_run,
            limit=limit,
            cycle_seq=cycle_seq,
        )

    campaign_created = False
    if dry_run:
        outcome = await _send()
    else:
        async with send_lock() as acquired:
            if not acquired:
                raise ConflictError(
                    "A survey send is already running. Wait for it to finish "
                    "and try again — nothing was sent."
                )
            outcome = await _send()
            # Nothing went out and the cap is why (#417). Raised BEFORE the
            # campaign-creation step and inside the lock, so a refused send
            # leaves the year exactly as it found it. `outcome.targets` is what
            # keeps this off the repair path of #405 — a year whose recipients
            # are all already claimed has no targets, sends nothing for a reason
            # that has nothing to do with the budget, and must still be allowed
            # to leave its backdated campaign behind.
            if (
                outcome.allowance == 0
                and outcome.stage is not None
                and outcome.targets
            ):
                raise ConflictError(
                    "The account-wide survey send budget is spent — no emails "
                    "remain in today's or this month's limit, so nothing was "
                    f"sent. {len(outcome.targets)} alumni are still owed this "
                    "email; raise the limit on the Surveys console, or leave it "
                    "and the daily scheduler will send them as budget frees up."
                )
            if start_date is None:
                # Inside the lock: the campaign is part of this send, and no
                # other sender may be mid-flight for the year while it appears.
                # A send that RAISED never gets here — the operator sees the
                # error, and their retry creates the campaign backdated to
                # whatever was claimed, because the creation reads the send log
                # rather than this call's outcome.
                campaign_created = await survey_schedule.create_campaign_for_send(
                    session,
                    graduation_year=graduation_year,
                    cycle_seq=cycle_seq,
                    actor_user_id=actor_user_id,
                )
    # What is left of THIS stage: the targets we did not send to. (Before, this
    # was measured against every eligible alum, which counted people who had
    # already received this exact email as still owed it.)
    done = len(outcome.prepared) if dry_run else outcome.sent
    remaining = len(outcome.targets) - done
    # The cohort account that lets the console explain the result — especially a
    # zero. It used to report every `sent=0` as "they need a personal email on
    # file", which was wrong for every other cause and was the misdiagnosis
    # behind #392: a cohort that had simply all replied within the 365-day window
    # was reported as having no email addresses.
    # Reuses the population the send already resolved rather than re-running the
    # cohort load — same numbers, and provably the ones that were sent to.
    breakdown = await recipient_breakdown(
        session,
        graduation_year,
        resolved=(outcome.eligible, list(outcome.duplicates)),
    )
    return SurveySendResult(
        graduation_year=graduation_year,
        total_recipients=len(outcome.eligible),
        prepared=len(outcome.prepared),
        sent=outcome.sent,
        remaining=remaining,
        dry_run=dry_run,
        retry_after_seconds=outcome.retry_after,
        # What the account may still send after this call, and whether that
        # budget is what cut this send short (#417). Together with `sent` and
        # `remaining` they are the whole of "12 of 300 sent, budget exhausted" —
        # which the console could not previously say, because the manual send did
        # not consult a budget at all.
        budget_remaining=outcome.budget_remaining,
        budget_limited=outcome.budget_limited,
        stage_complete=outcome.stage is None,
        campaign_created=campaign_created,
        breakdown=breakdown,
        sample=[
            SurveySendSample(
                email=r.email,
                link=_survey_link(base_url, r.alumni_id, graduation_year),
                email_source=r.email_source,
            )
            for r in outcome.prepared[:3]
        ],
    )

"""Response schemas for the survey-email send flow."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field


class SurveySubmitRequest(BaseModel):
    """The alum's submitted values, keyed by survey field keys (`table.column`).
    Only recognized survey fields are kept; anything else is ignored."""

    fields: dict[str, str]
    # True when the alum also picked a new profile photo to upload. A photo-only
    # submission (empty `fields`) must still create a response row so the page has
    # an id to attach the photo to — see `submit_response`.
    has_photo: bool = False


class SurveySubmitResult(BaseModel):
    """Outcome of a submit — how many changes were staged for review.

    `survey_response_id` is the id of the staged row (None when nothing was
    staged); the public survey page uses it to attach an optional profile photo
    via `POST /survey/respond/{token}/photo`."""

    staged: bool
    change_count: int
    survey_response_id: int | None = None


class SurveyChange(BaseModel):
    """One field an alum's response would change: what's on file vs submitted."""

    field_key: str
    label: str
    before: str
    after: str


class SurveyResponseItem(BaseModel):
    """One pending response for the admin review queue, with its diff."""

    survey_response_id: int
    alumni_id: int
    name: str
    submitted_at: str
    changes: list[SurveyChange]
    # Short-lived signed URL of a NEW profile photo the alum submitted with this
    # response, for the reviewer to preview. None when no photo was staged.
    photo_preview_url: str | None = None


class SurveyRespondInfo(BaseModel):
    """The alum's current on-file info for the public confirm page, resolved from
    a survey token. `fields` is keyed by the frontend's SURVEY_FIELDS keys
    (`table.column`), mirroring the sample-alum shape so the page can drop it in."""

    first_name: str
    full_name: str
    fields: dict[str, str]


class GraduationYearCount(BaseModel):
    """One graduation year present in the DB + how many eligible alumni it has.
    Drives the survey console's year picker."""

    graduation_year: int
    total_alumni: int
    # Distinct alumni in this grad year who have really replied within the last
    # 365 days — `pending` or `applied` (`survey_email.RESPONDED_STATUSES`).
    # Drives the console's "N replied" count. A `rejected` response is NOT a
    # reply: staff threw that submission away, nothing reached the record, so the
    # alum stays surveyable and still counts as a non-responder.
    responded: int = 0
    # Alumni this year's survey WANTS to email and cannot: no usable personal or
    # work address (#392). Reported next to the year in the picker so a cohort
    # with a contact-data gap is visible before a campaign is scheduled rather
    # than after it quietly under-delivers. Excludes suppressed alumni — see
    # `SurveyRecipientBreakdown`.
    unreachable: int = 0


class SurveySendSample(BaseModel):
    """One prepared recipient, surfaced in a dry-run so staff can eyeball it."""

    email: str
    link: str
    # Which column the address came from — "personal" or "work" (#392). A sample
    # full of work addresses is a visible cue that the cohort's personal-email
    # coverage is poor.
    email_source: str = "personal"


class SurveyRecipientBreakdown(BaseModel):
    """Who a year's survey reaches, and who it does not — the console's one
    account of a cohort (#392).

    The buckets PARTITION the year's alumni (is_alumni, not archived)::

        cohort_total = suppressed + already_responded + unreachable + eligible
        recipients   = eligible - duplicate_emails

    Every consumer reads these same numbers — the year picker, the send
    confirmation, and the send result — because they are produced by the same
    queries the send itself runs. Deriving a count separately from the send is
    the standing bug in this area: the console reports a figure, a different
    number goes out, and nobody can tell which was wrong.

    `suppressed` and `unreachable` are SEPARATE and must stay that way in the UI.
    Deceased / Do Not Contact is a decision to honour; no usable address is a gap
    to close. Summing them into one "not emailed" total would either hide real
    gaps or put Do Not Contact alumni on a chase list.
    """

    graduation_year: int
    # Everyone in the year, before any survey rule is applied.
    cohort_total: int
    # Deceased or Do Not Contact — deliberately never emailed. NOT unreachable.
    suppressed: int
    # Replied within the 365-day re-survey window, so not due again yet.
    already_responded: int
    # No usable personal OR work address. The gap; see the drill-down endpoint.
    unreachable: int
    # Passed every rule and has an address — before the shared-address dedupe.
    eligible: int
    # Of those, dropped for sharing an address with another recipient (spouses,
    # reused addresses). Each carries a live edit token, so only one is emailed.
    duplicate_emails: int
    # What a send would actually email: eligible - duplicate_emails.
    recipients: int
    # Of `recipients`, how many are reached at their WORK address because no
    # usable personal one exists — the population this change unblocked.
    work_email_fallback: int


class SurveyUnreachableAlum(BaseModel):
    """One alumnus this campaign cannot email (#392).

    The count made actionable, mirroring `SurveyNonResponder`: staff need names
    and the offending values, not a number. The reason separates "we have never
    had an address" from "the address we hold is unusable" — the second is often
    a typo fixable straight from this list.

    Never contains a suppressed (Deceased / Do Not Contact) alumnus.
    """

    alumni_id: int
    name: str
    # Machine-readable: "no_email" | "unusable_email".
    reason: str
    # Human-readable form of `reason`, so the UI need not duplicate the mapping.
    reason_label: str
    # Whatever IS on file, so a bad address can be corrected on sight. Both None
    # when the reason is "no_email".
    personal_email: str | None = None
    work_email: str | None = None


class SurveySendResult(BaseModel):
    """Summary returned by the send endpoint.

    `dry_run=True` prepares (and counts) everything but sends nothing — the safe
    default. `prepared` is how many emails were built for this call (capped by
    the daily limit); `sent` is how many actually went to Resend; `remaining` is
    recipients left over for a later day under the cap.
    """

    graduation_year: int
    total_recipients: int
    prepared: int
    sent: int
    remaining: int
    dry_run: bool
    # Set when Resend rate-limited us mid-send (429): seconds to wait before the
    # remaining recipients can be sent. None = not throttled. The limit is
    # Resend's, discovered from its response — never a value we configure.
    retry_after_seconds: int | None = None
    sample: list[SurveySendSample]
    # True when every permitted stage has already been delivered to everyone
    # owed it — i.e. `sent=0` because there is nothing left to send, not because
    # anything is wrong. Without this the console could only guess at a zero, and
    # it guessed WRONG: it blamed "they need a personal email on file" for every
    # zero-send, including cohorts that had simply all replied already (#392).
    stage_complete: bool = False
    # The full account of the cohort, so the console can state the REAL reason a
    # send was small or empty. Same numbers as the standalone breakdown endpoint
    # — one function produces both.
    breakdown: SurveyRecipientBreakdown | None = None


class SurveyUsage(BaseModel):
    """Real Resend send usage for the console's daily/monthly tallies — emails
    actually sent today and this calendar month, counted from `survey_send_log`.
    NOT from the audit trail: an engineer actor's audit row is rerouted to
    `engineer_action_log`, which left the meter reading zero. UTC day/month
    boundaries, matching the rest of the app's date filtering."""

    sent_today: int
    sent_this_month: int


# --------------------------------------------------------------- scheduler ----


class SurveyScheduleCreateRequest(BaseModel):
    """Create/replace the auto-send schedule for a graduation year (#542)."""

    graduation_year: int
    # Initial send date. The 1-week and 2-week reminders follow from here.
    start_date: datetime.date


class SurveyScheduleBulkRequest(BaseModel):
    """Create/replace the auto-send schedule for many graduation years at once
    (#542). Lets an admin schedule every class from one dialog instead of one at
    a time. A duplicate ``graduation_year`` in the list resolves to a single
    row — last one wins."""

    schedules: list[SurveyScheduleCreateRequest]


class SurveyNewCycleRequest(BaseModel):
    """Start the next survey campaign for a graduation year (#357).

    Carries only the new start date — the cycle number is server-assigned, never
    client-supplied, so a caller can neither skip a cycle nor re-open an old one
    and re-email against its log."""

    start_date: datetime.date


class SurveyNewCyclePreview(BaseModel):
    """What ``POST /survey/schedules/{year}/new-cycle`` WOULD do (#357).

    Backs the confirmation staff see before starting the next annual campaign.
    Starting a cycle emails the whole eligible cohort again and cannot be
    undone, so the dialog states the blast size in real numbers rather than
    asking "are you sure?" about an abstraction."""

    graduation_year: int
    current_cycle: int
    next_cycle: int
    # The campaign's status right now — a cycle started while the previous one is
    # still running is a likely mistake, so the UI can warn on it.
    current_status: str
    # Eligible alumni the new cycle would email (same eligibility as the send).
    would_email: int
    # How many of those already received the CURRENT cycle — i.e. the people who
    # would get a second email. This is the number that makes the ask concrete.
    previously_emailed: int


class SurveyScheduleItem(BaseModel):
    """One survey schedule + how many emails each stage has sent so far."""

    survey_schedule_id: int
    graduation_year: int
    start_date: datetime.date
    status: str
    # Which campaign this year is on (#357). 1 until someone starts a second
    # cycle. The per-stage counts below are scoped to THIS cycle, not all time.
    cycle_seq: int = 1
    last_run_at: datetime.datetime | None = None
    created_at: datetime.datetime | None = None
    # Who started this campaign, as a display name (falling back to their email).
    # The internal ``created_by_user_id`` PK is never disclosed — only the
    # resolved name leaves the API, matching ``InteractionRead.logged_by``
    # (FERPA — minimize internal identifiers). None when the schedule predates
    # the column, or the creator's account has since been deleted (FK SET NULL).
    created_by: str | None = None
    # When the campaign was paused — set only while ``status == 'paused'``, and
    # cleared on resume. The console shows it so "paused" is never an undated
    # state ("paused 3 days ago" is what tells staff a stopped campaign has been
    # forgotten about).
    paused_at: datetime.datetime | None = None
    # Delivered counts per stage from survey_send_log (0=initial, 1/2=reminders).
    sent_initial: int = 0
    sent_reminder_1: int = 0
    sent_reminder_2: int = 0
    # NEEDS MANUAL FOLLOW-UP (#359): alumni who received all three of THIS
    # cycle's emails and still never replied — #151's third step, which had no
    # implementation before. Without it `status='completed'` reads identically
    # whether the cohort all answered or none of them did. Cycle-scoped, so a
    # previous campaign's non-responders are not carried into this one, and a
    # `rejected` submission does not count as a reply (staff threw it away, so
    # the alum still needs chasing). The names are at
    # ``GET /survey/schedules/{year}/non-responders``.
    non_responders: int = 0


class SurveySchedulePauseAllResult(BaseModel):
    """Outcome of the engineer blanket pause (``POST /survey/schedules/pause-all``).

    Same shape and contract as :class:`SurveyScheduleCancelAllResult` — the two
    controls sit together in the console — but reports what was PAUSED, which is
    reversible: every year named here can be resumed and will pick its cadence up
    where it left off. Both fields are empty / 0 when nothing was running; the
    call is idempotent."""

    paused: int
    graduation_years: list[int]


class SurveyScheduleCancelAllResult(BaseModel):
    """Outcome of the engineer kill switch (``POST /survey/schedules/cancel-all``).

    Reports exactly what was stopped so the console can say so honestly rather
    than claiming a blanket success: ``cancelled`` is the number of campaigns
    moved to ``cancelled``, and ``graduation_years`` names them. Both are empty /
    0 when nothing was running — the call is idempotent."""

    cancelled: int
    graduation_years: list[int]


class SurveyScheduleRunItem(BaseModel):
    """What one due schedule did on this cron run."""

    graduation_year: int
    # The stage sent (0/1/2), or None if the campaign was already complete.
    stage: int | None
    sent: int
    remaining: int
    # Set when Resend rate-limited us mid-run (429): seconds to wait before the
    # remaining recipients go out. Picked up on the next cron run.
    retry_after_seconds: int | None = None
    # Set on the run that COMPLETES a campaign (#359): how many alumni received
    # every stage of this cycle and never replied, i.e. how many now need manual
    # follow-up. None on any other run — the campaign is still sending, so the
    # question isn't answerable yet. 0 is a real answer (everyone replied) and is
    # not the same as None.
    non_responders: int | None = None


class SurveyScheduleRunSummary(BaseModel):
    """Summary of a cron run over every due schedule."""

    ran: list[SurveyScheduleRunItem]
    # True when this run did nothing because another send (the cron, or an
    # admin's manual send) already held the send lock (#358). `ran` is empty and
    # not one email went out. This is a NORMAL, successful outcome, not an
    # error: Vercel Cron is at-least-once, sending is irreversible, and anything
    # that was due is still due on the next run. Reported rather than silent so
    # an empty `ran` is never ambiguous between "nothing was owed" and "someone
    # else is already doing it".
    skipped_locked: bool = False


class SurveyNonResponder(BaseModel):
    """One alum who needs manual follow-up (#359): they received every email of
    their year's current campaign and never replied.

    Enough to act on — a name and an address — and nothing more. The count alone
    (``SurveyScheduleItem.non_responders``) tells staff there is work; this tells
    them who to call."""

    alumni_id: int
    name: str
    # The personal email the survey was sent to. None only if the contact row has
    # since lost it.
    email: str | None = None
    # When the last of their three emails went out — how cold the trail is.
    last_sent_at: datetime.datetime | None = None


# ------------------------------------------------------------- send cap --------


class SurveySendConfigItem(BaseModel):
    """The account-wide send cap the scheduler paces against. When ``enabled``,
    the daily cron sends at most ``daily_limit`` emails per UTC day and
    ``monthly_limit`` per calendar month across every graduation year, spreading
    a big cohort over several days. When disabled there is no internal cap —
    sends are limited only by Resend."""

    enabled: bool
    daily_limit: int
    monthly_limit: int


class SurveySendConfigUpdateRequest(BaseModel):
    """Update the send cap (in-console admin control). ``enabled`` false turns
    the cap off (e.g. after upgrading the Resend plan)."""

    enabled: bool
    daily_limit: int = Field(ge=0)
    monthly_limit: int = Field(ge=0)


# ----------------------------------------------- per-alumnus campaign reset ----
#
# The engineer's replacement for hand-running SQL to re-survey ONE person (#395).
# Two shapes: what their survey state is right now (`SurveyAlumniState`) and what
# a reset actually removed (`SurveyResetResult`). Everything here is scoped to a
# single alumni_id — there is deliberately no cohort- or year-wide variant.


class SurveyAlumniSend(BaseModel):
    """One survey email this alumnus was actually sent (`survey_send_log`)."""

    graduation_year: int
    cycle_seq: int
    # 0 = initial, 1 = 1-week reminder, 2 = 2-week reminder.
    stage: int
    stage_label: str
    sent_at: datetime.datetime
    # True when this row belongs to the cohort's CURRENT campaign — the only
    # rows that can block a re-send. An old cycle's rows are inert history, so
    # showing "we emailed them" without this would make every long-standing
    # alumnus look blocked.
    current_cycle: bool


class SurveyAlumniResponse(BaseModel):
    """One submission this alumnus made (`survey_responses`), any status."""

    survey_response_id: int
    submitted_at: datetime.datetime
    # 'pending' (awaiting review), 'applied' (written to the record), or
    # 'rejected' (thrown away by staff).
    status: str
    # How many fields the submission carried, and whether a photo came with it —
    # a plain measure of what a reset would destroy.
    field_count: int
    has_photo: bool
    # True when this reply falls inside the 365-day re-survey window AND counts
    # as a reply (`survey_email.RESPONDED_STATUSES` — `rejected` does not), i.e.
    # it is what is currently holding the alumnus out of a send.
    blocks_resend: bool


class SurveyAlumniState(BaseModel):
    """An alumnus's complete survey state, for the engineer to read BEFORE
    deciding whether a reset is warranted (#395).

    The point of this shape is that a reset is USUALLY THE WRONG MOVE: someone
    can look blocked simply because they legitimately answered three months ago,
    and deleting that answer to re-ask them destroys a real reply. So the state
    is reported as facts (what went out, what came back, when, with what status)
    plus `blocked_reasons` in plain words, rather than a single yes/no.
    """

    alumni_id: int
    name: str
    graduation_year: int | None = None
    email: str | None = None
    archived: bool = False
    # The cohort's campaign, when the year has one — status/start date/cycle, so
    # the engineer can tell a live campaign from finished history.
    schedule_status: str | None = None
    schedule_start_date: datetime.date | None = None
    schedule_cycle_seq: int | None = None
    sends: list[SurveyAlumniSend]
    responses: list[SurveyAlumniResponse]
    # Why another survey email would NOT reach this person today, in plain
    # words — empty when nothing is holding them back, in which case a reset is
    # pure data loss and the UI says so.
    blocked_reasons: list[str]


class SurveyResetResult(BaseModel):
    """What a per-alumnus reset actually deleted (#395).

    Counts, not booleans, because the audit trail records these and "we removed
    3 emails and 1 reply" is the only useful answer to "what did that button
    do?". A reset that found nothing succeeds and reports zeros."""

    alumni_id: int
    name: str
    # Rows removed from `survey_send_log` — what unblocks a repeat send inside
    # the current cycle.
    sends_deleted: int
    # Rows removed from `survey_responses` (EVERY status, including `rejected`)
    # — what clears the 365-day re-survey window.
    responses_deleted: int
    # Staged survey photos removed from the headshots bucket alongside their
    # rows, so a deleted response never leaves an orphaned image behind.
    staged_photos_deleted: int

"""Response schemas for the survey-email send flow."""

from __future__ import annotations

import datetime

from pydantic import BaseModel, Field

from app.schemas.alumni import DuplicateWarning


class SurveySubmitRequest(BaseModel):
    """The alum's submitted values, keyed by survey field keys (`table.column`).
    Only recognized survey fields are kept; anything else is ignored."""

    fields: dict[str, str]
    # True when the alum also picked a new profile photo to upload. A photo-only
    # submission (empty `fields`) must still create a response row so the page has
    # an id to attach the photo to — see `submit_response`.
    has_photo: bool = False
    # "Yes, everything is correct" (#755) — a reply that changes NOTHING. Send it
    # with `fields: {}` and `has_photo: false`; it records a `confirmed` response
    # so the alum counts toward the response rate and stops getting reminders,
    # which pressing that button previously did not do at all.
    #
    # IGNORED when the submission carries anything else. A body with real fields
    # or a photo describes a submission WITH changes, and honouring the flag
    # would throw them away — so content always wins. Send the flag on its own.
    confirmed_only: bool = False


class SurveySubmitResult(BaseModel):
    """Outcome of a submit — how many changes were staged for review.

    `survey_response_id` is the id of the staged row (None when nothing was
    staged); the public survey page uses it to attach an optional profile photo
    via `POST /survey/respond/{token}/photo`."""

    staged: bool
    change_count: int
    survey_response_id: int | None = None
    # True when the row this result points at is a CONFIRMATION (#755) — a reply
    # that changed nothing. It answers "is the alum's reply on record as a
    # confirmation?", not "did this call create a row": a repeated confirm is
    # idempotent and reports the reply that already existed. So it is False when
    # the alum had already submitted real changes and a stale tab confirmed
    # afterwards — that submission stands and is NOT overwritten.
    confirmed: bool = False


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


class SurveyApplyResult(BaseModel):
    """What applying a staged response reports back (#646).

    The apply itself is already done and committed — this is not a confirmation,
    it is the one thing the reviewer could not have known before clicking:
    ``duplicate_warnings`` is non-empty when the response RENAMED the alumnus into
    a collision with a live record (same first + last name and graduation year).

    Warn-and-continue, matching the staff rename path this reuses (#627): two
    alumni genuinely can share a name and a year, and a marriage rename into a
    real collision is sometimes correct. Empty on every apply that didn't move
    ``first_name`` / ``last_name`` — the check isn't even run.

    The endpoint used to be a bodyless 204. Consumers that ignore the body keep
    working; the frontend's generated client needs a regen.
    """

    duplicate_warnings: list[DuplicateWarning] = []
    # True when the response HAD a photo staged but it could not be decoded, so
    # the field changes were applied and the photo was thrown away. The reviewer
    # approved a submission that showed a photo, and the alum's profile still
    # shows the old one — if this is not surfaced they will believe the new photo
    # went live. Warn-and-continue for the same reason as the duplicates above:
    # failing the apply would leave a response with good field changes stuck
    # pending forever, since the retry hits the same undecodable bytes.
    photo_dropped: bool = False


class SurveySupportContact(BaseModel):
    """Who a survey respondent may email directly (#774) — a NAME and an ADDRESS,
    and deliberately nothing else.

    ⚠️ THIS IS A NARROW, DELIBERATE EXCEPTION to the support-contacts privacy
    rule, not an oversight. `app/api/routes/support.py` says there is
    "deliberately NO unauthenticated endpoint, so these names/emails are never
    exposed on the public login page", and that still holds: the rule protects a
    surface anyone on the internet can load. This one rides on
    `GET /survey/respond/{token}`, which needs a valid HMAC-signed survey token
    we mailed to one alum, and it carries exactly ONE contact — the row the
    engineer labelled for the survey — never the list.

    So the exposure is one chosen person's work name and work address, to someone
    already holding a link addressed to them, on the page that asks them to reply.
    Keep it that way: no `support_contact_id`, no `role_label`, no `sort_order`,
    no second contact. Those would turn a mailbox we are advertising back into
    the staff directory the rule exists to protect.
    """

    #: Display name for the link text ("Email Tanya Harmon"). Never empty — the
    #: resolver falls back to the address itself, so the frontend never renders
    #: "Email " with nothing after it.
    name: str
    #: The address the `mailto:` opens. Shape-checked by the resolver; a row whose
    #: email does not look like an address yields `null` for the whole contact.
    email: str


class SurveyRespondInfo(BaseModel):
    """The alum's current on-file info for the public confirm page, resolved from
    a survey token. `fields` is keyed by the frontend's SURVEY_FIELDS keys
    (`table.column`), mirroring the sample-alum shape so the page can drop it in."""

    first_name: str
    full_name: str
    fields: dict[str, str]
    # The "email us directly" contact at the foot of the survey (#774), resolved
    # from the engineer-managed `support_contacts` table by role label — see
    # `survey_email.survey_support_contact`.
    #
    # ⚠️ `None` IS A REAL ANSWER, not a failure. It means no contact is
    # configured (or the configured one is unusable), and the frontend renders
    # NOTHING for it. There is deliberately no fallback address: a `mailto:` that
    # opens a message to the wrong mailbox is worse than no button at all,
    # because the respondent believes they have reached a human and stops looking
    # for another way. The survey itself must still load either way.
    support_contact: SurveySupportContact | None = None


class GraduationYearCount(BaseModel):
    """One graduation year present in the DB + how many eligible alumni it has.
    Drives the survey console's year picker."""

    graduation_year: int
    total_alumni: int
    # Distinct alumni in this grad year who have really replied within the last
    # 365 days — `pending`, `applied` or `confirmed`
    # (`survey_email.RESPONDED_STATUSES`).
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


class SurveyHeldOutAlum(BaseModel):
    """One alumnus this year's send is holding out, and why (#658).

    `SurveyRecipientBreakdown` reports the exclusions as three counts. A count is
    not actionable: a cancelled-then-re-sent campaign told staff "1 already
    replied within the last year" and there was no way to find out who, so the
    cohort had to be searched by hand until she turned up. This is that same
    number with a name on it.

    `alumni_id` is here because it is what the engineer's next call needs — the
    state/reset pair (`GET|POST /survey/alumni/{alumni_id}/...`) is keyed on it,
    and that reset is the only thing that makes a recent responder sendable again.
    """

    alumni_id: int
    name: str
    # Which bucket of the breakdown they fell into — "suppressed" |
    # "already_responded" | "unreachable". Machine-readable; the same three
    # strings the endpoint's `reason` filter accepts.
    reason: str
    # Human-readable form of `reason`, so the UI need not duplicate the mapping.
    reason_label: str
    # WHEN they replied — set only for `already_responded`, None for the other
    # two buckets. This is the fact the re-send decision actually turns on: an
    # answer from three months ago is a reason to leave them alone, one from a
    # retired campaign an hour before the cohort was re-sent is not. Without it
    # the row would say someone is blocked and leave the operator no better able
    # to judge whether unblocking them is reasonable.
    last_reply_at: datetime.datetime | None = None


class SurveyHeldOutPage(BaseModel):
    """A page of the held-out list, plus the size of the whole set (#658).

    `total` is the count BEFORE paging, and it is the number the console can
    check its own breakdown against: both come from the same predicates, so
    `total` for `reason="already_responded"` is
    `SurveyRecipientBreakdown.already_responded` by construction. If they ever
    differ, something has re-derived one of them and that is the bug.
    """

    graduation_year: int
    # Which bucket was asked for, echoed back; None = all three.
    reason: str | None = None
    # Rows matching `reason` for this year, ignoring `limit` / `offset`.
    total: int
    limit: int
    offset: int
    items: list[SurveyHeldOutAlum]


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
    # True when this send had to create the year's campaign because there wasn't
    # one (#405). A manual send used to write send-log rows and no
    # `survey_schedule` row, and the schedule is what drives the day 0 / +7 / +14
    # reminders — so the initial went out, both reminders silently never did, and
    # the console listed no campaign. The send now leaves one behind; this says so
    # out loud, because "we also started a campaign for this cohort" is a
    # consequence the operator should see rather than discover.
    campaign_created: bool = False
    # The full account of the cohort, so the console can state the REAL reason a
    # send was small or empty. Same numbers as the standalone breakdown endpoint
    # — one function produces both.
    breakdown: SurveyRecipientBreakdown | None = None
    # How many emails the account-wide daily/monthly cap still allows AFTER this
    # call; None when the cap is switched off. A dry run spends nothing, so for a
    # preview this is simply what is available right now.
    budget_remaining: int | None = None
    # True when that cap — not the caller's own `limit` — is what truncated this
    # send (#417). The manual send used to ignore the budget entirely, so "Send
    # now" on a large cohort emailed the whole stage past a limit the console was
    # displaying beside the button. Now it is clamped, and this is what lets the
    # console say "12 of 300 sent, budget exhausted" rather than leaving an
    # operator to read a short send as a broken one.
    budget_limited: bool = False


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
    # Emails this year has EVER been sent, across every cycle (#398) — unlike
    # the three per-stage counts above, which are this cycle only. It is what
    # decides whether the campaign can be deleted or only cancelled: once a year
    # has send-log rows, its schedule row is the only thing holding the
    # `cycle_seq` that keeps them scoped to a past campaign, so deleting it would
    # make the next campaign for that year skip everyone (#357). The console uses
    # this to offer the honest control, and the backend enforces it either way.
    emails_sent_all_time: int = 0
    # AT-A-GLANCE PROGRESS (#543/#497). The three counts above say what LEFT;
    # these say whether it worked, which nothing reported while a campaign was
    # still running. `non_responders` cannot fill that gap — it only counts
    # people who have had all three emails, so it is legitimately 0 for the first
    # fortnight of every campaign no matter how many have answered.
    #
    # All three are scoped to the year's CURRENT cycle and counted over the send
    # log, so they share one denominator: nobody can reply to a survey they were
    # never sent, and a stray response from outside the cycle cannot push the
    # rate past 100%. "Replied" is the sender's own definition — a pending,
    # applied or confirmed response inside the re-survey window, not superseded
    # by a reset — so a cohort never reads as answered while the sender still
    # owes it email.
    #
    # The response RATE is deliberately not a field: it is replied/recipients,
    # and a stored copy is one more thing that can disagree with its own inputs.
    recipients: int = 0
    replied: int = 0
    # Replies sitting in the review queue — the actionable number, since these
    # are answers nobody has applied or rejected yet.
    awaiting_review: int = 0
    # REVIEW OUTCOME (#497). `awaiting_review` says how much is still queued;
    # these two say what happened to the rest — i.e. how much of what came back
    # was actually usable, which nothing reported anywhere. Same cycle scope,
    # same re-survey window, same reset rule as the three counts above, so they
    # are read against the same denominator.
    #
    # `applied`: staff accepted the submission and it was written to the record.
    applied: int = 0
    # `rejected`: staff THREW THE SUBMISSION AWAY (spam, junk, someone else's
    # data). It is NOT a reply and is deliberately not part of `replied` —
    # nothing reached the record, so that alum still owes us an answer and the
    # sender will email them again. Displaying it beside `replied` needs the same
    # care the console footer already takes with `non_responders`: the same
    # person legitimately appears under `rejected` AND under `non_responders`,
    # and that is not a contradiction.
    #
    # All six are counts of DISTINCT ALUMNI, not of submissions, so that they
    # are comparable with `recipients`. One alum who submitted twice and had one
    # applied and one rejected is counted in both columns — so
    # `awaiting_review + applied + rejected` need not equal `replied`, and none
    # of them is a partition of anything. Do not compute a rate from them.
    rejected: int = 0
    # `confirmed` (#755): alumni who answered "yes, everything is correct" —
    # a reply that changed nothing, so there is no submission to review and
    # nothing was written to the record. They ARE part of `replied` (unlike
    # `rejected`), and this column is what explains the gap: without it a cohort
    # reads as "40 replied, 3 awaiting review, 5 applied" with 32 unaccounted
    # for, which looks like a bug in the table rather than the best possible
    # outcome — an alum whose record was already right.
    confirmed: int = 0


class SurveySchedulePauseAllResult(BaseModel):
    """Outcome of the engineer blanket pause (``POST /survey/schedules/pause-all``).

    Same shape and contract as :class:`SurveyScheduleCancelAllResult` — the two
    controls sit together in the console — but reports what was PAUSED, which is
    reversible: every year named here can be resumed and will pick its cadence up
    where it left off. Both fields are empty / 0 when nothing was running; the
    call is idempotent."""

    paused: int
    graduation_years: list[int]


class SurveyScheduleDeleteResult(BaseModel):
    """Outcome of removing a campaign (``DELETE /survey/schedules/{year}``, #398).

    Only the ``survey_schedule`` row goes, whatever the campaign's status. Every
    number here is a KEPT count, because the reasonable assumption about a button
    labelled "delete campaign" is that the emails and the alumni's submitted
    answers went with it. They did not — they were RETIRED, which is a statement
    about what the next campaign for this year can see, not about what is in the
    database — and the console says the numbers out loud rather than leaving the
    assumption standing."""

    graduation_year: int
    # What the campaign's status was before it was removed — the console echoes
    # it so "deleted a scheduled campaign" and "deleted a completed one" are
    # distinguishable after the fact.
    previous_status: str
    # The cycle this campaign was on, now retired: its send-log rows keep this
    # number and stop counting as current.
    retired_cycle: int
    # Where a new campaign for this year starts. Above `retired_cycle` by
    # construction, which is what makes the alumni it emailed reachable again and
    # keeps the send log's unique key from refusing their new rows.
    next_cycle: int
    # Emails this campaign sent. Still in `survey_send_log`, still on the alumni's
    # profiles, still counted by the Resend usage meter — they really were sent.
    emails_retired: int
    # Survey responses for this graduation year still in the database. Untouched.
    responses_kept: int


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
# a reset superseded (`SurveyResetResult`). Everything here is scoped to a single
# alumni_id — there is deliberately no cohort- or year-wide variant.
#
# NOTHING IS DELETED (Jake, 2026-08-05). A reset records an event; the responses
# and the send log stay in the database. These shapes therefore report what was
# SUPERSEDED and what was PRESERVED, and the UI must not describe the action as
# deletion.


class SurveyAlumniSend(BaseModel):
    """One survey email this alumnus was actually sent (`survey_send_log`)."""

    graduation_year: int
    cycle_seq: int
    # 0 = initial, 1 = 1-week reminder, 2 = 2-week reminder.
    stage: int
    stage_label: str
    sent_at: datetime.datetime
    # True when an engineer reset has happened since this email went out (#395).
    # The row is kept — the email really was sent — but it no longer holds them
    # out of anything.
    superseded: bool = False
    # True when this row belongs to the cohort's CURRENT campaign AND has not
    # been superseded — the only rows that can block a re-send. An old cycle's
    # rows are inert history, so showing "we emailed them" without this would
    # make every long-standing alumnus look blocked.
    current_cycle: bool


class SurveyAlumniResponse(BaseModel):
    """One submission this alumnus made (`survey_responses`), any status."""

    survey_response_id: int
    submitted_at: datetime.datetime
    # 'pending' (awaiting review), 'applied' (written to the record),
    # 'rejected' (thrown away by staff), or 'confirmed' (#755 — "yes, everything
    # is correct": a reply that changed nothing, so `field_count` is 0 by
    # definition and there is nothing to review).
    status: str
    # How many fields the submission carried, and whether a photo came with it.
    field_count: int
    has_photo: bool
    # True when an engineer reset happened after this was submitted (#395): the
    # answer belongs to a previous survey cycle. It is still in the database,
    # still on the profile's Surveys tab, and still reviewable if it is pending —
    # it simply stopped counting toward eligibility.
    superseded: bool = False
    # True when this reply falls inside the 365-day re-survey window, counts as
    # a reply (`survey_email.RESPONDED_STATUSES` — `rejected` does not) and has
    # not been superseded, i.e. it is what is currently holding the alumnus out
    # of a send.
    blocks_resend: bool


class SurveyAlumniState(BaseModel):
    """An alumnus's complete survey state, for the engineer to read BEFORE
    deciding whether a reset is warranted (#395).

    A reset destroys nothing, but it is still usually unnecessary: someone can
    look blocked simply because they legitimately answered three months ago, and
    re-asking them then is a judgement call, not a repair. So the state is
    reported as facts (what went out, what came back, when, with what status,
    what a previous reset already superseded) plus `blocked_reasons` in plain
    words, rather than a single yes/no.
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
    # How many times this alumnus has already been reset, and when the last one
    # was — so the screen can say "reset twice, most recently on ..." rather than
    # leaving superseded rows looking unexplained.
    reset_count: int = 0
    last_reset_at: datetime.datetime | None = None
    # Why another survey email would NOT reach this person today, in plain
    # words — empty when nothing is holding them back, in which case a reset
    # changes nothing and the UI says so.
    blocked_reasons: list[str]


class SurveyResetResult(BaseModel):
    """What a per-alumnus reset did (#395, revised 2026-08-05).

    NOTHING IS DELETED. The counts say what stopped counting toward eligibility
    and — just as importantly — what is still there, because the operator has to
    be able to see that their answers survived. A reset that found nothing
    succeeds and reports zeros."""

    alumni_id: int
    name: str
    # Which reset this was for them (1 = the first). Also the value their next
    # survey email's `survey_send_log` row will carry.
    reset_seq: int
    # `survey_send_log` rows that stopped blocking. The rows are KEPT.
    sends_superseded: int
    # `survey_responses` rows that stopped counting toward the 365-day
    # re-survey window. The rows are KEPT, untouched, at every status.
    responses_superseded: int
    # Everything of theirs still in `survey_responses` afterwards — i.e. all of
    # it. Reported so the console can say plainly that nothing was lost.
    responses_preserved: int
    # Of those, how many are still `pending`: awaiting review, still in the
    # review queue, still applicable to the record, staged photo intact.
    pending_preserved: int

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
    # Distinct alumni in this grad year who have submitted a survey response (any
    # status — pending/applied/rejected; a reply is a reply). Drives the console's
    # "N replied" count.
    responded: int = 0


class SurveySendSample(BaseModel):
    """One prepared recipient, surfaced in a dry-run so staff can eyeball it."""

    email: str
    link: str


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


class SurveyUsage(BaseModel):
    """Real Resend send usage for the console's daily/monthly tallies — emails
    actually sent today and this calendar month (summed from the `send_survey`
    audit rows). UTC day/month boundaries, matching the rest of the app's date
    filtering."""

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


class SurveyScheduleItem(BaseModel):
    """One survey schedule + how many emails each stage has sent so far."""

    survey_schedule_id: int
    graduation_year: int
    start_date: datetime.date
    status: str
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


class SurveyScheduleRunSummary(BaseModel):
    """Summary of a cron run over every due schedule."""

    ran: list[SurveyScheduleRunItem]


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

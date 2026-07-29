"""Response schemas for the survey-email send flow."""

from __future__ import annotations

from pydantic import BaseModel


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

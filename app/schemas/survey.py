"""Response schemas for the survey-email send flow."""

from __future__ import annotations

from pydantic import BaseModel


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
    sample: list[SurveySendSample]

"""Schemas for the new-people survey (api#661).

One shared link the career directors email to people who are not in the database
yet. Public routes read the form's rules and HOLD a submission; staff routes
manage the link and review what was held. Field keys are the existing survey
field keys (``profile.first_name``, ``contact.personal_email``, ...) plus two
new ones for the BYU-alum path: ``new.graduation_year`` and ``new.byu_degree``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, field_validator

from app.schemas.alumni import DuplicateWarning
from app.schemas.survey import SurveySupportContact, validate_survey_comment

NewPersonKind = Literal["alum", "friend"]


class NewPeopleRequiredFields(BaseModel):
    """Required fields per kind. Each inner list is an ANY-OF group: at least one
    key in it must be answered (so the email group is personal OR work)."""

    alum: list[list[str]]
    friend: list[list[str]]


class NewPeopleSurveyInfo(BaseModel):
    """``GET /survey/new/{key}``: what the public form needs before it renders.
    The server is the single source of truth for which fields are required."""

    required_fields: NewPeopleRequiredFields
    support_contact: SurveySupportContact | None = None


class NewPersonSubmitRequest(BaseModel):
    """``POST /survey/new/{key}``.

    ``fields`` takes the same keys, caps and cleaning as
    ``SurveySubmitRequest.fields``; ``comments`` and ``fill_seconds`` follow the
    same rules as there. ``website`` is a HONEYPOT: a hidden input a person never
    fills. Anything in it means a bot, and the server answers 200 while storing
    nothing.
    """

    kind: NewPersonKind
    fields: dict[str, str]
    comments: str | None = None
    fill_seconds: int | None = None
    website: str = ""

    @field_validator("comments", mode="before")
    @classmethod
    def _clean_comments(cls, value: object) -> str | None:
        return validate_survey_comment(value)


class NewPersonSubmitResult(BaseModel):
    """Always the same body, so a bot cannot tell a held submission from a
    honeypot drop."""

    received: bool = True


class NewPeopleLinkRead(BaseModel):
    """The shared link as staff see it. ``key`` is the secret in the public URL."""

    enabled: bool
    key: str
    updated_at: str


class NewPeopleLinkUpdate(BaseModel):
    """``PUT /survey/new-people/link``: the on/off switch."""

    enabled: bool


class NewPersonSubmissionField(BaseModel):
    """One answered question, in display form."""

    field_key: str
    label: str
    value: str


class NewPersonPossibleMatch(BaseModel):
    """A live record that may be the same person (email or first + last name)."""

    alumni_id: int
    name: str
    graduation_year: int | None = None
    is_friend: bool
    match_reason: Literal["email", "name"]


class NewPersonSubmissionItem(BaseModel):
    """One pending submission in the New people review queue."""

    submission_id: int
    kind: NewPersonKind
    # ISO timestamp. Send it back unchanged on approve (the stale check).
    submitted_at: str
    name: str
    email: str | None = None
    fields: list[NewPersonSubmissionField]
    comments: str | None = None
    possible_matches: list[NewPersonPossibleMatch]


class NewPersonApproveRequest(BaseModel):
    """``POST /survey/new-people/submissions/{id}/approve``.

    ``create`` makes a new record (``kind`` overrides the submitted kind; null
    keeps it). ``merge`` applies the answers to ``alumni_id`` (required) as if
    that person had answered the normal survey. ``submitted_at`` must equal the
    row's, or the approve is refused with 409.
    """

    action: Literal["create", "merge"]
    kind: NewPersonKind | None = None
    alumni_id: int | None = None
    submitted_at: str


class NewPersonApproveResult(BaseModel):
    """The record the approval created or updated, plus soft duplicate warnings
    (never blocking: the write is already committed)."""

    alumni_id: int
    created: bool
    duplicate_warnings: list[DuplicateWarning] = []

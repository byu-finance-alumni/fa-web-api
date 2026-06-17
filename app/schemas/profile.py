"""Aggregate alumni-profile read schemas.

``ProfileRead`` is the payload behind ``GET /alumni/{id}/profile`` — the alumni
core plus every related collection the profile tabs render (Contact, Career,
Employment, Leadership, Engagement, Survey, Interactions, Tasks, Attachments).
Read-only; per-tab write endpoints are separate. All sections are optional so a
sparse record (most fields awaiting data) serializes cleanly.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.dropdowns import (
    validate_industry,
    validate_status_label,
    validate_tag,
)
from app.schemas.alumni import AlumniRead


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EmploymentHistoryCreate(BaseModel):
    """Add a prior role to an alumnus's employment history (Employment panel)."""

    model_config = ConfigDict(extra="forbid")

    employer_name: str = Field(min_length=1, max_length=255)
    employment_title: str | None = Field(default=None, max_length=255)
    employment_industry: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    start_year: int | None = Field(default=None, ge=1900, le=2100)
    end_year: int | None = Field(default=None, ge=1900, le=2100)
    is_current: bool = False

    @field_validator("employment_industry", mode="before")
    @classmethod
    def _check_industry(cls, value: str | None) -> str | None:
        return validate_industry(value)

    @field_validator("employer_name", mode="before")
    @classmethod
    def _employer_trim(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value

    @field_validator("employment_title", "city", "state", mode="before")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class EmploymentHistoryUpdate(BaseModel):
    """Edit fields on an existing employment-history row (all optional)."""

    model_config = ConfigDict(extra="forbid")

    employer_name: str | None = Field(default=None, min_length=1, max_length=255)
    employment_title: str | None = Field(default=None, max_length=255)
    employment_industry: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    start_year: int | None = Field(default=None, ge=1900, le=2100)
    end_year: int | None = Field(default=None, ge=1900, le=2100)
    is_current: bool | None = None

    @field_validator("employment_industry", mode="before")
    @classmethod
    def _check_industry(cls, value: str | None) -> str | None:
        return validate_industry(value)

    @field_validator("employer_name", mode="before")
    @classmethod
    def _employer_trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("employment_title", "city", "state", mode="before")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class EducationCreate(BaseModel):
    """Add an education entry to an alumnus's record (Education panel)."""

    model_config = ConfigDict(extra="forbid")

    university: str | None = Field(default=None, max_length=255)
    college: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    degree: str | None = Field(default=None, max_length=255)
    major: str | None = Field(default=None, max_length=255)
    degree_status: str | None = Field(default=None, max_length=100)
    degree_year: int | None = Field(default=None, ge=1900, le=2100)

    @field_validator(
        "university",
        "college",
        "department",
        "degree",
        "major",
        "degree_status",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class EducationUpdate(EducationCreate):
    """Edit fields on an existing education entry (all optional, same shape)."""


class LeadershipCreate(BaseModel):
    """Add a Finance Society leadership entry to an alumnus's record."""

    model_config = ConfigDict(extra="forbid")

    leadership_role: str = Field(min_length=1, max_length=100)
    role_year: int | None = Field(default=None, ge=1900, le=2100)

    @field_validator("leadership_role", mode="before")
    @classmethod
    def _role_trim(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class LeadershipUpdate(BaseModel):
    """Edit fields on an existing leadership entry (all optional)."""

    model_config = ConfigDict(extra="forbid")

    leadership_role: str | None = Field(default=None, min_length=1, max_length=100)
    role_year: int | None = Field(default=None, ge=1900, le=2100)

    @field_validator("leadership_role", mode="before")
    @classmethod
    def _role_trim(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class TagCreate(BaseModel):
    """Attach a canonical engagement tag to an alumnus."""

    model_config = ConfigDict(extra="forbid")

    tag: str

    @field_validator("tag", mode="before")
    @classmethod
    def _check_tag(cls, value: str) -> str:
        return validate_tag(value)


class StatusLabelCreate(BaseModel):
    """Attach a canonical status label to an alumnus."""

    model_config = ConfigDict(extra="forbid")

    label: str

    @field_validator("label", mode="before")
    @classmethod
    def _check_label(cls, value: str) -> str:
        return validate_status_label(value)


class EventAttendanceCreate(BaseModel):
    """Mark an alumnus as an attendee of an existing event."""

    model_config = ConfigDict(extra="forbid")

    event_id: int
    attendance_status: str | None = Field(default=None, max_length=100)

    @field_validator("attendance_status", mode="before")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class InteractionCreate(BaseModel):
    """Log an interaction against an alumni (Interactions tab)."""

    model_config = ConfigDict(extra="forbid")

    interaction_type: str = Field(min_length=1, max_length=100)
    interaction_date_time: datetime.datetime | None = None
    interaction_notes: str | None = None


class TaskCreate(BaseModel):
    """Create a follow-up task (Tasks tab)."""

    model_config = ConfigDict(extra="forbid")

    task_title: str = Field(min_length=1, max_length=255)
    due_date: datetime.date | None = None
    task_notes: str | None = None


class TaskCompleteUpdate(BaseModel):
    """Toggle a task's completion state."""

    model_config = ConfigDict(extra="forbid")

    completed: bool


class ContactRead(_Orm):
    contact_info_id: int
    personal_email: str | None = None
    work_email: str | None = None
    phone: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None
    region: str | None = None


class CurrentCareerRead(_Orm):
    current_employment_id: int
    current_employer: str | None = None
    current_title: str | None = None
    current_industry: str | None = None
    current_industry_secondary: str | None = None
    current_city: str | None = None
    current_state: str | None = None
    current_country: str | None = None
    current_zip: str | None = None
    seniority_level: str | None = None
    last_verified_at: datetime.datetime | None = None


class EmploymentHistoryRead(_Orm):
    employment_history_id: int
    employer_name: str | None = None
    employment_title: str | None = None
    employment_industry: str | None = None
    city: str | None = None
    state: str | None = None
    start_year: int | None = None
    end_year: int | None = None
    is_current: bool


class EducationRead(_Orm):
    education_id: int
    university: str | None = None
    college: str | None = None
    department: str | None = None
    degree: str | None = None
    major: str | None = None
    degree_status: str | None = None
    degree_year: int | None = None


class LeadershipRead(_Orm):
    finance_society_leadership_id: int
    leadership_role: str
    role_year: int | None = None


class ProgramEngagementRead(_Orm):
    engagement_profile_id: int
    nettrek_host_willing: bool
    finance_conference_willing: bool
    mentor_willing: bool
    company_event_sponsor_willing: bool
    guest_speaker_willing: bool
    help_at_event_willing: bool
    case_competition_host_willing: bool
    women_in_finance_mentor_willing: bool
    hired_finance_intern: bool
    hired_finance_full_time: bool
    piff_donor: bool
    cfp_designation: bool
    cfa_designation: bool
    engagement_notes: str | None = None


class EngagementNoteRead(_Orm):
    engagement_id: int
    engagement_interest_type: str | None = None
    engagement_notes: str | None = None


class SurveyRead(_Orm):
    survey_id: int
    survey_year: int | None = None
    survey_due_date: datetime.date | None = None
    completed: bool
    completed_at: datetime.datetime | None = None
    survey_status: str | None = None
    survey_notes: str | None = None


class InteractionRead(_Orm):
    interaction_id: int
    interaction_type: str | None = None
    interaction_date_time: datetime.datetime | None = None
    interaction_notes: str | None = None
    # Internal user PK is never disclosed; only the resolved display name
    # ``logged_by`` leaves the API (FERPA — minimize internal identifiers).
    logged_by: str | None = None


class TaskRead(_Orm):
    follow_up_task_id: int
    task_title: str | None = None
    due_date: datetime.date | None = None
    completed: bool
    completed_at: datetime.datetime | None = None
    task_notes: str | None = None
    # Internal assignee PK is never disclosed; only the resolved display name
    # ``assigned_to`` leaves the API.
    assigned_to: str | None = None


class AdminTaskItem(_Orm):
    """A single follow-up task in the cross-alumni admin task list.

    Reuses the ``TaskRead`` task fields, plus the owning alumnus's id and display
    name so the row can deep-link to the profile. ``assigned_to`` is the
    resolved assignee display name (None when unassigned)."""

    follow_up_task_id: int
    alumni_id: int
    alumni_name: str | None = None
    task_title: str | None = None
    due_date: datetime.date | None = None
    completed: bool
    completed_at: datetime.datetime | None = None
    task_notes: str | None = None
    assigned_to_user_id: int | None = None
    assigned_to: str | None = None


class AdminTaskPage(BaseModel):
    """A page of cross-alumni follow-up tasks plus the pagination envelope."""

    items: list[AdminTaskItem]
    total: int
    limit: int
    offset: int


class AttachmentRead(_Orm):
    attachment_id: int
    file_name: str
    file_type: str | None = None
    attachment_notes: str | None = None
    uploaded_at: datetime.datetime
    uploaded_by_user_id: int | None = None


class EventAttendedRead(_Orm):
    event_id: int
    event_name: str
    event_type: str | None = None
    event_date: datetime.date | None = None
    event_location: str | None = None
    attendance_status: str | None = None


class AuditEntryRead(_Orm):
    audit_log_id: int
    action_type: str
    field_name: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    created_at: datetime.datetime
    user_id: int | None = None


class ProfileRead(BaseModel):
    """The full profile aggregate for one alumni."""

    alumni: AlumniRead
    # When alumni.spouse_alumni_id is set, the linked alumnus's current display
    # name — so the profile can render a deep-link with an up-to-date label even
    # if the stored spouse_first_name/last_name drift. None when not linked.
    spouse_alumni_name: str | None = None
    contact: ContactRead | None = None
    current_career: CurrentCareerRead | None = None
    employment_history: list[EmploymentHistoryRead] = []
    education: list[EducationRead] = []
    leadership: list[LeadershipRead] = []
    program_engagement: ProgramEngagementRead | None = None
    engagement_notes: list[EngagementNoteRead] = []
    tags: list[str] = []
    status_labels: list[str] = []
    surveys: list[SurveyRead] = []
    interactions: list[InteractionRead] = []
    interaction_count: int = 0
    tasks: list[TaskRead] = []
    attachments: list[AttachmentRead] = []
    events: list[EventAttendedRead] = []
    audit: list[AuditEntryRead] = []

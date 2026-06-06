"""Aggregate alumni-profile read schemas.

``ProfileRead`` is the payload behind ``GET /alumni/{id}/profile`` — the alumni
core plus every related collection the profile tabs render (Contact, Career,
Employment, Leadership, Engagement, Survey, Interactions, Tasks, Attachments).
Read-only; per-tab write endpoints are separate. All sections are optional so a
sparse record (most fields awaiting data) serializes cleanly.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.alumni import AlumniRead


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


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
    piff_donor_amount: Decimal | None = None
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
    user_id: int | None = None
    logged_by: str | None = None


class TaskRead(_Orm):
    follow_up_task_id: int
    task_title: str | None = None
    due_date: datetime.date | None = None
    completed: bool
    completed_at: datetime.datetime | None = None
    task_notes: str | None = None
    assigned_to_user_id: int | None = None
    assigned_to: str | None = None


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

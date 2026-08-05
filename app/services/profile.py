"""Alumni profile aggregation.

Assembles the read-only ``ProfileRead`` payload for one alumni: the core record
plus every related collection the profile tabs render. One call per related
table; for a single alumni the row counts are tiny, so this stays well within
the dashboard/profile performance budget.
"""

from __future__ import annotations

import contextlib
import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dropdowns import ENGAGEMENT_FLAG_TAGS, engagement_flag_for_tag
from app.core.errors import ConflictError, NotFoundError
from app.core.security import AuthorizationError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.crm import Attachment, FollowUpTask, Interaction, Survey
from app.models.donation import Donation
from app.models.employment import (
    CurrentEmployment,
    EducationHistory,
    EmploymentHistory,
)
from app.models.engagement import (
    AlumniEngagement,
    AlumniProgramEngagement,
    FinanceSocietyLeadership,
)
from app.models.event import Event, EventAttendance
from app.models.survey_reset import SurveyResetLog
from app.models.survey_response import SurveyResponse
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.models.tags import AlumniStatusLabel, AlumniTag, StatusLabel, Tag
from app.models.user import User
from app.schemas.alumni import AlumniRead, minimize_alumni_read
from app.schemas.profile import (
    AttachmentRead,
    AuditEntryRead,
    ContactRead,
    CurrentCareerRead,
    EducationCreate,
    EducationRead,
    EducationUpdate,
    EmploymentHistoryCreate,
    EmploymentHistoryRead,
    EmploymentHistoryUpdate,
    EngagementNoteRead,
    EventAttendanceCreate,
    EventAttendedRead,
    InteractionCreate,
    InteractionRead,
    InteractionUpdate,
    LeadershipCreate,
    LeadershipRead,
    LeadershipUpdate,
    PayItForwardSummary,
    ProfileRead,
    ProgramEngagementRead,
    StatusLabelCreate,
    SurveyRead,
    TagCreate,
    TaskCreate,
    TaskRead,
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _full_name(first: str | None, last: str | None, email: str | None) -> str | None:
    name = " ".join(p for p in (first, last) if p).strip()
    return name or email


async def _pay_it_forward_summary(
    session: AsyncSession, alumni_id: int, *, show_amounts: bool
) -> PayItForwardSummary:
    """Roll up an alumnus's Pay It Forward giving from the donations ledger (#403).

    Returns last-gift amount/date, lifetime total, and gift count. DOLLAR amounts
    are gated to amount-viewers (full_access+): when ``show_amounts`` is False the
    amount fields are ``None`` while the count and last-gift DATE remain — mirrors
    exactly how the donations endpoints null amounts for non-privileged callers.
    ``last_donation_date`` is month-level (day always 1; month defaults to January
    when only a year is recorded), matching the ledger's year + optional-month
    precision. A non-donor comes back as ``donation_count == 0`` with null fields."""
    rows = (
        await session.scalars(
            select(Donation)
            .where(Donation.alumni_id == alumni_id)
            .order_by(
                Donation.donation_year.desc(),
                Donation.donation_month.desc().nullslast(),
                Donation.donation_id.desc(),
            )
        )
    ).all()
    if not rows:
        return PayItForwardSummary(donation_count=0)

    latest = rows[0]
    last_date = datetime.date(latest.donation_year, latest.donation_month or 1, 1)
    lifetime = sum((d.amount for d in rows), Decimal(0))
    return PayItForwardSummary(
        last_donation_amount=float(latest.amount) if show_amounts else None,
        last_donation_date=last_date,
        total_lifetime_amount=float(lifetime) if show_amounts else None,
        donation_count=len(rows),
    )


async def _require_alumni(session: AsyncSession, alumni_id: int) -> Alumni:
    alumnus = await session.get(Alumni, alumni_id)
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    return alumnus


async def _actor_name(session: AsyncSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return _full_name(user.first_name, user.last_name, user.email) if user else None


def _require_interaction_ownership(
    interaction: Interaction, actor_user_id: int | None, can_edit_others: bool
) -> None:
    """Gate edit/delete of an interaction by ownership for non-edit-tier actors.

    Edit-tier roles (``can_edit_others=True``) may mutate any interaction. A
    view_only "Professor" (``can_edit_others=False``) may mutate only the
    interactions they logged themselves; anything else raises
    ``AuthorizationError`` (403). A row with no ``user_id`` (e.g. a legacy /
    system row) has no owner, so a non-edit-tier actor can never claim it."""
    if can_edit_others:
        return
    if actor_user_id is None or interaction.user_id != actor_user_id:
        raise AuthorizationError(
            "You can only edit or delete interactions you logged."
        )


# Synthetic `survey_id`s for derived history rows (#40). The rows aren't stored,
# but `SurveyRead.survey_id` is the list key, so each needs a stable, unique
# value. Real `surveys.survey_id`s are IDENTITY-generated and therefore >= 1, so
# 0 and negatives can never collide with a legacy row.
_OPEN_CYCLE_SURVEY_ID = 0
# Days from a campaign's start_date to the end of its 2-week reminder window,
# after which `survey_schedule.send_due` marks the campaign complete. Mirrors
# STAGE_REMINDER_2 * _STAGE_WINDOW_DAYS + _STAGE_WINDOW_DAYS in
# app/services/survey_schedule.py — the alum's effective deadline to reply.
_CAMPAIGN_WINDOW_DAYS = 21

_RESPONSE_STATUS_LABELS = {
    "applied": "Completed",
    "pending": "Completed - awaiting review",
    "rejected": "Completed - not applied",
}

def _as_utc(value: datetime.datetime) -> datetime.datetime:
    """Aware-UTC view of a timestamp, so a naive one from the DB driver cannot
    make a comparison raise instead of answering."""
    return value.replace(tzinfo=datetime.UTC) if value.tzinfo is None else value


_SEND_STAGE_LABELS = {
    0: "Survey sent",
    1: "1-week reminder sent",
    2: "2-week reminder sent",
}


async def _derive_survey_history(
    session: AsyncSession,
    alumni_id: int,
    graduation_year: int | None,
) -> list[SurveyRead]:
    """The alum's real survey history, derived from what actually happened.

    The `surveys` table this tab was built against has NO writer anywhere in the
    codebase — nothing has ever inserted a row — so the Surveys tab rendered
    empty for every alumnus. The truth now lives in the scheduler's tables, so
    read it from there rather than adding a second write path that could drift:

      * ``survey_responses`` — one row per cycle they actually answered.
      * ``survey_send_log``  — what we emailed them, and when (stage 0/1/2).
      * ``survey_schedule``  — their cohort's campaign, for the due date.

    Returns rows in the SAME ``SurveyRead`` shape the legacy table produced, so
    the profile tab renders them with no change.

    An engineer reset (#395) deletes none of this — it only stops those rows
    counting toward eligibility — so everything still renders. Responses that
    predate the alum's latest reset are labelled as belonging to a previous
    survey cycle, because "you answered this in a cycle we have since re-opened"
    is a real difference to whoever is reading the tab, and an unlabelled second
    answer to "the same" survey looks like a duplicate.
    """
    responses = list(
        (
            await session.scalars(
                select(SurveyResponse)
                .where(SurveyResponse.alumni_id == alumni_id)
                .order_by(SurveyResponse.submitted_at.desc())
            )
        ).all()
    )
    sends = list(
        (
            await session.scalars(
                select(SurveySendLog)
                .where(SurveySendLog.alumni_id == alumni_id)
                .order_by(SurveySendLog.sent_at)
            )
        ).all()
    )
    if not responses and not sends:
        return []

    # The alum's latest engineer reset, if any (#395). Everything submitted at or
    # before it belongs to a superseded cycle.
    last_reset_at = await session.scalar(
        select(func.max(SurveyResetLog.reset_at)).where(
            SurveyResetLog.alumni_id == alumni_id
        )
    )

    # The cohort campaign's deadline, when there is one to tie rows to.
    campaign_start: datetime.date | None = None
    if graduation_year is not None:
        campaign_start = await session.scalar(
            select(SurveySchedule.start_date).where(
                SurveySchedule.graduation_year == graduation_year
            )
        )
    campaign_due = (
        campaign_start + datetime.timedelta(days=_CAMPAIGN_WINDOW_DAYS) if campaign_start else None
    )

    rows: list[SurveyRead] = []
    for r in responses:
        submitted = r.submitted_at
        # Only claim the campaign's due date for a response that actually
        # answered it — an older response predating the campaign gets none
        # rather than borrowing a deadline it was never measured against.
        answered_campaign = campaign_start is not None and submitted.date() >= campaign_start
        field_count = len(r.payload or {})
        notes = f"{field_count} field{'' if field_count == 1 else 's'} submitted"
        if r.staged_photo_path:
            notes += " + photo"
        if last_reset_at is not None and _as_utc(last_reset_at) >= _as_utc(submitted):
            # Kept and shown — it IS something they submitted — but named as the
            # earlier cycle so it does not read as a stale duplicate of the
            # answer to the campaign now running.
            notes += " (previous survey cycle)"
        rows.append(
            SurveyRead(
                survey_id=-r.survey_response_id,
                survey_year=submitted.year,
                survey_due_date=campaign_due if answered_campaign else None,
                completed=True,
                completed_at=submitted,
                # `completed` stays True for every response, including rejected:
                # the alum DID answer, and the badge shouldn't imply otherwise.
                # Whether staff applied it is carried in the label instead.
                survey_status=_RESPONSE_STATUS_LABELS.get(r.status, "Completed"),
                survey_notes=notes,
            )
        )

    # An open cycle: we emailed them and nothing has come back since. One row at
    # most — `survey_send_log` is unique on (year, alumni, stage), so an alum has
    # a single campaign's worth of sends.
    if sends:
        first_sent = sends[0].sent_at
        last = sends[-1]
        answered = any(r.submitted_at >= first_sent for r in responses)
        if not answered:
            rows.append(
                SurveyRead(
                    survey_id=_OPEN_CYCLE_SURVEY_ID,
                    survey_year=(campaign_start or first_sent.date()).year,
                    survey_due_date=campaign_due,
                    completed=False,
                    completed_at=None,
                    # Left None on purpose: the UI derives "Overdue" vs
                    # "Pending" from the due date, so the badge stays right as
                    # the deadline passes without anything re-deriving here.
                    survey_status=None,
                    survey_notes=(
                        f"{_SEND_STAGE_LABELS.get(last.stage, 'Survey sent')}"
                        f" {last.sent_at.date().isoformat()} - no reply yet"
                    ),
                )
            )
    return rows


async def get_profile(
    session: AsyncSession,
    alumni_id: int,
    *,
    include_tasks: bool = True,
    include_archived: bool = False,
    can_edit: bool = True,
    show_pay_it_forward_amounts: bool = True,
    actor_user_id: int | None = None,
) -> ProfileRead:
    """Assemble the full profile aggregate for one alumnus.

    FERPA scoping:
      * Archived records 404 on direct read unless ``include_archived`` (a
        full_access edit flow). They were removed from the directory and must
        not resurface via this read.
      * ``can_edit=False`` (view_only "Professor") gets a MINIMIZED aggregate:
        sensitive PII/notes are nulled on the core record, free-text interaction
        / survey / engagement notes are stripped, and the embedded audit trail
        is omitted. Implemented in ``_minimize_profile_for_view_only``.
      * When ``actor_user_id`` is known, a single ``view_profile`` audit row is
        written for the disclosure (the profile aggregate is the sensitive read;
        the lightweight single-record GET is left unlogged to avoid noise).
    """
    alumnus = await session.get(Alumni, alumni_id)
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    if alumnus.archived and not include_archived:
        raise NotFoundError(f"Alumni {alumni_id} not found.")

    # Resolve the linked spouse's current display name (for the profile link
    # label). Prefer the preferred first name when present.
    spouse_alumni_name: str | None = None
    if alumnus.spouse_alumni_id is not None:
        spouse = await session.get(Alumni, alumnus.spouse_alumni_id)
        if spouse is not None:
            spouse_alumni_name = _full_name(
                spouse.preferred_first_name or spouse.first_name,
                spouse.last_name,
                None,
            )

    # Resolve the display name of the user who last manually updated this profile
    # ("Profile updated by ...") for the hover label. None when unset/unknown.
    profile_updated_by_name = await _actor_name(
        session, alumnus.profile_updated_by_user_id
    )

    contact = await session.scalar(
        select(AlumniContactInfo)
        .where(AlumniContactInfo.alumni_id == alumni_id)
        .order_by(AlumniContactInfo.contact_info_id)
        .limit(1)
    )
    career = await session.scalar(
        select(CurrentEmployment)
        .where(CurrentEmployment.alumni_id == alumni_id)
        .order_by(CurrentEmployment.current_employment_id.desc())
        .limit(1)
    )
    employment = (
        await session.scalars(
            select(EmploymentHistory)
            .where(EmploymentHistory.alumni_id == alumni_id)
            .order_by(
                EmploymentHistory.is_current.desc(),
                EmploymentHistory.end_year.desc().nullsfirst(),
                EmploymentHistory.start_year.desc().nullslast(),
            )
        )
    ).all()
    education = (
        await session.scalars(
            select(EducationHistory)
            .where(EducationHistory.alumni_id == alumni_id)
            .order_by(EducationHistory.degree_year.desc().nullslast())
        )
    ).all()
    leadership = (
        await session.scalars(
            select(FinanceSocietyLeadership)
            .where(FinanceSocietyLeadership.alumni_id == alumni_id)
            .order_by(FinanceSocietyLeadership.role_year.desc().nullslast())
        )
    ).all()
    program = await session.scalar(
        select(AlumniProgramEngagement).where(
            AlumniProgramEngagement.alumni_id == alumni_id
        )
    )
    engagement_notes = (
        await session.scalars(
            select(AlumniEngagement)
            .where(AlumniEngagement.alumni_id == alumni_id)
            .order_by(AlumniEngagement.engagement_id)
        )
    ).all()
    # Both tag stores, merged (#629) — ordinary `alumni_tags` rows plus the nine
    # "ways to get involved" derived from `program` above. Rendered in the
    # profile header, which every role sees, so answering "willing to mentor"
    # now shows up somewhere instead of nowhere.
    tags = sorted(
        {
            name
            for name in (
                await session.scalars(
                    select(Tag.tag_name)
                    .join(AlumniTag, AlumniTag.tag_id == Tag.tag_id)
                    .where(AlumniTag.alumni_id == alumni_id)
                )
            ).all()
            if engagement_flag_for_tag(name) is None
        }
        | set(_engagement_tag_names(program))
    )
    status_labels = (
        await session.scalars(
            select(StatusLabel.status_label_name)
            .join(
                AlumniStatusLabel,
                AlumniStatusLabel.status_label_id == StatusLabel.status_label_id,
            )
            .where(AlumniStatusLabel.alumni_id == alumni_id)
            .order_by(StatusLabel.status_label_name)
        )
    ).all()
    # Survey history (#40) = the LEGACY `surveys` rows plus rows derived from
    # what actually happened to this alum. See `_derive_survey_history`: the
    # legacy table has no writer anywhere in the codebase, so on its own the
    # Surveys tab was empty for everyone.
    legacy_surveys = (
        await session.scalars(
            select(Survey)
            .where(Survey.alumni_id == alumni_id)
            .order_by(Survey.survey_year.desc().nullslast())
        )
    ).all()
    surveys = [SurveyRead.model_validate(s) for s in legacy_surveys]
    surveys.extend(
        await _derive_survey_history(session, alumni_id, alumnus.graduation_year)
    )
    surveys.sort(
        key=lambda s: (
            s.survey_year or 0,
            s.completed_at.date() if s.completed_at else datetime.date.min,
        ),
        reverse=True,
    )
    # Cap the returned list to the 50 most recent; expose the true total
    # separately so the UI can show an accurate count without a huge payload.
    interaction_count = await session.scalar(
        select(func.count())
        .select_from(Interaction)
        .where(Interaction.alumni_id == alumni_id)
    )
    interactions = (
        await session.scalars(
            select(Interaction)
            .where(Interaction.alumni_id == alumni_id)
            .order_by(Interaction.interaction_date_time.desc().nullslast())
            .limit(50)
        )
    ).all()
    # Tasks are admin-only: skip the query entirely for view_only callers so the
    # data never leaves the API (the profile page also hides the panel for them).
    tasks = (
        (
            await session.scalars(
                select(FollowUpTask)
                .where(FollowUpTask.alumni_id == alumni_id)
                .order_by(
                    FollowUpTask.completed.asc(),
                    FollowUpTask.due_date.asc().nullslast(),
                )
            )
        ).all()
        if include_tasks
        else []
    )
    attachments = (
        await session.scalars(
            select(Attachment)
            .where(Attachment.alumni_id == alumni_id)
            .order_by(Attachment.uploaded_at.desc())
        )
    ).all()
    event_rows = (
        await session.execute(
            select(Event, EventAttendance.attendance_status)
            .join(EventAttendance, EventAttendance.event_id == Event.event_id)
            .where(EventAttendance.alumni_id == alumni_id)
            .order_by(Event.event_date.desc().nullslast())
        )
    ).all()
    audit = (
        await session.scalars(
            select(AuditLog)
            .where(AuditLog.entity_type == "alumni", AuditLog.entity_id == alumni_id)
            .order_by(AuditLog.created_at.desc())
            .limit(50)
        )
    ).all()

    # Resolve user display names for interactions/tasks/attachments in one
    # lookup (batched — no N+1). Audit rows carry their own ``actor_name`` /
    # ``actor_email`` snapshot (set by a DB trigger at insert time, so it
    # survives the actor's later deletion), so they need no join here.
    user_ids = (
        {i.user_id for i in interactions if i.user_id}
        | {t.assigned_to_user_id for t in tasks if t.assigned_to_user_id}
        | {a.uploaded_by_user_id for a in attachments if a.uploaded_by_user_id}
        # Legacy audit rows (pre-snapshot-trigger) have no actor_name/email; their
        # user_id is included so the fallback name lookup below can resolve them.
        | {a.user_id for a in audit if a.user_id}
    )
    names: dict[int, str | None] = {}
    # First names only, for the view_only redaction below. Intentionally NO email
    # fallback: a nameless account must surface as "—" to a view_only caller, not
    # leak an email address.
    first_names: dict[int, str | None] = {}
    if user_ids:
        for u in (
            await session.scalars(select(User).where(User.user_id.in_(user_ids)))
        ).all():
            names[u.user_id] = _full_name(u.first_name, u.last_name, u.email)
            first_names[u.user_id] = u.first_name or None

    pay_it_forward = await _pay_it_forward_summary(
        session, alumni_id, show_amounts=show_pay_it_forward_amounts
    )

    # Next scheduled survey send for this alum's graduation year (#364): the
    # initial send if it hasn't started yet, else the next reminder (+7 / +14
    # days) still in the future. Only a runnable schedule (scheduled/active)
    # counts; None once it's completed/cancelled or past its last reminder.
    next_survey_date: datetime.date | None = None
    if alumnus.graduation_year is not None:
        start_date = await session.scalar(
            select(SurveySchedule.start_date).where(
                SurveySchedule.graduation_year == alumnus.graduation_year,
                SurveySchedule.status.in_(("scheduled", "active")),
            )
        )
        if isinstance(start_date, datetime.date):
            today = datetime.datetime.now(datetime.UTC).date()
            if start_date >= today:
                next_survey_date = start_date
            else:
                for offset in (7, 14):
                    reminder = start_date + datetime.timedelta(days=offset)
                    if reminder >= today:
                        next_survey_date = reminder
                        break

    profile = ProfileRead(
        alumni=AlumniRead.model_validate(alumnus).model_copy(
            update={"profile_updated_by_name": profile_updated_by_name}
        ),
        spouse_alumni_name=spouse_alumni_name,
        contact=ContactRead.model_validate(contact) if contact else None,
        current_career=CurrentCareerRead.model_validate(career) if career else None,
        employment_history=[EmploymentHistoryRead.model_validate(e) for e in employment],
        education=[EducationRead.model_validate(e) for e in education],
        leadership=[LeadershipRead.model_validate(le) for le in leadership],
        program_engagement=(
            ProgramEngagementRead.model_validate(program) if program else None
        ),
        engagement_notes=[
            EngagementNoteRead.model_validate(en) for en in engagement_notes
        ],
        tags=list(tags),
        status_labels=list(status_labels),
        surveys=surveys,
        next_survey_date=next_survey_date,
        interactions=[
            InteractionRead.model_validate(i).model_copy(
                # full_access/student see the logger's full name; view_only sees
                # the first name only (recomputed from source, no email fallback).
                update={
                    "logged_by": (
                        (names if can_edit else first_names).get(i.user_id)
                        if i.user_id
                        else None
                    )
                }
            )
            for i in interactions
        ],
        interaction_count=int(interaction_count or 0),
        tasks=[
            TaskRead.model_validate(t).model_copy(
                update={
                    "assigned_to": (
                        names.get(t.assigned_to_user_id)
                        if t.assigned_to_user_id
                        else None
                    )
                }
            )
            for t in tasks
        ],
        attachments=[
            AttachmentRead.model_validate(a).model_copy(
                update={
                    "uploaded_by": (
                        names.get(a.uploaded_by_user_id)
                        if a.uploaded_by_user_id
                        else None
                    )
                }
            )
            for a in attachments
        ],
        events=[
            EventAttendedRead.model_validate(ev).model_copy(
                update={"attendance_status": status}
            )
            for ev, status in event_rows
        ],
        audit=[
            AuditEntryRead.model_validate(a).model_copy(
                # Prefer the snapshotted actor name/email (survives the actor's
                # deletion); fall back to a live name lookup for legacy rows
                # written before the snapshot trigger existed.
                update={
                    "performed_by": (
                        a.actor_name
                        or a.actor_email
                        or (names.get(a.user_id) if a.user_id else None)
                    )
                }
            )
            for a in audit
        ],
        pay_it_forward=pay_it_forward,
    )

    if not can_edit:
        profile = _minimize_profile_for_view_only(profile)

    # Disclosure logging: a single audit row records that the actor viewed this
    # alumnus's full profile. The payload itself is never stored — only the
    # action + actor + entity. No-op when the actor is unknown.
    if actor_user_id is not None:
        # Best-effort: disclosure logging must never break the read itself.
        try:
            _audit_alumni(session, actor_user_id, "view_profile", alumni_id)
            await session.commit()
        except Exception:  # noqa: BLE001 - audit is best-effort
            with contextlib.suppress(Exception):
                await session.rollback()

    return profile


async def export_profile(
    session: AsyncSession, alumni_id: int, *, actor_user_id: int | None
) -> dict:
    """Server-side, audited export of one alumnus's profile (full_access).

    Returns the full profile aggregate as a MINIMIZED JSON-able dict:
      * the embedded ``audit`` trail is excluded entirely, and
      * internal user PKs are never present (``InteractionRead`` /
        ``TaskRead`` no longer carry ``user_id`` / ``assigned_to_user_id``).
    Writes an ``export_profile`` audit row BEFORE returning so every export is
    attributable. Archived records 404.

    This is the contract the frontend calls instead of doing a client-side
    export: ``GET /alumni/{id}/export`` -> JSON body of the minimized profile.
    """
    # Build the full aggregate (full_access caller -> can_edit=True, so no
    # view_only field-stripping; we do our own minimization below). No view
    # audit here — the export audit row is the disclosure record.
    profile = await get_profile(
        session, alumni_id, include_tasks=True, can_edit=True
    )
    # Drop the audit trail; internal user PKs are already absent from the read
    # schemas. exclude is belt-and-suspenders for the PK fields.
    data = profile.model_dump(
        mode="json",
        exclude={
            "audit": True,
            "interactions": {"__all__": {"user_id"}},
            "tasks": {"__all__": {"assigned_to_user_id"}},
        },
    )

    if actor_user_id is not None:
        try:
            _audit_alumni(session, actor_user_id, "export_profile", alumni_id)
            await session.commit()
        except Exception:  # noqa: BLE001 - audit is best-effort
            with contextlib.suppress(Exception):
                await session.rollback()
    return data


def _minimize_profile_for_view_only(profile: ProfileRead) -> ProfileRead:
    """Strip the FERPA-sensitive parts of a profile for a ``view_only`` caller.

    Nulls the sensitive core PII (via ``minimize_alumni_read``), redacts the
    home MAILING ADDRESS (street lines + ZIP — city/state/region/country stay,
    as directory-style location), strips all free-text notes (interaction /
    survey / engagement / program-engagement notes), and omits the embedded
    audit trail entirely. Returns a new ``ProfileRead``; the input is left
    untouched.

    Contact reachability (#166 — INTENTIONAL PRODUCT DECISION): personal/work
    email and phone are DELIBERATELY exposed to view_only so a "Professor" can
    reach out to alumni for the outreach use-case. This reverses the earlier
    full-redaction audit (which hid email/phone too); only the physical home
    address (street + ZIP) stays protected here.

    Note: ``logged_by`` is already reduced to the logger's FIRST NAME upstream in
    ``get_profile`` (full name only for editors), so it is intentionally left
    untouched here — a view_only caller sees who made contact by first name.
    """
    # Redact the home mailing address (street + ZIP) for view_only; keep
    # city/state/region/country (directory-style location). Email and phone are
    # INTENTIONALLY left visible (#166) so view_only can contact alumni for
    # outreach — do NOT re-add personal_email/work_email/phone here.
    contact = (
        profile.contact.model_copy(
            update={
                "address_line_1": None,
                "address_line_2": None,
                "zip": None,
                # best_contact holds a raw phone-or-email value straight off the
                # intake sheet — which may be a HOME number the address redaction
                # above is meant to withhold. The frontend never renders it at
                # all (it only round-trips through the edit form and CSV export),
                # so nulling it here costs view_only nothing and keeps the
                # unreviewed free text out of the payload.
                "best_contact": None,
            }
        )
        if profile.contact is not None
        else None
    )
    # Drop interaction notes only; logged_by is already first-name for view_only.
    interactions = [
        i.model_copy(update={"interaction_notes": None})
        for i in profile.interactions
    ]
    surveys = [s.model_copy(update={"survey_notes": None}) for s in profile.surveys]
    engagement_notes = [
        en.model_copy(update={"engagement_notes": None})
        for en in profile.engagement_notes
    ]
    program_engagement = (
        profile.program_engagement.model_copy(update={"engagement_notes": None})
        if profile.program_engagement is not None
        else None
    )
    return profile.model_copy(
        update={
            "alumni": minimize_alumni_read(profile.alumni, can_edit=False),
            # Resolved name of the linked spouse's own record — spouse PII, so
            # redact it for view_only (the raw spouse_first/last are already
            # nulled via VIEW_ONLY_HIDDEN_FIELDS + spouse_alumni_id).
            "spouse_alumni_name": None,
            "contact": contact,
            "interactions": interactions,
            "surveys": surveys,
            "engagement_notes": engagement_notes,
            "program_engagement": program_engagement,
            # Drop the audit trail entirely for view_only (old/new values can
            # echo sensitive prior data).
            "audit": [],
        }
    )


def _audit_alumni(
    session: AsyncSession,
    actor_user_id: int | None,
    action: str,
    alumni_id: int,
    *,
    field_name: str | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    """Record a profile-activity audit event against the alumni entity, so it
    surfaces in the profile Audit tab. No-op when the actor is unknown.

    The optional field/old/new args capture WHAT changed (used by interaction
    edit/delete so the FERPA trail can reconstruct altered/removed relationship
    notes, not just that a change happened)."""
    if actor_user_id is not None:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type=action,
                entity_type="alumni",
                entity_id=alumni_id,
                field_name=field_name,
                old_value=old_value,
                new_value=new_value,
            )
        )


def _audit_str(value: object) -> str | None:
    """Stringify a value for an audit old/new column (None stays None)."""
    return None if value is None else str(value)


async def add_interaction(
    session: AsyncSession,
    alumni_id: int,
    payload: InteractionCreate,
    actor_user_id: int | None,
) -> InteractionRead:
    await _require_alumni(session, alumni_id)
    interaction = Interaction(
        alumni_id=alumni_id,
        user_id=actor_user_id,
        interaction_type=payload.interaction_type,
        interaction_date_time=payload.interaction_date_time or _now(),
        interaction_notes=payload.interaction_notes,
    )
    session.add(interaction)
    _audit_alumni(session, actor_user_id, "add_interaction", alumni_id)
    await session.commit()
    await session.refresh(interaction)
    return InteractionRead.model_validate(interaction).model_copy(
        update={"logged_by": await _actor_name(session, interaction.user_id)}
    )


async def update_interaction(
    session: AsyncSession,
    alumni_id: int,
    interaction_id: int,
    payload: InteractionUpdate,
    actor_user_id: int | None,
    can_edit_others: bool = True,
) -> InteractionRead:
    """Edit an interaction on an alumni's timeline.

    404 if the row is missing or belongs to a different alumnus.

    ``can_edit_others`` is the actor's edit-tier flag (engineer / super_admin /
    full_access / student). When False (a view_only "Professor"), the actor may
    edit only an interaction they logged themselves; editing another user's row
    raises ``AuthorizationError`` (403). The ownership check runs AFTER the
    existence/parent check so it never reveals whether some other alumnus's
    interaction exists."""
    row = await session.get(Interaction, interaction_id)
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(f"Interaction {interaction_id} not found.")
    _require_interaction_ownership(row, actor_user_id, can_edit_others)
    data = payload.model_dump(exclude_unset=True)
    # Audit each field that actually changes with its old + new value, so a later
    # FERPA review can see exactly what a note edit altered — not just that an
    # edit occurred. A true no-op writes no audit row.
    for field, value in data.items():
        old = getattr(row, field)
        if old == value:
            continue
        setattr(row, field, value)
        _audit_alumni(
            session,
            actor_user_id,
            "update_interaction",
            alumni_id,
            field_name=field,
            old_value=_audit_str(old),
            new_value=_audit_str(value),
        )
    await session.commit()
    await session.refresh(row)
    return InteractionRead.model_validate(row).model_copy(
        update={"logged_by": await _actor_name(session, row.user_id)}
    )


async def delete_interaction(
    session: AsyncSession,
    alumni_id: int,
    interaction_id: int,
    actor_user_id: int | None,
    can_edit_others: bool = True,
) -> None:
    """Delete an interaction from an alumni's timeline.

    404 if the row is missing or belongs to a different alumnus.

    ``can_edit_others`` is the actor's edit-tier flag (engineer / super_admin /
    full_access / student). When False (a view_only "Professor"), the actor may
    delete only an interaction they logged themselves; deleting another user's
    row raises ``AuthorizationError`` (403). The ownership check runs AFTER the
    existence/parent check so it never reveals whether some other alumnus's
    interaction exists."""
    row = await session.get(Interaction, interaction_id)
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(f"Interaction {interaction_id} not found.")
    _require_interaction_ownership(row, actor_user_id, can_edit_others)
    # Snapshot the content BEFORE deleting so the FERPA trail retains what was
    # removed (a hard delete otherwise loses the note text irrecoverably).
    snapshot = (
        f"type={row.interaction_type!r}; "
        f"when={row.interaction_date_time}; "
        f"notes={row.interaction_notes!r}"
    )
    await session.delete(row)
    _audit_alumni(
        session,
        actor_user_id,
        "delete_interaction",
        alumni_id,
        field_name="interaction",
        old_value=snapshot,
    )
    await session.commit()


async def add_task(
    session: AsyncSession,
    alumni_id: int,
    payload: TaskCreate,
    actor_user_id: int | None,
) -> TaskRead:
    await _require_alumni(session, alumni_id)
    task = FollowUpTask(
        alumni_id=alumni_id,
        assigned_to_user_id=actor_user_id,
        task_title=payload.task_title,
        due_date=payload.due_date,
        task_notes=payload.task_notes,
    )
    session.add(task)
    _audit_alumni(session, actor_user_id, "add_task", alumni_id)
    await session.commit()
    await session.refresh(task)
    return TaskRead.model_validate(task).model_copy(
        update={"assigned_to": await _actor_name(session, task.assigned_to_user_id)}
    )


async def set_task_completed(
    session: AsyncSession,
    alumni_id: int,
    task_id: int,
    completed: bool,
    actor_user_id: int | None,
) -> TaskRead:
    task = await session.get(FollowUpTask, task_id)
    if task is None or task.alumni_id != alumni_id:
        raise NotFoundError(f"Task {task_id} not found.")
    if task.completed != completed:
        task.completed = completed
        task.completed_at = _now() if completed else None
        _audit_alumni(
            session,
            actor_user_id,
            "complete_task" if completed else "reopen_task",
            alumni_id,
        )
        await session.commit()
        await session.refresh(task)
    return TaskRead.model_validate(task).model_copy(
        update={"assigned_to": await _actor_name(session, task.assigned_to_user_id)}
    )


async def add_employment(
    session: AsyncSession,
    alumni_id: int,
    payload: EmploymentHistoryCreate,
    actor_user_id: int | None,
) -> EmploymentHistoryRead:
    """Insert a prior role into an alumnus's employment history (full_access)."""
    await _require_alumni(session, alumni_id)
    row = EmploymentHistory(
        alumni_id=alumni_id,
        employer_name=payload.employer_name,
        employment_title=payload.employment_title,
        employment_industry=payload.employment_industry,
        city=payload.city,
        state=payload.state,
        start_year=payload.start_year,
        end_year=payload.end_year,
        is_current=payload.is_current,
    )
    session.add(row)
    _audit_alumni(session, actor_user_id, "add_employment", alumni_id)
    await session.commit()
    await session.refresh(row)
    return EmploymentHistoryRead.model_validate(row)


async def update_employment(
    session: AsyncSession,
    alumni_id: int,
    employment_history_id: int,
    payload: EmploymentHistoryUpdate,
    actor_user_id: int | None,
) -> EmploymentHistoryRead:
    """Edit a prior role on an alumnus's employment history (full_access).

    404 if the row is missing or belongs to a different alumnus."""
    row = await session.get(EmploymentHistory, employment_history_id)
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(
            f"Employment history {employment_history_id} not found."
        )
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    _audit_alumni(session, actor_user_id, "update_employment", alumni_id)
    await session.commit()
    await session.refresh(row)
    return EmploymentHistoryRead.model_validate(row)


async def delete_employment(
    session: AsyncSession,
    alumni_id: int,
    employment_history_id: int,
    actor_user_id: int | None,
) -> None:
    """Delete a prior role from an alumnus's employment history (full_access).

    404 if the row is missing or belongs to a different alumnus."""
    row = await session.get(EmploymentHistory, employment_history_id)
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(
            f"Employment history {employment_history_id} not found."
        )
    await session.delete(row)
    _audit_alumni(session, actor_user_id, "delete_employment", alumni_id)
    await session.commit()


async def add_education(
    session: AsyncSession,
    alumni_id: int,
    payload: EducationCreate,
    actor_user_id: int | None,
) -> EducationRead:
    """Add an education entry to an alumnus's record (full_access)."""
    await _require_alumni(session, alumni_id)
    row = EducationHistory(
        alumni_id=alumni_id,
        university=payload.university,
        college=payload.college,
        department=payload.department,
        degree=payload.degree,
        major=payload.major,
        degree_status=payload.degree_status,
        degree_year=payload.degree_year,
    )
    session.add(row)
    _audit_alumni(session, actor_user_id, "add_education", alumni_id)
    await session.commit()
    await session.refresh(row)
    return EducationRead.model_validate(row)


async def update_education(
    session: AsyncSession,
    alumni_id: int,
    education_id: int,
    payload: EducationUpdate,
    actor_user_id: int | None,
) -> EducationRead:
    """Edit an education entry on an alumnus's record (full_access).

    404 if the row is missing or belongs to a different alumnus."""
    row = await session.get(EducationHistory, education_id)
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(f"Education {education_id} not found.")
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    _audit_alumni(session, actor_user_id, "update_education", alumni_id)
    await session.commit()
    await session.refresh(row)
    return EducationRead.model_validate(row)


async def delete_education(
    session: AsyncSession,
    alumni_id: int,
    education_id: int,
    actor_user_id: int | None,
) -> None:
    """Delete an education entry from an alumnus's record (full_access).

    404 if the row is missing or belongs to a different alumnus."""
    row = await session.get(EducationHistory, education_id)
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(f"Education {education_id} not found.")
    await session.delete(row)
    _audit_alumni(session, actor_user_id, "delete_education", alumni_id)
    await session.commit()


async def add_leadership(
    session: AsyncSession,
    alumni_id: int,
    payload: LeadershipCreate,
    actor_user_id: int | None,
) -> LeadershipRead:
    """Add a Finance Society leadership entry to an alumnus (full_access)."""
    await _require_alumni(session, alumni_id)
    row = FinanceSocietyLeadership(
        alumni_id=alumni_id,
        leadership_role=payload.leadership_role,
        role_year=payload.role_year,
    )
    session.add(row)
    _audit_alumni(session, actor_user_id, "add_leadership", alumni_id)
    await session.commit()
    await session.refresh(row)
    return LeadershipRead.model_validate(row)


async def update_leadership(
    session: AsyncSession,
    alumni_id: int,
    finance_society_leadership_id: int,
    payload: LeadershipUpdate,
    actor_user_id: int | None,
) -> LeadershipRead:
    """Edit a Finance Society leadership entry (full_access).

    404 if the row is missing or belongs to a different alumnus."""
    row = await session.get(
        FinanceSocietyLeadership, finance_society_leadership_id
    )
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(
            f"Leadership {finance_society_leadership_id} not found."
        )
    data = payload.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(row, field, value)
    _audit_alumni(session, actor_user_id, "update_leadership", alumni_id)
    await session.commit()
    await session.refresh(row)
    return LeadershipRead.model_validate(row)


async def delete_leadership(
    session: AsyncSession,
    alumni_id: int,
    finance_society_leadership_id: int,
    actor_user_id: int | None,
) -> None:
    """Delete a Finance Society leadership entry (full_access).

    404 if the row is missing or belongs to a different alumnus."""
    row = await session.get(
        FinanceSocietyLeadership, finance_society_leadership_id
    )
    if row is None or row.alumni_id != alumni_id:
        raise NotFoundError(
            f"Leadership {finance_society_leadership_id} not found."
        )
    await session.delete(row)
    _audit_alumni(session, actor_user_id, "delete_leadership", alumni_id)
    await session.commit()


async def _tag_names(session: AsyncSession, alumni_id: int) -> list[str]:
    """Every tag on an alumnus, from BOTH stores, as one sorted list (#629).

    The nine "ways to get involved" are read off their
    ``alumni_program_engagement`` boolean; every other tag is read from
    ``alumni_tags``. This is the single definition of "what tags does this
    person have" — the profile read, the add/remove responses and anything
    else all go through it, so a chip can never appear that the `tag=` filter
    would not also match.

    A leftover ``alumni_tags`` row for one of the nine (a hand-applied "Mentor"
    predating #629) is deliberately IGNORED here rather than shown, because the
    `tag=` filter ignores it too. Showing it would put a chip on a profile that
    search cannot find, and would keep an alumnus who answered NO looking like
    a mentor forever. The backfill migration turns those rows into flags first,
    so nobody loses a tag by this.
    """
    rows = (
        await session.scalars(
            select(Tag.tag_name)
            .join(AlumniTag, AlumniTag.tag_id == Tag.tag_id)
            .where(AlumniTag.alumni_id == alumni_id)
            .order_by(Tag.tag_name)
        )
    ).all()
    names = {name for name in rows if engagement_flag_for_tag(name) is None}
    program = await session.scalar(
        select(AlumniProgramEngagement).where(
            AlumniProgramEngagement.alumni_id == alumni_id
        )
    )
    names.update(_engagement_tag_names(program))
    return sorted(names)


def _engagement_tag_names(
    program: AlumniProgramEngagement | None,
) -> list[str]:
    """The subset of the nine involvement tags whose flag is set on *program*."""
    if program is None:
        return []
    return [
        tag_name
        for tag_name, column in ENGAGEMENT_FLAG_TAGS.items()
        if getattr(program, column, False)
    ]


async def _get_or_create_program_engagement(
    session: AsyncSession, alumni_id: int
) -> AlumniProgramEngagement:
    """The alumnus's engagement row, created empty if they have none yet.

    An alumnus who has never been surveyed and never been edited has no
    ``alumni_program_engagement`` row at all, so applying an involvement tag by
    hand has to be able to make one. Every column is NOT NULL with a false
    server default, so an empty row is a valid "no to everything"."""
    program = await session.scalar(
        select(AlumniProgramEngagement).where(
            AlumniProgramEngagement.alumni_id == alumni_id
        )
    )
    if program is None:
        program = AlumniProgramEngagement(alumni_id=alumni_id)
        session.add(program)
        await session.flush()
    return program


async def _get_or_create_tag(session: AsyncSession, name: str) -> Tag:
    tag = await session.scalar(select(Tag).where(Tag.tag_name == name))
    if tag is None:
        tag = Tag(tag_name=name)
        session.add(tag)
        await session.flush()
    return tag


async def add_tag(
    session: AsyncSession,
    alumni_id: int,
    payload: TagCreate,
    actor_user_id: int | None,
) -> list[str]:
    """Attach a canonical tag to an alumnus; return the resulting tag list.

    Idempotent: re-adding an existing tag is a no-op (existence-checked against
    the unique constraint), never a 500.

    One of the nine involvement tags (#629) SETS ITS ENGAGEMENT FLAG rather than
    inserting an ``alumni_tags`` row, because the flag is that tag's only store.
    That is what makes hand-applying "Mentor" and answering "willing to mentor
    students" on the survey land in the same place, and therefore find the same
    people when someone filters for mentors."""
    await _require_alumni(session, alumni_id)
    column = engagement_flag_for_tag(payload.tag)
    if column is not None:
        program = await _get_or_create_program_engagement(session, alumni_id)
        if not getattr(program, column):
            setattr(program, column, True)
            _audit_alumni(session, actor_user_id, "add_tag", alumni_id)
            await session.commit()
        return await _tag_names(session, alumni_id)
    tag = await _get_or_create_tag(session, payload.tag)
    existing = await session.scalar(
        select(AlumniTag.alumni_tag_id).where(
            AlumniTag.alumni_id == alumni_id, AlumniTag.tag_id == tag.tag_id
        )
    )
    if existing is None:
        session.add(AlumniTag(alumni_id=alumni_id, tag_id=tag.tag_id))
        _audit_alumni(session, actor_user_id, "add_tag", alumni_id)
        await session.commit()
    return await _tag_names(session, alumni_id)


async def remove_tag(
    session: AsyncSession,
    alumni_id: int,
    tag_name: str,
    actor_user_id: int | None,
) -> list[str]:
    """Detach a tag from an alumnus; return the resulting tag list. 404 if the
    alumnus doesn't have that tag.

    For one of the nine involvement tags (#629) this CLEARS THE ENGAGEMENT FLAG,
    and also deletes any leftover pre-#629 ``alumni_tags`` row of the same name
    so "remove this tag" cannot leave a half of it behind. This is the same
    operation an alum performs by answering NO on next year's survey, which is
    the point: withdrawal has one implementation, not two."""
    await _require_alumni(session, alumni_id)
    column = engagement_flag_for_tag(tag_name)
    if column is not None:
        program = await session.scalar(
            select(AlumniProgramEngagement).where(
                AlumniProgramEngagement.alumni_id == alumni_id
            )
        )
        if program is None or not getattr(program, column):
            raise NotFoundError(f"Tag '{tag_name}' is not set on alumni {alumni_id}.")
        setattr(program, column, False)
        stale = await session.scalar(
            select(AlumniTag)
            .join(Tag, Tag.tag_id == AlumniTag.tag_id)
            .where(AlumniTag.alumni_id == alumni_id, Tag.tag_name == tag_name)
        )
        if stale is not None:
            await session.delete(stale)
        _audit_alumni(session, actor_user_id, "remove_tag", alumni_id)
        await session.commit()
        return await _tag_names(session, alumni_id)
    assoc = await session.scalar(
        select(AlumniTag)
        .join(Tag, Tag.tag_id == AlumniTag.tag_id)
        .where(AlumniTag.alumni_id == alumni_id, Tag.tag_name == tag_name)
    )
    if assoc is None:
        raise NotFoundError(f"Tag '{tag_name}' is not set on alumni {alumni_id}.")
    await session.delete(assoc)
    _audit_alumni(session, actor_user_id, "remove_tag", alumni_id)
    await session.commit()
    return await _tag_names(session, alumni_id)


async def _status_label_names(session: AsyncSession, alumni_id: int) -> list[str]:
    return list(
        (
            await session.scalars(
                select(StatusLabel.status_label_name)
                .join(
                    AlumniStatusLabel,
                    AlumniStatusLabel.status_label_id == StatusLabel.status_label_id,
                )
                .where(AlumniStatusLabel.alumni_id == alumni_id)
                .order_by(StatusLabel.status_label_name)
            )
        ).all()
    )


async def _get_or_create_status_label(
    session: AsyncSession, name: str
) -> StatusLabel:
    label = await session.scalar(
        select(StatusLabel).where(StatusLabel.status_label_name == name)
    )
    if label is None:
        label = StatusLabel(status_label_name=name)
        session.add(label)
        await session.flush()
    return label


async def add_status_label(
    session: AsyncSession,
    alumni_id: int,
    payload: StatusLabelCreate,
    actor_user_id: int | None,
) -> list[str]:
    """Attach a canonical status label to an alumnus; return the resulting list.
    Idempotent (existence-checked)."""
    await _require_alumni(session, alumni_id)
    label = await _get_or_create_status_label(session, payload.label)
    existing = await session.scalar(
        select(AlumniStatusLabel.alumni_status_label_id).where(
            AlumniStatusLabel.alumni_id == alumni_id,
            AlumniStatusLabel.status_label_id == label.status_label_id,
        )
    )
    if existing is None:
        session.add(
            AlumniStatusLabel(
                alumni_id=alumni_id, status_label_id=label.status_label_id
            )
        )
        _audit_alumni(session, actor_user_id, "add_status_label", alumni_id)
        await session.commit()
    return await _status_label_names(session, alumni_id)


async def remove_status_label(
    session: AsyncSession,
    alumni_id: int,
    label_name: str,
    actor_user_id: int | None,
) -> list[str]:
    """Detach a status label from an alumnus; return the resulting list. 404 if
    the alumnus doesn't have that label."""
    await _require_alumni(session, alumni_id)
    assoc = await session.scalar(
        select(AlumniStatusLabel)
        .join(
            StatusLabel,
            StatusLabel.status_label_id == AlumniStatusLabel.status_label_id,
        )
        .where(
            AlumniStatusLabel.alumni_id == alumni_id,
            StatusLabel.status_label_name == label_name,
        )
    )
    if assoc is None:
        raise NotFoundError(
            f"Status label '{label_name}' is not set on alumni {alumni_id}."
        )
    await session.delete(assoc)
    _audit_alumni(session, actor_user_id, "remove_status_label", alumni_id)
    await session.commit()
    return await _status_label_names(session, alumni_id)


async def add_event_attendance(
    session: AsyncSession,
    alumni_id: int,
    payload: EventAttendanceCreate,
    actor_user_id: int | None,
) -> EventAttendedRead:
    """Mark an alumnus as an attendee of an existing event (full_access).

    404 if the event or alumnus is unknown. Respects the unique
    (event_id, alumni_id) constraint: a duplicate raises ConflictError (409),
    never a 500."""
    await _require_alumni(session, alumni_id)
    event = await session.get(Event, payload.event_id)
    if event is None:
        raise NotFoundError(f"Event {payload.event_id} not found.")

    existing = await session.scalar(
        select(EventAttendance).where(
            EventAttendance.event_id == payload.event_id,
            EventAttendance.alumni_id == alumni_id,
        )
    )
    if existing is not None:
        raise ConflictError(
            f"Alumni {alumni_id} is already an attendee of event "
            f"{payload.event_id}."
        )

    session.add(
        EventAttendance(
            event_id=payload.event_id,
            alumni_id=alumni_id,
            attendance_status=payload.attendance_status,
        )
    )
    _audit_alumni(session, actor_user_id, "add_event_attendance", alumni_id)
    await session.commit()
    return EventAttendedRead.model_validate(event).model_copy(
        update={"attendance_status": payload.attendance_status}
    )

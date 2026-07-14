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
    tags = (
        await session.scalars(
            select(Tag.tag_name)
            .join(AlumniTag, AlumniTag.tag_id == Tag.tag_id)
            .where(AlumniTag.alumni_id == alumni_id)
            .order_by(Tag.tag_name)
        )
    ).all()
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
    surveys = (
        await session.scalars(
            select(Survey)
            .where(Survey.alumni_id == alumni_id)
            .order_by(Survey.survey_year.desc().nullslast())
        )
    ).all()
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
        surveys=[SurveyRead.model_validate(s) for s in surveys],
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
                # best_contact holds the raw home/best phone-or-email value; the
                # frontend hides it from view_only, so null it server-side too.
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
    return list(
        (
            await session.scalars(
                select(Tag.tag_name)
                .join(AlumniTag, AlumniTag.tag_id == Tag.tag_id)
                .where(AlumniTag.alumni_id == alumni_id)
                .order_by(Tag.tag_name)
            )
        ).all()
    )


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
    the unique constraint), never a 500."""
    await _require_alumni(session, alumni_id)
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
    alumnus doesn't have that tag."""
    await _require_alumni(session, alumni_id)
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

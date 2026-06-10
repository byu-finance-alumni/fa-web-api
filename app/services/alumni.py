"""Alumni business logic.

Owns the rules that aren't just data access:
  * soft-delete — ``archived`` is flipped, rows are never hard-deleted
  * manual-edit provenance — any client edit stamps ``manually_edited_at`` so
    later imports won't clobber it (manual edits win)
"""

import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment, EducationHistory
from app.models.engagement import AlumniProgramEngagement
from app.repositories import alumni as repo
from app.schemas.alumni import AlumniCreateFull, AlumniUpdateFull


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _audit(
    session: AsyncSession,
    actor_user_id: int | None,
    action: str,
    alumni_id: int,
    *,
    field_name: str | None = None,
    old_value: object | None = None,
    new_value: object | None = None,
) -> None:
    """Record an alumni audit event when an acting user is known.

    For field-level changes (updates), ``field_name`` + old/new values are
    captured so the audit doubles as version history. Values are stringified to
    fit the ``text`` audit columns; ``None`` stays ``NULL``.
    """
    if actor_user_id is not None:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type=action,
                entity_type="alumni",
                entity_id=alumni_id,
                field_name=field_name,
                old_value=None if old_value is None else str(old_value),
                new_value=None if new_value is None else str(new_value),
            )
        )


async def _validate_spouse_link(
    session: AsyncSession,
    spouse_alumni_id: int | None,
    *,
    self_id: int | None = None,
) -> None:
    """Guard the spouse self-link before it hits the DB constraints.

    A clean 404/409 here beats letting the FK / CHECK constraint surface as a
    500. ``self_id`` is the editing alumnus's own id (None on create, where the
    record doesn't exist yet so self-linking is impossible).
    """
    if spouse_alumni_id is None:
        return
    if self_id is not None and spouse_alumni_id == self_id:
        raise ConflictError("An alumnus cannot be linked as their own spouse.")
    exists = await session.scalar(
        select(Alumni.alumni_id).where(Alumni.alumni_id == spouse_alumni_id)
    )
    if exists is None:
        raise NotFoundError(f"Spouse alumni {spouse_alumni_id} not found.")


async def get_alumni(session: AsyncSession, alumni_id: int) -> Alumni:
    alumnus = await repo.get(session, alumni_id)
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    return alumnus


async def list_alumni(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    **filters: object,
) -> tuple[list[Alumni], int]:
    """List alumni with pagination. ``filters`` are forwarded to the repository
    (see ``build_alumni_query``: q, graduation_year, grad_year_min/max,
    deceased, include_archived)."""
    return await repo.list_page(session, limit=limit, offset=offset, **filters)


async def create_alumni(
    session: AsyncSession,
    payload: AlumniCreateFull,
    actor_user_id: int | None = None,
) -> Alumni:
    # Core columns only — nested sections are popped off before constructing the
    # Alumni row (they map to related tables, not the alumni table).
    core = payload.model_dump(
        exclude_unset=True,
        exclude={"contact", "career", "education", "engagement"},
    )
    await _validate_spouse_link(session, core.get("spouse_alumni_id"))
    alumnus = Alumni(**core)
    session.add(alumnus)
    # Need the generated alumni_id to attach the related rows; flush gets it
    # without committing, keeping everything in one transaction.
    await session.flush()
    if actor_user_id is not None:
        _audit(session, actor_user_id, "create", alumnus.alumni_id)

    # Insert each related section only when it carries at least one real value,
    # so untouched sections never create empty rows. getattr keeps this tolerant
    # of a core-only payload (e.g. direct service callers / tests).
    contact = getattr(payload, "contact", None)
    career = getattr(payload, "career", None)
    education = getattr(payload, "education", None)
    engagement = getattr(payload, "engagement", None)
    if contact is not None and contact.has_values():
        session.add(
            AlumniContactInfo(alumni_id=alumnus.alumni_id, **contact.model_dump())
        )
    if career is not None and career.has_values():
        session.add(
            CurrentEmployment(alumni_id=alumnus.alumni_id, **career.model_dump())
        )
    if education is not None and education.has_values():
        session.add(
            EducationHistory(
                alumni_id=alumnus.alumni_id, **education.model_dump()
            )
        )
    if engagement is not None and engagement.has_values():
        session.add(
            AlumniProgramEngagement(
                alumni_id=alumnus.alumni_id, **engagement.model_dump()
            )
        )

    await session.commit()
    await session.refresh(alumnus)
    return alumnus


async def _upsert_section(
    session: AsyncSession,
    model: type,
    alumni_id: int,
    values: dict[str, object],
    *,
    order_by=None,
) -> bool:
    """Update the existing related row for *alumni_id* (or insert one) from
    *values*. Returns True if a row was written (always, when called).

    The matching read query mirrors ``profile.get_profile`` so we update the
    same row the profile/edit page shows. Only called when the section carries
    at least one non-empty value (the caller checks ``has_values``).
    """
    stmt = select(model).where(model.alumni_id == alumni_id)
    if order_by is not None:
        stmt = stmt.order_by(order_by)
    existing = await session.scalar(stmt.limit(1))
    if existing is not None:
        for field, value in values.items():
            setattr(existing, field, value)
    else:
        session.add(model(alumni_id=alumni_id, **values))
    return True


async def update_alumni(
    session: AsyncSession,
    alumni_id: int,
    payload: AlumniUpdateFull,
    actor_user_id: int | None = None,
) -> Alumni:
    alumnus = await get_alumni(session, alumni_id)
    # Core columns only — nested sections are handled via upsert below.
    changes = payload.model_dump(
        exclude_unset=True,
        exclude={"contact", "career", "education", "engagement"},
    )
    if "spouse_alumni_id" in changes:
        await _validate_spouse_link(
            session, changes["spouse_alumni_id"], self_id=alumni_id
        )
    # Only audit fields that actually changed; capture before/after per field.
    applied: dict[str, tuple[object, object]] = {}
    for field, value in changes.items():
        old = getattr(alumnus, field)
        if old != value:
            applied[field] = (old, value)
            setattr(alumnus, field, value)

    # Upsert each related section only when it carries a real value, so empty
    # sections never create rows. Mirrors create_alumni's section handling;
    # getattr keeps this tolerant of a core-only payload (direct callers/tests).
    contact = getattr(payload, "contact", None)
    career = getattr(payload, "career", None)
    education = getattr(payload, "education", None)
    engagement = getattr(payload, "engagement", None)
    section_written = False
    if contact is not None and contact.has_values():
        section_written |= await _upsert_section(
            session,
            AlumniContactInfo,
            alumni_id,
            contact.model_dump(),
            order_by=AlumniContactInfo.contact_info_id,
        )
    if career is not None and career.has_values():
        section_written |= await _upsert_section(
            session,
            CurrentEmployment,
            alumni_id,
            career.model_dump(),
            order_by=CurrentEmployment.current_employment_id.desc(),
        )
    if education is not None and education.has_values():
        section_written |= await _upsert_section(
            session,
            EducationHistory,
            alumni_id,
            education.model_dump(),
            order_by=EducationHistory.degree_year.desc().nullslast(),
        )
    if engagement is not None and engagement.has_values():
        section_written |= await _upsert_section(
            session,
            AlumniProgramEngagement,
            alumni_id,
            engagement.model_dump(),
        )

    if applied or section_written:
        # Any manual edit (core or section) stamps provenance so later imports
        # won't clobber it.
        alumnus.manually_edited_at = _now()
        for field, (old, new) in applied.items():
            _audit(
                session,
                actor_user_id,
                "update",
                alumni_id,
                field_name=field,
                old_value=old,
                new_value=new,
            )
        if section_written and not applied:
            # Record at least one audit entry for a section-only edit.
            _audit(session, actor_user_id, "update", alumni_id)
        await session.commit()
        await session.refresh(alumnus)
    return alumnus


async def archive_alumni(
    session: AsyncSession, alumni_id: int, actor_user_id: int | None = None
) -> Alumni:
    """Soft-delete: flag the record archived. Idempotent."""
    alumnus = await get_alumni(session, alumni_id)
    if not alumnus.archived:
        alumnus.archived = True
        alumnus.manually_edited_at = _now()
        _audit(session, actor_user_id, "archive", alumni_id)
        await session.commit()
        await session.refresh(alumnus)
    return alumnus


async def restore_alumni(
    session: AsyncSession, alumni_id: int, actor_user_id: int | None = None
) -> Alumni:
    """Reverse a soft-delete (unarchive). Idempotent."""
    alumnus = await get_alumni(session, alumni_id)
    if alumnus.archived:
        alumnus.archived = False
        alumnus.manually_edited_at = _now()
        _audit(session, actor_user_id, "restore", alumni_id)
        await session.commit()
        await session.refresh(alumnus)
    return alumnus

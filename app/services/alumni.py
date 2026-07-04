"""Alumni business logic.

Owns the rules that aren't just data access:
  * soft-delete — ``archived`` is flipped, rows are never hard-deleted
  * manual-edit provenance — any client edit stamps ``manually_edited_at`` so
    later imports won't clobber it (manual edits win)
"""

import contextlib
import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.crm import Survey
from app.models.employment import (
    CurrentEmployment,
    EducationHistory,
    EmploymentHistory,
)
from app.models.engagement import AlumniProgramEngagement, FinanceSocietyLeadership
from app.models.tags import StatusLabel, Tag
from app.repositories import alumni as repo
from app.schemas.alumni import AlumniCreateFull, AlumniUpdateFull
from app.services import hygiene

# Cap each filter-option list so the panel can't request an unbounded distinct
# set (mirrors the geography options cap).
_OPTIONS_CAP = 200


async def _distinct_values(session: AsyncSession, column, *, desc: bool = False) -> list:
    """Distinct, non-null, sorted values for one column (capped)."""
    order = column.desc() if desc else column
    rows = (
        await session.execute(
            select(column)
            .where(column.is_not(None))
            .distinct()
            .order_by(order)
            .limit(_OPTIONS_CAP)
        )
    ).all()
    return [r[0] for r in rows]


async def filter_options(session: AsyncSession) -> dict:
    """Distinct option lists for the advanced-filter panel's multi-selects.

    Pulled live from the data so new employers / titles / etc. appear without a
    code change. Each list is capped."""
    return {
        "employers": await _distinct_values(session, CurrentEmployment.current_employer),
        "past_employers": await _distinct_values(
            session, EmploymentHistory.employer_name
        ),
        "titles": await _distinct_values(session, CurrentEmployment.current_title),
        "seniority_levels": await _distinct_values(
            session, CurrentEmployment.seniority_level
        ),
        "industries": await _distinct_values(
            session, CurrentEmployment.current_industry
        ),
        "cities": await _distinct_values(session, AlumniContactInfo.city),
        "states": await _distinct_values(session, AlumniContactInfo.state),
        "tags": await _distinct_values(session, Tag.tag_name),
        "status_labels": await _distinct_values(session, StatusLabel.status_label_name),
        "leadership_roles": await _distinct_values(
            session, FinanceSocietyLeadership.leadership_role
        ),
        "survey_statuses": await _distinct_values(session, Survey.survey_status),
        "graduation_years": await _distinct_values(
            session, Alumni.graduation_year, desc=True
        ),
    }

# Nested write sections handled via related tables, not the alumni core row.
SECTION_KEYS = frozenset({"contact", "career", "education", "engagement"})


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


async def get_alumni(
    session: AsyncSession,
    alumni_id: int,
    *,
    include_archived: bool = False,
) -> Alumni:
    """Fetch one alumnus by id.

    Archived (soft-deleted) records are treated as absent (404) for normal
    reads — a direct ``GET /alumni/{id}`` must not surface a record that was
    removed from the directory. ``include_archived=True`` (full_access edit
    flows: preview/update/archive/restore) bypasses that so those paths keep
    working on archived rows.
    """
    alumnus = await repo.get(session, alumni_id)
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    if alumnus.archived and not include_archived:
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


async def log_search(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    filters: dict[str, object],
) -> None:
    """Record a disclosure-audit row for an alumni search/list (FERPA).

    Stores the actor + a short summary of the ACTIVE filters (only the
    non-empty ones) in ``new_value`` — never the result payload. One lightweight
    row per search. No-op when the actor is unknown.
    """
    if actor_user_id is None:
        return
    active = {k: v for k, v in filters.items() if v not in (None, "", False)}
    summary = ", ".join(f"{k}={v}" for k, v in sorted(active.items())) or "(no filters)"
    # Best-effort: a disclosure-audit failure must NEVER break the read itself.
    try:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type="search",
                entity_type="alumni",
                entity_id=None,
                new_value=summary[:1000],
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - audit is best-effort
        with contextlib.suppress(Exception):
            await session.rollback()


async def log_preview(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    alumni_id: int | None = None,
) -> None:
    """Record an audit row for a data-hygiene preview (FERPA).

    Previews read the DB (duplicate detection) and surface stored data, so they
    are a disclosure worth attributing. ``alumni_id`` is set for an EDIT preview
    and ``None`` for a CREATE preview. No-op when the actor is unknown.
    """
    if actor_user_id is None:
        return
    try:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type="preview",
                entity_type="alumni",
                entity_id=alumni_id,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - audit is best-effort
        with contextlib.suppress(Exception):
            await session.rollback()


async def create_alumni(
    session: AsyncSession,
    payload: AlumniCreateFull,
    actor_user_id: int | None = None,
) -> Alumni:
    # Data-hygiene pass: clean the payload and persist the CLEANED values (the
    # UI's /preview is not trusted — defense-in-depth). Exact duplicates block.
    cleaned, _changes = hygiene.clean_alumni_payload(payload, jsonable=False)
    blockers, _warnings = await hygiene.detect_duplicates(session, cleaned)
    if blockers:
        raise ConflictError(blockers[0]["message"])

    # Core columns only — nested sections are popped off before constructing the
    # Alumni row (they map to related tables, not the alumni table).
    core = {k: v for k, v in cleaned.items() if k not in SECTION_KEYS}
    await _validate_spouse_link(session, core.get("spouse_alumni_id"))
    alumnus = Alumni(**core)
    session.add(alumnus)
    # Need the generated alumni_id to attach the related rows; flush gets it
    # without committing, keeping everything in one transaction.
    await session.flush()
    if actor_user_id is not None:
        _audit(session, actor_user_id, "create", alumnus.alumni_id)

    # Insert each related section only when it carries at least one real value,
    # so untouched sections never create empty rows. The presence/has_values
    # gate still comes from the validated payload section; the values written
    # come from the cleaned dict. getattr keeps this tolerant of a core-only
    # payload (e.g. direct service callers / tests).
    contact = getattr(payload, "contact", None)
    career = getattr(payload, "career", None)
    education = getattr(payload, "education", None)
    engagement = getattr(payload, "engagement", None)
    if contact is not None and contact.has_values():
        session.add(
            AlumniContactInfo(
                alumni_id=alumnus.alumni_id, **cleaned.get("contact", {})
            )
        )
    if career is not None and career.has_values():
        session.add(
            CurrentEmployment(
                alumni_id=alumnus.alumni_id, **cleaned.get("career", {})
            )
        )
    if education is not None and education.has_values():
        session.add(
            EducationHistory(
                alumni_id=alumnus.alumni_id, **cleaned.get("education", {})
            )
        )
    if engagement is not None and engagement.has_values():
        session.add(
            AlumniProgramEngagement(
                alumni_id=alumnus.alumni_id, **cleaned.get("engagement", {})
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
    # Archived records 404 on edit, symmetric with GET /alumni/{id} — an archived
    # record is "removed from the directory", so it must be restored (via
    # POST /alumni/{id}/restore) before it can be edited, not silently mutated.
    alumnus = await get_alumni(session, alumni_id, include_archived=False)
    # Data-hygiene pass: clean the provided fields (write the CLEANED values) and
    # block exact duplicates against everyone *except* this record. Fuzzy
    # warnings never block. jsonable=False keeps dates as date objects for the
    # ORM. Cleaning is idempotent, so a re-save of already-clean data is a no-op.
    cleaned, _changes = hygiene.clean_alumni_payload(payload, jsonable=False)
    blockers, _warnings = await hygiene.detect_duplicates(
        session, cleaned, exclude_alumni_id=alumni_id
    )
    if blockers:
        raise ConflictError(blockers[0]["message"])

    # Core columns only — nested sections are handled via upsert below.
    changes = {k: v for k, v in cleaned.items() if k not in SECTION_KEYS}
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
            hygiene.clean_section("contact", contact.model_dump()),
            order_by=AlumniContactInfo.contact_info_id,
        )
    if career is not None and career.has_values():
        section_written |= await _upsert_section(
            session,
            CurrentEmployment,
            alumni_id,
            hygiene.clean_section("career", career.model_dump()),
            order_by=CurrentEmployment.current_employment_id.desc(),
        )
    if education is not None and education.has_values():
        # NOTE (#175): the full-edit-form education block edits the alumnus's
        # MOST-RECENT degree in place (single-row upsert). Multi-degree records
        # (e.g. BS + MBA) are managed via the dedicated per-row endpoints
        # (POST/PATCH/DELETE /alumni/{id}/education); this path intentionally does
        # NOT create a second row, so a form save can't silently fan out degrees.
        section_written |= await _upsert_section(
            session,
            EducationHistory,
            alumni_id,
            hygiene.clean_section("education", education.model_dump()),
            order_by=EducationHistory.degree_year.desc().nullslast(),
        )
    if engagement is not None and engagement.has_values():
        section_written |= await _upsert_section(
            session,
            AlumniProgramEngagement,
            alumni_id,
            hygiene.clean_section("engagement", engagement.model_dump()),
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
    alumnus = await get_alumni(session, alumni_id, include_archived=True)
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
    alumnus = await get_alumni(session, alumni_id, include_archived=True)
    if alumnus.archived:
        alumnus.archived = False
        alumnus.manually_edited_at = _now()
        _audit(session, actor_user_id, "restore", alumni_id)
        await session.commit()
        await session.refresh(alumnus)
    return alumnus

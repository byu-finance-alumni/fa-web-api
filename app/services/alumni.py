"""Alumni business logic.

Owns the rules that aren't just data access:
  * soft-delete — ``archived`` is flipped, rows are never hard-deleted
  * manual-edit provenance — any client edit stamps ``manually_edited_at`` so
    later imports won't clobber it (manual edits win)
  * last-updated provenance (#285) — any write that actually changes something
    stamps ``profile_updated_by_user_id`` with the acting user, so the profile
    can render "Last updated <updated_at> by <name>" off ONE trustworthy pair:
    ``updated_at`` (bumped by ``TimestampMixin.onupdate``) + this FK. The
    hand-typed ``profile_updated_date`` / ``profile_updated_by`` columns are
    intake-sheet provenance only and are no longer part of the client write path
    (stripped at the route boundary; the CSV importer still records what the
    spreadsheet claimed).
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
from app.models.tags import AlumniStatusLabel, AlumniTag, StatusLabel, Tag
from app.repositories import alumni as repo
from app.schemas.alumni import AlumniCreateFull, AlumniUpdateFull
from app.services import hygiene

# Cap each filter-option list so the panel can't request an unbounded distinct
# set (mirrors the geography options cap).
_OPTIONS_CAP = 200


def _visible_alumni_exists(alumni_id_col):
    """Correlated EXISTS restricting an option row to the VISIBLE population.

    The list endpoint defaults to ``kind="alumni"`` with archived excluded, so a
    filter option must only surface values held by a non-archived graduate —
    otherwise the panel offers a value that returns zero rows (#184). Archived
    and friend-only (``is_alumni=false``) records are excluded here to match.
    ``alumni_id_col`` is the linking column on the option's own (or association)
    table.
    """
    return (
        select(Alumni.alumni_id)
        .where(
            Alumni.alumni_id == alumni_id_col,
            Alumni.archived.is_(False),
            Alumni.is_alumni.is_(True),
        )
        .exists()
    )


def _distinct_query(column, *, desc: bool = False, scope=None):
    """Pure ``SELECT DISTINCT column`` (non-null, sorted, capped).

    ``scope`` is an optional iterable of extra WHERE predicates restricting the
    rows to the visible population. Split out (no IO) so the scoping can be
    unit-tested by compiling the statement.
    """
    order = column.desc() if desc else column
    stmt = select(column).where(column.is_not(None))
    if scope is not None:
        stmt = stmt.where(*scope)
    return stmt.distinct().order_by(order).limit(_OPTIONS_CAP)


async def _distinct_values(
    session: AsyncSession, column, *, desc: bool = False, scope=None
) -> list:
    """Distinct, non-null, sorted values for one column (capped, population-scoped)."""
    rows = (
        await session.execute(_distinct_query(column, desc=desc, scope=scope))
    ).all()
    return [r[0] for r in rows]


async def filter_options(session: AsyncSession) -> dict:
    """Distinct option lists for the advanced-filter panel's multi-selects.

    Pulled live from the data so new employers / titles / etc. appear without a
    code change. Each list is capped and scoped to the VISIBLE population (the
    list endpoint's default: non-archived graduates), so an option never returns
    zero rows (#184).
    """
    # Per-table links into the visible-population guard. Every option table (or
    # its association table) references the alumni row via ``alumni_id``.
    employment_scope = [_visible_alumni_exists(CurrentEmployment.alumni_id)]
    history_scope = [_visible_alumni_exists(EmploymentHistory.alumni_id)]
    leadership_scope = [_visible_alumni_exists(FinanceSocietyLeadership.alumni_id)]
    survey_scope = [_visible_alumni_exists(Survey.alumni_id)]
    # graduation_year lives on the alumni row itself, so the guard is a plain
    # predicate rather than a correlated EXISTS.
    alumni_scope = [Alumni.archived.is_(False), Alumni.is_alumni.is_(True)]
    # Tag / status-label labels live in lookup tables; scope through their
    # association row to the visible population.
    tag_scope = [
        select(AlumniTag.alumni_tag_id)
        .where(
            AlumniTag.tag_id == Tag.tag_id,
            _visible_alumni_exists(AlumniTag.alumni_id),
        )
        .exists()
    ]
    status_scope = [
        select(AlumniStatusLabel.alumni_status_label_id)
        .where(
            AlumniStatusLabel.status_label_id == StatusLabel.status_label_id,
            _visible_alumni_exists(AlumniStatusLabel.alumni_id),
        )
        .exists()
    ]

    # Industries span the PRIMARY and SECONDARY columns because the list filter
    # matches either (repositories.alumni.build_alumni_query), so the option list
    # must offer both. Union the two distinct sets, dedupe + sort, then re-cap so
    # options and results agree (#184).
    primary_industries = await _distinct_values(
        session, CurrentEmployment.current_industry, scope=employment_scope
    )
    secondary_industries = await _distinct_values(
        session, CurrentEmployment.current_industry_secondary, scope=employment_scope
    )
    industries = sorted(set(primary_industries) | set(secondary_industries))[
        :_OPTIONS_CAP
    ]

    return {
        "employers": await _distinct_values(
            session, CurrentEmployment.current_employer, scope=employment_scope
        ),
        "past_employers": await _distinct_values(
            session, EmploymentHistory.employer_name, scope=history_scope
        ),
        "titles": await _distinct_values(
            session, CurrentEmployment.current_title, scope=employment_scope
        ),
        "seniority_levels": await _distinct_values(
            session, CurrentEmployment.seniority_level, scope=employment_scope
        ),
        "industries": industries,
        # City/State options come off the EMPLOYMENT record, matching what the
        # list filter matches on (repositories.alumni.build_alumni_query) and
        # what the map plots (#287). They used to read AlumniContactInfo, which
        # only ever agreed because the import mirrored the work address onto the
        # contact row — offering options the filter couldn't match the moment
        # that stopped being true.
        "cities": await _distinct_values(
            session, CurrentEmployment.current_city, scope=employment_scope
        ),
        "states": await _distinct_values(
            session, CurrentEmployment.current_state, scope=employment_scope
        ),
        "tags": await _distinct_values(session, Tag.tag_name, scope=tag_scope),
        "status_labels": await _distinct_values(
            session, StatusLabel.status_label_name, scope=status_scope
        ),
        "leadership_roles": await _distinct_values(
            session, FinanceSocietyLeadership.leadership_role, scope=leadership_scope
        ),
        "survey_statuses": await _distinct_values(
            session, Survey.survey_status, scope=survey_scope
        ),
        "graduation_years": await _distinct_values(
            session, Alumni.graduation_year, desc=True, scope=alumni_scope
        ),
        "graduation_classes": await _distinct_values(
            session, Alumni.graduation_class, desc=True, scope=alumni_scope
        ),
    }

# Nested write sections handled via related tables, not the alumni core row.
# former -> employment_history (a prior role); leadership -> finance_society_leadership.
SECTION_KEYS = frozenset(
    {"contact", "career", "education", "engagement", "former", "leadership"}
)


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
    # Last-updated provenance (#285): stamp the creator as the updater so a brand
    # new record reads "Last updated <created> by <name>" instead of falling back
    # to the intake sheet's free-text name. Never set from the client — the FK is
    # absent from the write schema, so the actor is the only source. Left untouched
    # when the actor is unknown (direct service callers / tests) rather than
    # writing NULL over a resolved user.
    if actor_user_id is not None:
        core["profile_updated_by_user_id"] = actor_user_id
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
    former = getattr(payload, "former", None)
    leadership = getattr(payload, "leadership", None)
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
    # A prior role -> one employment_history row flagged is_current=False.
    if former is not None and former.has_values():
        session.add(
            EmploymentHistory(
                alumni_id=alumnus.alumni_id,
                is_current=False,
                **cleaned.get("former", {}),
            )
        )
    # A student finance-society leadership role -> one leadership row.
    if leadership is not None and leadership.has_values():
        session.add(
            FinanceSocietyLeadership(
                alumni_id=alumnus.alumni_id, **cleaned.get("leadership", {})
            )
        )

    await session.commit()
    await session.refresh(alumnus)
    return alumnus


def _as_datetime(value: object) -> datetime.datetime | None:
    """A ``date``/``datetime`` as an aware UTC ``datetime``, else ``None``.

    Naive datetimes are read as UTC and a bare ``date`` as its UTC midnight, so
    the two never compare unequal purely because of their type. ``bool`` is
    checked first: it is an ``int``, not a date, but being explicit keeps the
    intent obvious.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=datetime.UTC)
    if isinstance(value, datetime.date):
        return datetime.datetime(
            value.year, value.month, value.day, tzinfo=datetime.UTC
        )
    return None


def _is_blank(value: object) -> bool:
    """True for the values that all mean "nothing here": ``None`` and whitespace.

    Legacy rows (pre-hygiene imports) can hold ``""`` where a cleaned payload now
    sends ``None``. Both render as an empty field, so calling that a change would
    bump "last updated" on a save the user can see changed nothing.
    """
    return value is None or (isinstance(value, str) and not value.strip())


def _unchanged(old: object, new: object) -> bool:
    """True when writing *new* over *old* would not actually change anything.

    A FALSE "changed" here is permanent — the field would re-report as changed on
    every single save forever — so the comparison is deliberately careful about
    values that are equal but not identically typed:

      * blank-vs-blank (``None`` / ``""``) is not a change; blank-vs-value is.
      * ``date`` vs ``datetime``, and naive vs aware, are compared as UTC instants
        rather than by type (``date(2020,1,1) != datetime(2020,1,1)`` in Python).
      * numerics need no special case — ``Decimal("1.0") == Decimal("1") == 1``
        already holds.
    """
    if _is_blank(old) or _is_blank(new):
        return _is_blank(old) and _is_blank(new)
    old_dt, new_dt = _as_datetime(old), _as_datetime(new)
    if old_dt is not None and new_dt is not None:
        return old_dt == new_dt
    return bool(old == new)


async def _upsert_section(
    session: AsyncSession,
    model: type,
    alumni_id: int,
    values: dict[str, object],
    *,
    order_by=None,
) -> bool:
    """Update the existing related row for *alumni_id* (or insert one) from
    *values*. Returns True only when something ACTUALLY changed.

    The matching read query mirrors ``profile.get_profile`` so we update the
    same row the profile/edit page shows.

    The return value gates ``manually_edited_at`` / ``profile_updated_by_user_id``
    / ``updated_at`` in ``update_alumni``, so it must answer "did this write
    change the record?", not "was this section submitted?" (#285). Opening Edit ->
    Employment and saving without touching a field submits a full, populated
    section; reporting that as written would stamp the profile "updated today by
    <whoever opened it>" and make the very date this card exists to fix
    untrustworthy. Fields that match what is stored are left alone entirely, so a
    no-op save doesn't even dirty the session.

    Callers gate on ``has_values`` (plus the derived-region merge for contact), so
    *values* always carries something; a row that doesn't exist yet is still only
    inserted when the incoming values are more than blanks/False, matching
    ``has_values``' rule that an all-blank section is nothing to write.
    """
    stmt = select(model).where(model.alumni_id == alumni_id)
    if order_by is not None:
        stmt = stmt.order_by(order_by)
    existing = await session.scalar(stmt.limit(1))
    if existing is not None:
        changed = False
        for field, value in values.items():
            if _unchanged(getattr(existing, field, None), value):
                continue
            setattr(existing, field, value)
            changed = True
        return changed
    if all(_is_blank(v) or v is False for v in values.values()):
        return False
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
    # Region auto-fill (#283) only fires when the work state actually CHANGED, so
    # the cleaner needs the state currently on the record. Read it only when the
    # payload carries a work state at all — otherwise nothing can derive and the
    # query would be wasted. `stored_work_state` is the same read `/preview` uses,
    # so the preview and this write always agree on whether the state moved.
    stored_state = (
        await hygiene.stored_work_state(session, alumni_id)
        if hygiene.work_state_supplied(payload)
        else None
    )
    # Data-hygiene pass: clean the provided fields (write the CLEANED values) and
    # block exact duplicates against everyone *except* this record. Fuzzy
    # warnings never block. jsonable=False keeps dates as date objects for the
    # ORM. Cleaning is idempotent, so a re-save of already-clean data is a no-op.
    cleaned, _changes = hygiene.clean_alumni_payload(
        payload, jsonable=False, stored_state=stored_state
    )
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
    #
    # model_dump(exclude_unset=True): write ONLY the section fields the caller
    # actually sent, so a PARTIAL section update (e.g. the focused edit forms that
    # submit just Employment or just Personal) can't null the sibling fields it
    # omitted. A field sent as an explicit null IS "set", so intentional clears
    # still apply; a merely-absent field is left untouched.
    contact = getattr(payload, "contact", None)
    career = getattr(payload, "career", None)
    education = getattr(payload, "education", None)
    engagement = getattr(payload, "engagement", None)
    section_written = False
    contact_values = (
        hygiene.clean_section("contact", contact.model_dump(exclude_unset=True))
        if contact is not None and contact.has_values()
        else {}
    )
    # Region auto-fill (#283) has to be merged in HERE, not left to the sections
    # above: `hygiene.clean_alumni_payload` derives the region (only when the work
    # state actually CHANGED — see `derive_region`) into `cleaned["contact"]
    # ["region"]`, but the section write is driven by the raw payload — so an
    # EMPLOYMENT-ONLY edit (the exact case Tanya reported: change the work state,
    # touch nothing else) sends no contact section at all and the derived region
    # would be silently dropped while /preview claimed it was saved.
    #
    # Read the value off `cleaned` rather than calling `derive_region` again — it
    # returns None on a second pass over its own output by design (the region now
    # looks explicitly supplied), which is what keeps re-cleaning idempotent.
    # An explicitly-supplied region is already in `contact_values` and wins: in
    # that case `derive_region` derived nothing and this only re-reads what the
    # caller sent, so the `not in` guard leaves it untouched either way.
    derived_region = (cleaned.get("contact") or {}).get("region")
    if derived_region is not None and "region" not in contact_values:
        contact_values["region"] = derived_region
    if contact_values:
        section_written |= await _upsert_section(
            session,
            AlumniContactInfo,
            alumni_id,
            contact_values,
            order_by=AlumniContactInfo.contact_info_id,
        )
    if career is not None and career.has_values():
        section_written |= await _upsert_section(
            session,
            CurrentEmployment,
            alumni_id,
            hygiene.clean_section("career", career.model_dump(exclude_unset=True)),
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
            hygiene.clean_section("education", education.model_dump(exclude_unset=True)),
            order_by=EducationHistory.degree_year.desc().nullslast(),
        )
    if engagement is not None and engagement.has_values():
        section_written |= await _upsert_section(
            session,
            AlumniProgramEngagement,
            alumni_id,
            hygiene.clean_section("engagement", engagement.model_dump(exclude_unset=True)),
        )

    if applied or section_written:
        # Any manual edit (core or section) stamps provenance so later imports
        # won't clobber it.
        alumnus.manually_edited_at = _now()
        # Last-updated provenance (#285): record WHO made this edit. Gated on the
        # same `applied or section_written` condition as manually_edited_at, so a
        # no-op save never re-attributes the profile. Touching the Alumni row here
        # also guarantees TimestampMixin.onupdate bumps `updated_at` even for a
        # section-only edit (career/contact/...), keeping the profile's
        # "Last updated" honest for the employment edits that prompted this card.
        if actor_user_id is not None:
            alumnus.profile_updated_by_user_id = actor_user_id
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

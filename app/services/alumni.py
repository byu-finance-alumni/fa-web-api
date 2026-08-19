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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import audit_source, new_change_set_id
from app.core.dropdowns import ENGAGEMENT_FLAG_TAGS, engagement_flag_for_tag
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


async def _tag_filter_options(session: AsyncSession, tag_scope) -> list[str]:
    """The tag facet's options, drawn from BOTH tag stores (#629).

    Ordinary tags come from ``alumni_tags`` as before. The nine "ways to get
    involved" come from their ``alumni_program_engagement`` boolean instead,
    and only when at least one visible alumnus actually has the flag set — the
    same #184 rule the other facets follow, so the panel never offers an option
    that returns zero rows.

    Leftover ``alumni_tags`` rows for one of the nine are filtered out so the
    name is offered once, backed by the flag, rather than twice.
    """
    names = {
        name
        for name in await _distinct_values(session, Tag.tag_name, scope=tag_scope)
        if engagement_flag_for_tag(name) is None
    }
    # One aggregate row: "is this flag set on at least one visible alumnus?" for
    # all nine at once, rather than nine separate round trips.
    held = (
        await session.execute(
            select(
                *[
                    func.bool_or(getattr(AlumniProgramEngagement, column))
                    for column in ENGAGEMENT_FLAG_TAGS.values()
                ]
            ).where(_visible_alumni_exists(AlumniProgramEngagement.alumni_id))
        )
    ).all()
    if held:
        names.update(
            tag_name
            for tag_name, is_held in zip(ENGAGEMENT_FLAG_TAGS, held[0], strict=False)
            if is_held
        )
    return sorted(names)


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
    education_scope = [_visible_alumni_exists(EducationHistory.alumni_id)]
    contact_scope = [_visible_alumni_exists(AlumniContactInfo.alumni_id)]
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

    # Industries used to be ONE unioned list because one ``industry`` filter
    # matched either column. The filter is now split (#584) — ``industry`` hits
    # the primary column, ``secondary_industry`` the secondary one — so each
    # dropdown gets its own distinct list. Unioning them here again would offer
    # the primary select values only the secondary column holds, i.e. options
    # that return zero rows, which is exactly what #184 forbids.
    primary_industries = await _distinct_values(
        session, CurrentEmployment.current_industry, scope=employment_scope
    )
    secondary_industries = await _distinct_values(
        session, CurrentEmployment.current_industry_secondary, scope=employment_scope
    )

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
        "industries": primary_industries,
        "secondary_industries": secondary_industries,
        # Employment status (#584) is read LIVE off the alumni rows rather than
        # from the canonical 7-value dropdown, because the column is free text and
        # deliberately still holds off-list legacy values ("Employed", "Stay at
        # home parent"). Offering the vocab list instead would hide those alumni
        # behind an option the UI never shows.
        "employment_statuses": await _distinct_values(
            session, Alumni.employment_status, scope=alumni_scope
        ),
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
        # Country comes off the same employment record as city/state above.
        "countries": await _distinct_values(
            session, CurrentEmployment.current_country, scope=employment_scope
        ),
        # Region is the DERIVED grouping (#283) and is stored on the contact row,
        # so its options must be read there — that is the column the filter
        # matches, and re-deriving it here would let the two drift apart.
        "regions": await _distinct_values(
            session, AlumniContactInfo.region, scope=contact_scope
        ),
        "past_titles": await _distinct_values(
            session, EmploymentHistory.employment_title, scope=history_scope
        ),
        "universities": await _distinct_values(
            session, EducationHistory.university, scope=education_scope
        ),
        "degrees": await _distinct_values(
            session, EducationHistory.degree, scope=education_scope
        ),
        "majors": await _distinct_values(
            session, EducationHistory.major, scope=education_scope
        ),
        "tags": await _tag_filter_options(session, tag_scope),
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

# Payload keys that are WRITE CONTROLS, not data: they change what the save does
# rather than naming a column to write. They arrive at the top level of the
# payload alongside the core fields, so every "everything that isn't a section is
# a core column" comprehension has to exclude them or it will try to `setattr`
# them onto the Alumni row and blow up with an AttributeError.
#
# Mirrored as `hygiene._CONTROL_KEYS` (that module is imported BY this one and so
# cannot import back, exactly as `_SECTIONS` mirrors `SECTION_KEYS`), which drops
# them from the cleaned payload so `/preview` never shows a checkbox as a stored
# field. A parity test pins the two together.
CONTROL_KEYS = frozenset({"archive_previous_role"})

# current_employment column -> employment_history column, for #446's demotion of
# an outgoing current role. Only the five columns BOTH tables have appear here:
# employment_history has no home for current_industry_secondary, current_country,
# current_zip or seniority_level. Those are not silently dropped - the archive
# audit row carries a snapshot of the WHOLE outgoing current_employment row in
# its old_value (see `_CAREER_SNAPSHOT_FIELDS`), so the trail keeps what the
# table cannot. Widening employment_history to hold them is a schema change with
# read-model and UI consequences, deliberately not bundled into this one.
_ROLE_ARCHIVE_COLUMNS = {
    "current_employer": "employer_name",
    "current_title": "employment_title",
    "current_industry": "employment_industry",
    "current_city": "city",
    "current_state": "state",
}

# Everything meaningful on a current_employment row, for the audit snapshot.
# `company_address` is deliberately absent - it is retired and pending drop
# (#287), so snapshotting it would keep a dead column alive in the trail.
_CAREER_SNAPSHOT_FIELDS = (
    "current_employer",
    "current_title",
    "current_industry",
    "current_industry_secondary",
    "current_city",
    "current_state",
    "current_country",
    "current_zip",
    "seniority_level",
)

# Snapshot fields for the employment_history row we create. Same tuple and same
# order as `profile._EMPLOYMENT_FIELDS`, which is the established shape for an
# add/delete audit value on this table; kept local rather than imported because
# it is another service's private detail and importing it would couple two
# services for four lines.
_EMPLOYMENT_HISTORY_SNAPSHOT_FIELDS = (
    "employer_name",
    "employment_title",
    "employment_industry",
    "city",
    "state",
    "start_year",
    "end_year",
    "is_current",
)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _snapshot(row: object, fields: tuple[str, ...]) -> dict[str, object]:
    """Detach *fields* off *row* into a plain dict.

    Detaching MATTERS here and is not defensive habit: the row this is called on
    is the very ``current_employment`` row ``_upsert_section`` is about to mutate,
    and SQLAlchemy's identity map hands both callers the SAME object — so holding
    a reference and reading it after the upsert would read the NEW values and
    archive the incoming role instead of the outgoing one.
    """
    return {field: getattr(row, field, None) for field in fields}


def _snapshot_text(values: dict[str, object]) -> str:
    """A snapshot dict as a one-line audit old/new value.

    Same rendering as ``profile._row_snapshot``: ``name=value`` pairs with
    ``repr`` values, so an empty string is distinguishable from NULL in the trail.
    """
    return "; ".join(f"{field}={value!r}" for field, value in values.items())


def career_snapshot(row: object) -> dict[str, object]:
    """The whole of a ``current_employment`` row, detached, ready to archive.

    Public because all three write paths (staff edit, survey apply, CSV import)
    have to take this snapshot at the same moment and in the same shape, or the
    rows they archive and the audit values they record would not match.
    """
    return _snapshot(row, _CAREER_SNAPSHOT_FIELDS)


def _employer_key(value: object) -> str | None:
    """An employer name reduced to its comparison key, or ``None`` if blank.

    Trims, collapses internal whitespace runs, and casefolds - so "  ACME   Corp"
    and "Acme Corp" are the same employer. Whitespace collapsing matches what
    ``hygiene`` already does to this column on write, and the casefold is added on
    top so a CASING cleanup pass cannot read as a job change.
    """
    if not isinstance(value, str):
        return None
    return " ".join(value.split()).casefold() or None


def employer_changed(old: object, new: object) -> bool:
    """True when *old* -> *new* is a MOVE BETWEEN NAMED EMPLOYERS (#446).

    This is the trigger the SURVEY and IMPORT paths archive on, decided by the
    product owner on 2026-08-18. The staff edit path does not use it: there the
    trigger is the explicit "this is a new role" checkbox, because a person is
    present to answer the question. Nobody is present on the other two, and the
    owner chose to infer rather than to leave those paths never archiving.

    The tradeoff was put to him explicitly and accepted: because the values alone
    cannot distinguish a job change from a correction, a fixed typo, a company
    rename ("Facebook" -> "Meta") or a vendor sheet that spells employers
    differently WILL manufacture prior-role rows that nobody held. The rule below
    is therefore drawn as narrowly as it can be while still firing on a real move:

    * **Case- and whitespace-insensitive.** A casing or spacing cleanup pass is
      the single most likely mass edit to this column, and it is not a job change.
    * **Blank on EITHER side is not a change.** Blank -> named is an alum whose
      employer was simply never on file: there is no prior employer to preserve,
      and treating a first-ever entry as a job change would archive a role the
      alum never left. Named -> blank is a skipped question or an empty
      spreadsheet cell far more often than it is "I quit"; archiving on it would
      let one blank column demote every alum in an 8,000-row sheet.

    What it deliberately does NOT protect against is a genuine misspelling being
    corrected to a different string. That case is indistinguishable from a real
    move without asking a human, and asking is exactly what these two paths
    cannot do.
    """
    old_key, new_key = _employer_key(old), _employer_key(new)
    if old_key is None or new_key is None:
        return False
    return old_key != new_key


def audit_role_archive(
    session: AsyncSession,
    actor_user_id: int | None,
    alumni_id: int,
    *,
    outgoing: dict[str, object],
    archived: EmploymentHistory,
    change_set_id: str | None,
) -> None:
    """Record a role demotion in the audit trail (#446).

    Shared by all three write paths so the rows they produce are identical and a
    reader never has to know which path demoted a role. ``source`` comes from the
    request-scoped provenance contextvar via ``_audit``, so a survey apply stamps
    ``survey`` and an import stamps ``import`` without any callsite saying so.

    This is a row NOBODY TYPED - the save wrote an ``employment_history`` entry no
    human filled in - so it has to be legible as its own act rather than inferred
    from a career field having changed. Pass the enclosing save's
    *change_set_id* so the demotion and the new role's field changes read as one
    version.

    The row id rides in ``field_name`` (``employment[12]``), the convention the
    per-row employment endpoints already use, since ``audit_logs`` has no row-id
    column. ``old_value`` snapshots the WHOLE outgoing ``current_employment`` row
    - including ``current_country`` / ``current_zip`` / ``seniority_level`` /
    ``current_industry_secondary``, which ``employment_history`` has no column for
    - so the trail preserves what the archived row cannot. ``new_value``
    snapshots the history row actually created, which is where the synthesised
    ``end_year`` becomes visible as ours rather than a human's.
    """
    _audit(
        session,
        actor_user_id,
        "archive_current_role",
        alumni_id,
        field_name=f"employment[{archived.employment_history_id}]",
        old_value=_snapshot_text(outgoing),
        new_value=_snapshot_text(
            _snapshot(archived, _EMPLOYMENT_HISTORY_SNAPSHOT_FIELDS)
        ),
        change_set_id=change_set_id,
    )


async def archive_current_role(
    session: AsyncSession, alumni_id: int, outgoing: dict[str, object]
) -> EmploymentHistory | None:
    """Copy the OUTGOING current role into ``employment_history`` (#446).

    The ONE implementation, shared by the staff edit, survey apply and CSV import
    paths, so an archived role looks the same however it was demoted. Only the
    TRIGGER differs between them (see ``employer_changed``): staff tick a
    checkbox, the other two infer from the employer moving.

    Returns the created row (flushed, so it has an id for the audit trail), or
    ``None`` when the outgoing role held nothing worth keeping.

    **Dates.** This is the part nobody typed, so it is spelled out:

    * ``start_year`` stays NULL. ``current_employment`` has no start column, so
      the date the alum began the outgoing role was never recorded anywhere. Any
      value here would be invented, and a wrong start year is worse than an
      absent one - the "worked in year X" filter reads ``start_year`` as a hard
      bound. NULL is already the ordinary state of this column.
    * ``end_year`` is the year of THIS EDIT. It is genuinely synthesised, and it
      means "the year this role stopped being the current one **in this
      database**" - not necessarily the year the alum actually left, which may
      have been earlier and which nobody knows. Leaving it NULL was the tempting
      alternative and is actively wrong: NULL ``end_year`` already MEANS "still
      held" everywhere it is read (the dashboard's recent-employer roll-up treats
      ``end_year IS NULL`` as current, and the worked-in-year filter treats it as
      open-ended), so an archived role with no end date would go on counting as a
      job the alum still holds - the exact confusion this feature exists to end.
      Staff can correct it on the Employment panel; the audit row records that we
      supplied it rather than a human.
    * ``is_current`` is False. It is history now, by definition.

    ``source_id`` stays NULL: the row came from a demotion, not from a named
    data source.
    """
    values = {
        target: outgoing.get(source)
        for source, target in _ROLE_ARCHIVE_COLUMNS.items()
    }
    # Nothing but blanks means there is no role here to preserve; writing the row
    # anyway would leave an empty entry on the Employment panel that a human then
    # has to go and delete. Mirrors `_upsert_section`'s all-blank rule.
    if all(_is_blank(value) for value in values.values()):
        return None
    row = EmploymentHistory(
        alumni_id=alumni_id,
        is_current=False,
        start_year=None,
        end_year=_now().year,
        **values,
    )
    session.add(row)
    # Flush to obtain the surrogate id BEFORE the audit row is written, so the
    # trail names the row it created (same reason as `profile.add_employment`).
    # Still one transaction - `update_alumni` commits once, below.
    await session.flush()
    return row


def _audit(
    session: AsyncSession,
    actor_user_id: int | None,
    action: str,
    alumni_id: int,
    *,
    field_name: str | None = None,
    old_value: object | None = None,
    new_value: object | None = None,
    change_set_id: str | None = None,
) -> None:
    """Record an alumni audit event when an acting user is known.

    For field-level changes (updates), ``field_name`` + old/new values are
    captured so the audit doubles as version history. Values are stringified to
    fit the ``text`` audit columns; ``None`` stays ``NULL``.

    ``field_name`` is the core column name for the alumni row itself, and
    ``<section>.<column>`` for a nested section (e.g. ``contact.email``,
    ``career.current_employer``) — the section prefixes are exactly
    ``SECTION_KEYS``, so a reader can tell "the record's own ``region``" from
    "the contact row's ``region``" without a second lookup.

    ``change_set_id`` groups every row written by ONE save; pass the same value
    for all of them. ``source`` is read from the request-scoped provenance
    contextvar so no callsite has to thread it through (#45).
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
                change_set_id=change_set_id,
                source=audit_source(),
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
    # A create payload is complete by definition, so `cleaned` alone is already
    # the effective record here — no overlay needed (contrast `update_alumni`).
    blockers, warnings = await hygiene.detect_duplicates(session, cleaned)
    if blockers:
        raise ConflictError(blockers[0]["message"])

    # Core columns only — nested sections are popped off before constructing the
    # Alumni row (they map to related tables, not the alumni table), and so are
    # the write-control keys, which name no column at all. `AlumniCreateFull`
    # carries no control key today (a create has no outgoing role to archive);
    # the exclusion is here so adding one later can't silently become
    # `Alumni(archive_previous_role=...)`.
    core = {
        k: v
        for k, v in cleaned.items()
        if k not in SECTION_KEYS and k not in CONTROL_KEYS
    }
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
    # Same contract as `update_alumni` — soft duplicate warnings ride back on the
    # created record so the caller can show them (#627).
    alumnus.duplicate_warnings = warnings
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


def _namespaced(
    section: str, changes: dict[str, tuple[object, object]]
) -> dict[str, tuple[object, object]]:
    """Prefix a section's changed-field map with its section key (#45).

    ``{"email": (a, b)}`` -> ``{"contact.email": (a, b)}``. The prefix is always
    one of ``SECTION_KEYS``, which is what lets one flat audit stream carry both
    the alumni row's own ``region`` and the contact row's ``region`` unambiguously.
    """
    return {f"{section}.{field}": value for field, value in changes.items()}


async def _upsert_section(
    session: AsyncSession,
    model: type,
    alumni_id: int,
    values: dict[str, object],
    *,
    order_by=None,
) -> dict[str, tuple[object, object]]:
    """Update the existing related row for *alumni_id* (or insert one) from
    *values*. Returns ``{field: (old, new)}`` for the fields that ACTUALLY
    changed — empty when nothing did.

    The matching read query mirrors ``profile.get_profile`` so we update the
    same row the profile/edit page shows.

    Truthiness of the return value gates ``manually_edited_at`` /
    ``profile_updated_by_user_id`` / ``updated_at`` in ``update_alumni``, so it
    must answer "did this write change the record?", not "was this section
    submitted?" (#285). Opening Edit -> Employment and saving without touching a
    field submits a full, populated section; reporting that as written would stamp
    the profile "updated today by <whoever opened it>" and make the very date this
    card exists to fix untrustworthy. Fields that match what is stored are left
    alone entirely, so a no-op save doesn't even dirty the session.

    It returns the CHANGED MAP rather than a bare bool (#45) because the old
    values are read here and nowhere else: returning a bool discarded them, which
    is why every section edit ever made recorded only that *something* changed.
    Since history cannot be reconstructed after the fact, capturing them at the
    one place they exist is the whole point.

    Callers gate on ``has_values`` (plus the derived-region merge for contact), so
    *values* always carries something; a row that doesn't exist yet is still only
    inserted when the incoming values are more than blanks/False, matching
    ``has_values``' rule that an all-blank section is nothing to write.
    """
    stmt = select(model).where(model.alumni_id == alumni_id)
    if order_by is not None:
        stmt = stmt.order_by(order_by)
    existing = await session.scalar(stmt.limit(1))
    changed: dict[str, tuple[object, object]] = {}
    if existing is not None:
        for field, value in values.items():
            old = getattr(existing, field, None)
            if _unchanged(old, value):
                continue
            setattr(existing, field, value)
            changed[field] = (old, value)
        return changed
    if all(_is_blank(v) or v is False for v in values.values()):
        return {}
    session.add(model(alumni_id=alumni_id, **values))
    # A brand-new section row: every field that carries something is a change
    # FROM nothing, so it audits as ``None -> value``. Blank/False fields are
    # skipped by the same rule that decides whether to insert at all — recording
    # "empty became empty" would bury the real changes in noise.
    return {
        field: (None, value)
        for field, value in values.items()
        if not (_is_blank(value) or value is False)
    }


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
    #
    # Duplicate detection runs against the EFFECTIVE record — the stored row with
    # this patch overlaid — not the patch alone (#627). `cleaned` is
    # exclude_unset, so the focused edit forms that submit just the name fields
    # carry no graduation year, and the fuzzy first+last+grad-year check needs all
    # three: passing `cleaned` here meant a rename into an exact collision found
    # nothing to warn about. `effective_identity` is query-free — every field it
    # reads is already on the loaded row.
    blockers, warnings = await hygiene.detect_duplicates(
        session,
        hygiene.effective_identity(alumnus, cleaned),
        exclude_alumni_id=alumni_id,
    )
    if blockers:
        raise ConflictError(blockers[0]["message"])

    # Core columns only — nested sections are handled via upsert below, and the
    # write-control keys (#446's `archive_previous_role`) are not columns at all:
    # left in here they would be `setattr`-ed onto the Alumni row.
    changes = {
        k: v
        for k, v in cleaned.items()
        if k not in SECTION_KEYS and k not in CONTROL_KEYS
    }
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
    # {"<section>.<field>": (old, new)} for every nested-section field that
    # actually changed (#45). Namespaced by section so it can be merged with the
    # core `applied` map without a collision — `region` lives on BOTH the alumni
    # row and the contact row.
    section_applied: dict[str, tuple[object, object]] = {}
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
        section_applied.update(
            _namespaced(
                "contact",
                await _upsert_section(
                    session,
                    AlumniContactInfo,
                    alumni_id,
                    contact_values,
                    order_by=AlumniContactInfo.contact_info_id,
                ),
            )
        )
    # #446 — "this is a new role". The outgoing current role is copied down into
    # employment_history instead of being overwritten away. The trigger is this
    # EXPLICIT flag and nothing else: the values alone cannot distinguish a job
    # change from a typo correction, so nothing is inferred from whether the
    # employer string moved.
    #
    # The snapshot has to be taken BEFORE the upsert, which mutates the stored row
    # in place; `_upsert_section` returns only the fields that changed, so the
    # untouched ones (a seniority level that stayed put while the employer moved)
    # exist nowhere else by the time it returns.
    archived_role: EmploymentHistory | None = None
    outgoing_role: dict[str, object] | None = None
    # getattr, not attribute access: the plain `AlumniUpdate` schema and the
    # direct service callers in tests carry no control keys, matching how every
    # nested section is read just above.
    if (
        getattr(payload, "archive_previous_role", False)
        and career is not None
        and career.has_values()
    ):
        stored_role = await session.scalar(
            select(CurrentEmployment)
            .where(CurrentEmployment.alumni_id == alumni_id)
            .order_by(CurrentEmployment.current_employment_id.desc())
            .limit(1)
        )
        # `None` when the alum has no current role on file yet — the flag is then
        # simply nothing to act on, not an error: an alum whose first employer is
        # being entered has no previous role to demote.
        if stored_role is not None:
            outgoing_role = career_snapshot(stored_role)
    if career is not None and career.has_values():
        career_changes = await _upsert_section(
            session,
            CurrentEmployment,
            alumni_id,
            hygiene.clean_section("career", career.model_dump(exclude_unset=True)),
            order_by=CurrentEmployment.current_employment_id.desc(),
        )
        section_applied.update(_namespaced("career", career_changes))
        # Gated on the career section ACTUALLY changing, which is a no-op guard,
        # not inference: the flag is still required and still does all the
        # deciding. Without it a mis-ticked box on a save that changed nothing
        # would clone the current role into history, leaving the record reading
        # "left Acme in 2026, currently at Acme". A save that changes nothing must
        # never manufacture history — the same rule that governs every other write
        # in this function.
        if career_changes and outgoing_role is not None:
            archived_role = await archive_current_role(
                session, alumni_id, outgoing_role
            )
    if education is not None and education.has_values():
        # NOTE (#175): the full-edit-form education block edits the alumnus's
        # MOST-RECENT degree in place (single-row upsert). Multi-degree records
        # (e.g. BS + MBA) are managed via the dedicated per-row endpoints
        # (POST/PATCH/DELETE /alumni/{id}/education); this path intentionally does
        # NOT create a second row, so a form save can't silently fan out degrees.
        section_applied.update(
            _namespaced(
                "education",
                await _upsert_section(
                    session,
                    EducationHistory,
                    alumni_id,
                    hygiene.clean_section(
                        "education", education.model_dump(exclude_unset=True)
                    ),
                    order_by=EducationHistory.degree_year.desc().nullslast(),
                ),
            )
        )
    if engagement is not None and engagement.has_values():
        section_applied.update(
            _namespaced(
                "engagement",
                await _upsert_section(
                    session,
                    AlumniProgramEngagement,
                    alumni_id,
                    hygiene.clean_section(
                        "engagement", engagement.model_dump(exclude_unset=True)
                    ),
                ),
            )
        )

    if applied or section_applied:
        # Any manual edit (core or section) stamps provenance so later imports
        # won't clobber it.
        alumnus.manually_edited_at = _now()
        # Last-updated provenance (#285): record WHO made this edit. Gated on the
        # same `applied or section_applied` condition as manually_edited_at, so a
        # no-op save never re-attributes the profile. Touching the Alumni row here
        # also guarantees TimestampMixin.onupdate bumps `updated_at` even for a
        # section-only edit (career/contact/...), keeping the profile's
        # "Last updated" honest for the employment edits that prompted this card.
        if actor_user_id is not None:
            alumnus.profile_updated_by_user_id = actor_user_id
        # One save = one change set (#45), so a five-field edit reads as one
        # version instead of five unrelated rows. Minted here, inside the
        # "something changed" branch, so a no-op save mints nothing.
        change_set_id = new_change_set_id()
        # Core fields AND section fields, each with its own old/new. Sections used
        # to fall into a single bare row with no field/old/new — and only when NO
        # core field changed, because the old `if section_written and not applied`
        # guard skipped it otherwise. That guard is gone: a save that touched a
        # core field and a section field recorded the section change NOWHERE, and
        # since old values can't be reconstructed, that history was lost for good.
        for field, (old, new) in [*applied.items(), *section_applied.items()]:
            _audit(
                session,
                actor_user_id,
                "update",
                alumni_id,
                field_name=field,
                old_value=old,
                new_value=new,
                change_set_id=change_set_id,
            )
        # #446 — the demotion, if this save made one. Same change set as the
        # rest of the save, so version history reads "this one save moved the old
        # role down and set the new one" rather than two unrelated events. The
        # row's shape and provenance are decided in `audit_role_archive`, shared
        # with the survey and import paths.
        if archived_role is not None:
            audit_role_archive(
                session,
                actor_user_id,
                alumni_id,
                outgoing=outgoing_role or {},
                archived=archived_role,
                change_set_id=change_set_id,
            )
        await session.commit()
        await session.refresh(alumnus)
    # Hand the soft duplicate warnings back to the caller (#627). Fuzzy matches
    # never block — two alumni really can share a name and a graduation year, and
    # a marriage rename into a genuine collision is sometimes correct — but the
    # person doing the rename has to be TOLD. Set after `refresh`, which reloads
    # mapped columns only and would otherwise be an easy place to lose this.
    alumnus.duplicate_warnings = warnings
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

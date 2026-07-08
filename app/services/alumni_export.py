"""Customizable alumni CSV export (#33).

The list view's CSV import already exists; this is the export side. The user
picks which columns to include (from :data:`CATALOG`) and the export streams
every alumnus matching the *current list filters* — restricted to those columns.

The query reuses ``repositories.alumni.build_alumni_query`` so the export hits
the EXACT population the list shows (same filters, same archived gating). Side
tables (contact / current employment / latest education / program engagement)
are bulk-loaded once each — only for the groups that actually have a selected
column — so a wide export is a handful of indexed queries, never an N+1.

Export is ``full_access`` and up (route-enforced) and audit-logged
(``export_alumni``) with the filter + column summary, mirroring the per-profile
export's disclosure trail. Bodies/values are never logged.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment, EducationHistory
from app.models.engagement import AlumniProgramEngagement
from app.repositories.alumni import SURVEY_CADENCE, build_alumni_query
from app.schemas.alumni_export import (
    AlumniExportFilters,
    ExportColumn,
    ExportColumnCatalog,
)

log = logging.getLogger(__name__)

# Hard cap so a hostile/empty filter set can't stream the whole table into one
# response. Far above the current population; if a real export ever exceeds it,
# the route returns a clear "narrow your filters" error rather than truncating.
MAX_EXPORT_ROWS = 10000

# Source groups. Each maps to a side table loaded at most once per export.
_ALUMNI = "alumni"  # columns straight off the alumni row
_CONTACT = "contact"
_CAREER = "career"
_EDUCATION = "education"
_ENGAGEMENT = "engagement"


@dataclass(frozen=True)
class _Col:
    key: str
    label: str
    group: str  # picker section (display)
    source: str  # which side table the value comes from
    attr: str  # attribute on that source row
    kind: str = "str"  # str | bool | date | int — controls CSV formatting


# The exportable columns, in CSV order. Labels match the import template where a
# column round-trips, so an exported file is recognizable next to an import one.
CATALOG: list[_Col] = [
    # --- Identity (alumni row) ---
    _Col("byu_id", "BYU ID", "Identity", _ALUMNI, "byu_id"),
    _Col("net_id", "Net ID", "Identity", _ALUMNI, "net_id"),
    _Col("first_name", "First name", "Identity", _ALUMNI, "first_name"),
    _Col("middle_name", "Middle name", "Identity", _ALUMNI, "middle_name"),
    _Col("last_name", "Last name", "Identity", _ALUMNI, "last_name"),
    _Col(
        "preferred_first_name",
        "Preferred first name",
        "Identity",
        _ALUMNI,
        "preferred_first_name",
    ),
    _Col("gender", "Gender", "Identity", _ALUMNI, "gender"),
    _Col("birth_date", "Birthday", "Identity", _ALUMNI, "birth_date", "date"),
    _Col(
        "graduation_year",
        "Graduation year",
        "Identity",
        _ALUMNI,
        "graduation_year",
        "int",
    ),
    _Col(
        "graduation_month",
        "Graduation month",
        "Identity",
        _ALUMNI,
        "graduation_month",
        "int",
    ),
    _Col(
        "finance_program_year",
        "Finance program year",
        "Identity",
        _ALUMNI,
        "finance_program_year",
        "int",
    ),
    _Col("graduate_degree", "Graduate degree", "Identity", _ALUMNI, "graduate_degree"),
    _Col("linkedin_url", "LinkedIn URL", "Identity", _ALUMNI, "linkedin_url"),
    _Col("deceased", "Deceased?", "Identity", _ALUMNI, "deceased", "bool"),
    _Col("notes", "Notes", "Identity", _ALUMNI, "notes"),
    # --- Spouse ---
    _Col("spouse_first_name", "Spouse first name", "Spouse", _ALUMNI, "spouse_first_name"),
    _Col("spouse_last_name", "Spouse last name", "Spouse", _ALUMNI, "spouse_last_name"),
    _Col(
        "spouse_birth_date",
        "Spouse birthday",
        "Spouse",
        _ALUMNI,
        "spouse_birth_date",
        "date",
    ),
    # --- Contact ---
    _Col("personal_email", "Personal email", "Contact", _CONTACT, "personal_email"),
    _Col("work_email", "Work email", "Contact", _CONTACT, "work_email"),
    _Col("phone", "Phone", "Contact", _CONTACT, "phone"),
    _Col("address_line_1", "Address line 1", "Contact", _CONTACT, "address_line_1"),
    _Col("address_line_2", "Address line 2", "Contact", _CONTACT, "address_line_2"),
    _Col("city", "City", "Contact", _CONTACT, "city"),
    _Col("state", "State", "Contact", _CONTACT, "state"),
    _Col("zip", "ZIP", "Contact", _CONTACT, "zip"),
    _Col("country", "Country", "Contact", _CONTACT, "country"),
    _Col("region", "Region", "Contact", _CONTACT, "region"),
    # --- Current career ---
    _Col("current_employer", "Current employer", "Career", _CAREER, "current_employer"),
    _Col("current_title", "Current title", "Career", _CAREER, "current_title"),
    _Col("current_industry", "Current industry", "Career", _CAREER, "current_industry"),
    _Col(
        "current_industry_secondary",
        "Secondary industry",
        "Career",
        _CAREER,
        "current_industry_secondary",
    ),
    _Col("current_city", "Current city", "Career", _CAREER, "current_city"),
    _Col("current_state", "Current state", "Career", _CAREER, "current_state"),
    _Col("current_country", "Current country", "Career", _CAREER, "current_country"),
    _Col("current_zip", "Current ZIP", "Career", _CAREER, "current_zip"),
    _Col("seniority_level", "Seniority level", "Career", _CAREER, "seniority_level"),
    # --- Education (latest entry) ---
    _Col("university", "University", "Education", _EDUCATION, "university"),
    _Col("college", "College", "Education", _EDUCATION, "college"),
    _Col("department", "Department", "Education", _EDUCATION, "department"),
    _Col("degree", "Degree", "Education", _EDUCATION, "degree"),
    _Col("major", "Major", "Education", _EDUCATION, "major"),
    _Col("degree_status", "Degree status", "Education", _EDUCATION, "degree_status"),
    _Col("degree_year", "Degree year", "Education", _EDUCATION, "degree_year", "int"),
    # --- Program engagement ---
    _Col(
        "nettrek_host_willing",
        "Willing to host NetTrek",
        "Engagement",
        _ENGAGEMENT,
        "nettrek_host_willing",
        "bool",
    ),
    _Col(
        "finance_conference_willing",
        "Willing to attend finance conference",
        "Engagement",
        _ENGAGEMENT,
        "finance_conference_willing",
        "bool",
    ),
    _Col(
        "mentor_willing",
        "Willing to mentor",
        "Engagement",
        _ENGAGEMENT,
        "mentor_willing",
        "bool",
    ),
    _Col(
        "company_event_sponsor_willing",
        "Willing to sponsor company event",
        "Engagement",
        _ENGAGEMENT,
        "company_event_sponsor_willing",
        "bool",
    ),
    _Col(
        "guest_speaker_willing",
        "Willing to guest speak",
        "Engagement",
        _ENGAGEMENT,
        "guest_speaker_willing",
        "bool",
    ),
    _Col(
        "help_at_event_willing",
        "Willing to help at events",
        "Engagement",
        _ENGAGEMENT,
        "help_at_event_willing",
        "bool",
    ),
    _Col(
        "case_competition_host_willing",
        "Willing to host case competition",
        "Engagement",
        _ENGAGEMENT,
        "case_competition_host_willing",
        "bool",
    ),
    _Col(
        "women_in_finance_mentor_willing",
        "Willing to mentor - Women in Finance",
        "Engagement",
        _ENGAGEMENT,
        "women_in_finance_mentor_willing",
        "bool",
    ),
    _Col(
        "hired_finance_intern",
        "Hired a finance intern",
        "Engagement",
        _ENGAGEMENT,
        "hired_finance_intern",
        "bool",
    ),
    _Col(
        "hired_finance_full_time",
        "Hired finance full-time",
        "Engagement",
        _ENGAGEMENT,
        "hired_finance_full_time",
        "bool",
    ),
    _Col("piff_donor", "PIFF donor", "Engagement", _ENGAGEMENT, "piff_donor", "bool"),
    _Col(
        "cfp_designation",
        "CFP designation",
        "Engagement",
        _ENGAGEMENT,
        "cfp_designation",
        "bool",
    ),
    _Col(
        "cfa_designation",
        "CFA designation",
        "Engagement",
        _ENGAGEMENT,
        "cfa_designation",
        "bool",
    ),
    _Col(
        "engagement_notes",
        "Engagement notes",
        "Engagement",
        _ENGAGEMENT,
        "engagement_notes",
    ),
]

_BY_KEY: dict[str, _Col] = {c.key: c for c in CATALOG}

# Default-checked columns: the everyday directory fields, deliberately excluding
# the most sensitive PII (BYU/Net id, birthday, free-text notes) so a casual
# export is FERPA-light by default. The user can still opt those in.
DEFAULT_SELECTED: list[str] = [
    "first_name",
    "last_name",
    "preferred_first_name",
    "graduation_year",
    "graduation_month",
    "current_employer",
    "current_title",
    "current_industry",
    "personal_email",
    "work_email",
    "city",
    "state",
    "linkedin_url",
]


def build_catalog() -> ExportColumnCatalog:
    return ExportColumnCatalog(
        columns=[ExportColumn(key=c.key, label=c.label, group=c.group) for c in CATALOG],
        default_selected=list(DEFAULT_SELECTED),
    )


def validate_columns(keys: list[str]) -> list[_Col]:
    """Resolve requested keys to catalog columns (in CSV/catalog order), de-duped.

    Raises ``ValueError`` if any key is unknown (the route maps that to a 422)."""
    unknown = [k for k in keys if k not in _BY_KEY]
    if unknown:
        raise ValueError(f"Unknown export column(s): {', '.join(sorted(set(unknown)))}.")
    requested = set(keys)
    # Emit in canonical catalog order regardless of the order keys were sent.
    return [c for c in CATALOG if c.key in requested]


def _filters_dict(filters: AlumniExportFilters) -> dict:
    """The filter kwargs for ``build_alumni_query`` — only the fields the caller
    actually set, so unset fields keep the query builder's defaults.

    When ``needs_survey`` is requested, derive the biennial-survey cutoff
    server-side (the body never carries a trusted "now") so the export hits the
    same DUE population the list view shows. The export route is full_access and
    up, which is exactly the admin tier allowed to use this filter."""
    out = filters.model_dump(exclude_unset=True, exclude={"sort"})
    if out.get("needs_survey"):
        out["survey_due_before"] = datetime.datetime.now(datetime.UTC) - SURVEY_CADENCE
    return out


async def count_matching(session: AsyncSession, filters: AlumniExportFilters) -> int:
    from sqlalchemy import func

    base = build_alumni_query(**_filters_dict(filters))
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    return int(total or 0)


# Leading characters Excel / LibreOffice treat as the start of a formula. A
# free-text field (employer, notes, linkedin_url, ...) starting with one of these
# would execute on open — classic CSV/formula injection. We neutralize by
# prefixing a tab (invisible, keeps the cell plain text) before such a value.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _fmt(value: object, kind: str) -> str:
    if value is None:
        return ""
    if kind == "bool":
        return "Yes" if value else "No"
    if kind == "date" and isinstance(value, datetime.date):
        return value.isoformat()
    text = str(value)
    # Only free-text (str-kind) cells can carry a formula payload; bool/date/int
    # are already normalized to safe tokens above.
    if text and text[0] in _FORMULA_LEAD:
        return "\t" + text
    return text


async def _load_side(
    session: AsyncSession,
    model,
    alumni_ids: list[int],
    *,
    latest_by: str | None = None,
    pk_attr: str | None = None,
) -> dict[int, object]:
    """Bulk-load one side table keyed by ``alumni_id``.

    With ``latest_by`` (education), keep the row with the greatest value of that
    column, breaking ties by the greatest ``pk_attr`` (newest). Otherwise keep
    the first row seen (the 1:1 tables have a single row per alumnus)."""
    if not alumni_ids:
        return {}
    rows = (
        (await session.execute(select(model).where(model.alumni_id.in_(alumni_ids))))
        .scalars()
        .all()
    )
    out: dict[int, object] = {}
    for row in rows:
        if latest_by is None:
            out.setdefault(row.alumni_id, row)
            continue
        current = out.get(row.alumni_id)
        if current is None or _rank(row, latest_by, pk_attr) > _rank(current, latest_by, pk_attr):
            out[row.alumni_id] = row
    return out


def _rank(row: object, latest_by: str, pk_attr: str | None) -> tuple:
    # Sort key for "latest" — Nones sort lowest. PK breaks ties newest-first.
    primary = getattr(row, latest_by, None)
    pk = (getattr(row, pk_attr, 0) or 0) if pk_attr else 0
    return (primary is not None, primary or 0, pk)


async def export_csv(
    session: AsyncSession,
    *,
    columns: list[_Col],
    filters: AlumniExportFilters,
    actor_user_id: int | None,
) -> str:
    """Build the CSV text for *columns* over every alumnus matching *filters*.

    Capped at :data:`MAX_EXPORT_ROWS`; callers should ``count_matching`` first to
    reject an over-cap export with a clear message."""
    # Reuse the exact filtered statement (correlated EXISTS conditions and all)
    # so the export population is identical to count_matching's — just ordered
    # and capped. Don't rebuild from .whereclause; that risks dropping query
    # structure for join/EXISTS-based filters.
    base = build_alumni_query(**_filters_dict(filters))
    stmt = base.order_by(Alumni.last_name.asc(), Alumni.alumni_id.asc()).limit(MAX_EXPORT_ROWS)
    alumni = (await session.execute(stmt)).scalars().all()
    ids = [a.alumni_id for a in alumni]

    groups = {c.source for c in columns}
    contact = await _load_side(session, AlumniContactInfo, ids) if _CONTACT in groups else {}
    career = await _load_side(session, CurrentEmployment, ids) if _CAREER in groups else {}
    engagement = (
        await _load_side(session, AlumniProgramEngagement, ids) if _ENGAGEMENT in groups else {}
    )
    education = (
        await _load_side(
            session, EducationHistory, ids, latest_by="degree_year", pk_attr="education_id"
        )
        if _EDUCATION in groups
        else {}
    )
    side = {
        _CONTACT: contact,
        _CAREER: career,
        _ENGAGEMENT: engagement,
        _EDUCATION: education,
    }

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([c.label for c in columns])
    for a in alumni:
        record_for = {src: rows.get(a.alumni_id) for src, rows in side.items()}
        row_out: list[str] = []
        for c in columns:
            source_row = a if c.source == _ALUMNI else record_for.get(c.source)
            value = getattr(source_row, c.attr, None) if source_row is not None else None
            row_out.append(_fmt(value, c.kind))
        writer.writerow(row_out)

    _audit_export(session, actor_user_id, filters, columns, len(alumni))
    await session.commit()
    return buffer.getvalue()


def _audit_export(
    session: AsyncSession,
    actor_user_id: int | None,
    filters: AlumniExportFilters,
    columns: list[_Col],
    row_count: int,
) -> None:
    """Disclosure audit for a bulk export — actor + a summary of WHAT left the
    system (active filters, chosen columns, row count). Never the data itself.

    The actor is always present on the API path (export is full_access). Guard
    anyway: a non-HTTP caller with no actor is logged as a warning rather than
    producing a silent, unaudited disclosure."""
    if actor_user_id is None:
        log.warning("Alumni export audit skipped: no actor (rows=%s)", row_count)
        return
    active = {
        k: v
        for k, v in filters.model_dump(exclude_unset=True).items()
        if v not in (None, "", False, [])
    }
    filt = ", ".join(f"{k}={v}" for k, v in sorted(active.items())) or "(no filters)"
    summary = f"rows={row_count}; columns={','.join(c.key for c in columns)}; filters: {filt}"
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="export_alumni",
            entity_type="alumni",
            entity_id=None,
            new_value=summary[:2000],
        )
    )

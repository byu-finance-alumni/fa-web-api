"""Opportunity-link service: the staff CRUD, the moderation queue, and the
PUBLIC survey write path (#441).

Alumni tell us about internships and jobs — at their own company or anywhere
else — and staff work the resulting list from a "Links" tab. There is no public
or student-facing surface; distribution stays manual.

WHY THIS IS NOT PART OF THE SURVEY RESPONSE PIPELINE. Two independent reasons,
both structural:

  * ``services.survey_responses`` is built on "one survey question maps to one
    real database column" — its ``_FIELDS`` keys are literally ``table.column``
    and ``apply_response`` setattrs them onto the alum's record. An opening has a
    url, a location, a role type, a deadline and a description of its own, and an
    alum can name several. There is no column to map to.
  * The response review queue is all-or-nothing per submission: ``apply_response``
    takes only a response id, so approving an address correction and approving
    whatever link rode along with it are the same click. Link moderation needs
    its own state and its own endpoints, which is what lives here.

Nothing in this module touches ``survey_responses``' apply/reject behaviour.

--------------------------------------------------------------------------------
SECURITY MODEL — the part to read before changing anything
--------------------------------------------------------------------------------

``submit_links`` is a PUBLIC write. The signed survey token is the whole
credential; there is no logged-in actor. That makes this the second publicly
writable surface in the app, and everything the survey whitelist learned the hard
way applies here:

  * **Validation runs on the write path, server-side, always.** Every writer in
    this module funnels through ``_validated_fields``, which calls the SAME
    ``validate_opportunity_url`` / text rules the Pydantic schemas call. It is
    belt-and-braces on the HTTP path and it is the ONLY check for any future
    non-HTTP caller. The prior High-severity finding in this codebase was exactly
    this shape: a rule that existed only on the staff schema, on a path that
    bypassed Pydantic entirely.
  * **Scheme gating, not hostname gating.** ``linkedin_url`` is safe because of a
    hostname allow-list. That defence cannot transfer — these links point at
    arbitrary employer sites by design. See ``validate_opportunity_url``'s
    docstring for what that does and does not buy.
  * **Human approval is not a technical control.** A pending link is a URL that a
    signed-in staff member will click. Scheme gating stops ``javascript:``;
    nothing here stops ``https://careers-acme-jobs.example/apply`` from being a
    credential-harvest page, and a reviewer cannot tell by looking. The approve
    step is a governance control that assigns ownership of that risk to a named
    person. It is not, and must not be described as, a filter that catches
    malicious links.
  * **Length caps are the persistence bound on a public write.** They are
    declared once on the model (``URL_MAX`` and friends), mirrored in the DB
    CHECKs, enforced by the schemas, and re-enforced here.

Every staff write is audited against the owning alumnus, so an opportunity link
shows up in that alum's Audit tab — the same convention ``services.notes`` uses.
The public submit writes no audit row, matching ``submit_response``: the pending
row IS the record of the submission, and there is no actor to attribute it to.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
from types import SimpleNamespace

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.employment import CurrentEmployment
from app.models.opportunity_link import (
    CITY_MAX,
    COMPANY_NAME_MAX,
    COUNTRY_MAX,
    STATE_MAX,
    OpportunityLink,
)
from app.models.user import User
from app.schemas.opportunity_link import (
    OpportunityLinkCreate,
    OpportunityLinkFilters,
    OpportunityLinkPage,
    OpportunityLinkRead,
    OpportunityLinkSubmitRequest,
    OpportunityLinkSubmitResult,
    OpportunityLinkUpdate,
    _validate_short_text,
    validate_application_deadline,
    validate_details,
    validate_opportunity_url,
)
from app.services import opportunity_link_alert

# The spreadsheet-formula lead characters, IMPORTED rather than redeclared:
# ``alumni_export`` already owns that list (#169) and a second copy is a second
# thing to remember to update. ``app/api/routes/events.py`` reaches into the
# same module for the same reason.
from app.services.alumni_export import _FORMULA_LEAD
from app.services.survey_email import LINK_DEAD_MESSAGE, verify_survey_token

log = logging.getLogger(__name__)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# --------------------------------------------------------------- validation ---


def _validated_fields(payload, *, check_deadline: bool = True) -> dict:
    """Re-run every field rule and return the values that may be written.

    Raises ``ValueError`` (which the routes surface as a 422) on the first bad
    value. This is NOT redundant with the Pydantic validators on the schemas: it
    is the same rule applied at the persistence boundary, so the guarantee is
    "nothing reaches this table unvalidated" rather than "nothing reaches this
    table unvalidated via HTTP". The distinction is the whole reason the survey's
    apply path had a High-severity finding — it wrote with a raw ``setattr`` and
    every schema validator silently did not run.

    Accepts either creation shape (staff or survey); they share a base, and that
    sameness is deliberate — a rule that is stricter on the staff path than the
    public one is a rule that does not exist.

    ``check_deadline`` is the ONE rule that is not unconditional, and the reason
    is that it is a rule about a value being SET, not about a value being stored.
    Creation and the public submit always pass it (default ``True``): a brand-new
    posting whose window has already closed is not a posting. ``update_link``
    passes ``False`` when the caller did not change the deadline, so a row whose
    deadline has since passed stays editable — otherwise a reviewer could not fix
    a typo in an expired posting, and the row would be frozen by the mere passage
    of time. Everything else in here stays unconditional: a value that was never
    acceptable does not become acceptable because it is already on the row.
    """
    url = validate_opportunity_url(payload.url)
    company = payload.company_name
    if payload.is_own_company:
        # The name is derived from the employment record at read time, so a typed
        # one must not be persisted alongside the flag (also the DB CHECK).
        company = None
    else:
        if not company or not company.strip():
            raise ValueError("A company name is required.")
        company = _validate_short_text(
            company, field="Company name", max_length=COMPANY_NAME_MAX
        )
    city = payload.location_city
    if city and city.strip():
        city = _validate_short_text(city, field="City", max_length=CITY_MAX)
    else:
        city = None
    state = payload.location_state
    if state and state.strip():
        state = _validate_short_text(state, field="State", max_length=STATE_MAX)
    else:
        state = None
    country = payload.location_country
    if country and country.strip():
        country = _validate_short_text(
            country, field="Country", max_length=COUNTRY_MAX
        )
    else:
        country = None
    deadline = payload.application_deadline
    if check_deadline:
        deadline = validate_application_deadline(deadline)
    details = payload.details
    if details and details.strip():
        details = validate_details(details)
    else:
        details = None
    return {
        "is_own_company": bool(payload.is_own_company),
        "company_name": company,
        "url": url,
        "location_city": city,
        "location_state": state,
        "location_country": country,
        "role_type": payload.role_type,
        "application_deadline": deadline,
        "details": details,
    }


# ------------------------------------------------------------- projection -----


def _display_name(
    first: str | None, preferred: str | None, last: str | None, alumni_id: int
) -> str:
    name = " ".join(p for p in (preferred or first, last) if p).strip()
    return name or f"Alumni #{alumni_id}"


def _user_name(user: User | None) -> str | None:
    if user is None:
        return None
    name = " ".join(p for p in (user.first_name, user.last_name) if p).strip()
    return name or user.email


def _to_read(
    link: OpportunityLink,
    *,
    submitted_by: str | None,
    employer: str | None,
    reviewed_by: str | None,
) -> OpportunityLinkRead:
    """Project a row onto the read shape, resolving the company display name.

    ``is_own_company`` rows carry no stored name — the employer is looked up now,
    so an alum who changes jobs does not leave a stale label behind. A ``None``
    result (ticked the box, no employment row on file) is passed through rather
    than papered over with a placeholder: the list shows a dash, and the gap is
    visible to the staff member who could fix it.
    """
    return OpportunityLinkRead(
        opportunity_link_id=link.opportunity_link_id,
        alumni_id=link.alumni_id,
        submitted_by=submitted_by,
        is_own_company=link.is_own_company,
        company_name=employer if link.is_own_company else link.company_name,
        url=link.url,
        location_city=link.location_city,
        location_state=link.location_state,
        location_country=link.location_country,
        role_type=link.role_type,
        application_deadline=link.application_deadline,
        details=link.details,
        status=link.status,
        source=link.source,
        submitted_at=link.submitted_at,
        reviewed_by=reviewed_by,
        reviewed_at=link.reviewed_at,
    )


async def _project(
    session: AsyncSession, links: list[OpportunityLink]
) -> list[OpportunityLinkRead]:
    """Resolve the alumni names, employers and reviewer names for a PAGE of rows.

    Three bounded queries keyed on the page's ids, never one per row — the list
    is the hot read and an N+1 here would scale with the table.
    """
    if not links:
        return []
    alumni_ids = {link.alumni_id for link in links}
    reviewer_ids = {
        uid
        for link in links
        for uid in (link.reviewed_by_user_id,)
        if uid is not None
    }

    alumni_rows = (
        (
            await session.execute(
                select(Alumni).where(Alumni.alumni_id.in_(alumni_ids))
            )
        )
        .scalars()
        .all()
    )
    names = {
        a.alumni_id: _display_name(
            a.first_name, a.preferred_first_name, a.last_name, a.alumni_id
        )
        for a in alumni_rows
    }
    employment_rows = (
        (
            await session.execute(
                select(CurrentEmployment).where(
                    CurrentEmployment.alumni_id.in_(alumni_ids)
                )
            )
        )
        .scalars()
        .all()
    )
    employers = {e.alumni_id: e.current_employer for e in employment_rows}

    reviewers: dict[int, str | None] = {}
    if reviewer_ids:
        reviewer_rows = (
            (await session.execute(select(User).where(User.user_id.in_(reviewer_ids))))
            .scalars()
            .all()
        )
        reviewers = {u.user_id: _user_name(u) for u in reviewer_rows}

    return [
        _to_read(
            link,
            submitted_by=names.get(link.alumni_id),
            employer=employers.get(link.alumni_id),
            reviewed_by=reviewers.get(link.reviewed_by_user_id),
        )
        for link in links
    ]


# ------------------------------------------------------------------ audit -----


def _audit(
    session: AsyncSession,
    actor_user_id: int | None,
    action: str,
    link: OpportunityLink,
    *,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    """Record a staff write in the FERPA audit trail, against the OWNING alumnus
    so it surfaces on that alum's profile Audit tab (the ``services.notes``
    convention).

    No-ops with a warning when there is no actor. That is the public survey path,
    which has no logged-in user by design — the pending row itself is the record
    of the submission, exactly as ``submit_response`` stages one without an audit
    row. Never logs the URL or the details at warning level.
    """
    if actor_user_id is None:
        log.warning(
            "Opportunity-link audit skipped: no actor for action=%s link=%s",
            action,
            link.opportunity_link_id,
        )
        return
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type=action,
            entity_type="alumni",
            entity_id=link.alumni_id,
            field_name="opportunity_link",
            old_value=old_value,
            new_value=new_value,
        )
    )


def _summary(link: OpportunityLink) -> str:
    """A short, audit-safe description of a link: what it points at and for what.

    The URL is included deliberately — it is the whole substance of the record,
    and an audit row that omits it could not answer "what did we approve?".
    Truncated to stay well inside the audit column.
    """
    company = link.company_name or ("own company" if link.is_own_company else "—")
    return f"{company} | {link.role_type} | {link.url}"[:1000]


# ------------------------------------------------------------------- reads ----


def _filtered(stmt: Select, filters: OpportunityLinkFilters) -> Select:
    """Apply the Links list's filters to a SELECT over ``opportunity_links``.

    ⚠️ THE ONLY PLACE THESE PREDICATES ARE WRITTEN. The list, its count, and the
    CSV export all reach SQL through here, via :func:`build_population_query`,
    because this repo's recurring export bug is precisely a second copy of these
    predicates that drifts from the first. Adding a filter means adding a field to
    ``OpportunityLinkFilters`` and a branch here — never a ``where`` clause at a
    call site.

    ``company`` and ``search`` are LIKE predicates over caller text, so both go
    through ``_like_term`` — a bare ``%`` in the box must not turn into a
    match-everything wildcard (the escaping trap ``tests/test_like_escape.py``
    pins elsewhere in this codebase).

    ``company`` matches the STORED name only. A row whose name is derived from
    the alum's employer is matched through the joined employment column instead —
    see :func:`build_population_query`, which adds that leg.
    """
    if filters.status:
        stmt = stmt.where(OpportunityLink.status == filters.status)
    if filters.role_type:
        stmt = stmt.where(OpportunityLink.role_type == filters.role_type)
    if filters.company:
        term = _like_term(filters.company)
        stmt = stmt.where(
            or_(
                OpportunityLink.company_name.ilike(term, escape="\\"),
                CurrentEmployment.current_employer.ilike(term, escape="\\"),
            )
        )
    if filters.search:
        term = _like_term(filters.search)
        stmt = stmt.where(
            or_(
                OpportunityLink.company_name.ilike(term, escape="\\"),
                CurrentEmployment.current_employer.ilike(term, escape="\\"),
                OpportunityLink.details.ilike(term, escape="\\"),
                OpportunityLink.location_city.ilike(term, escape="\\"),
                OpportunityLink.location_state.ilike(term, escape="\\"),
                # Country is part of "location" for search, same as city/state —
                # leaving it out would make a non-US posting unfindable by the one
                # word that identifies where it is.
                OpportunityLink.location_country.ilike(term, escape="\\"),
                OpportunityLink.url.ilike(term, escape="\\"),
            )
        )
    # DATE RECEIVED (#771). Bounded against ``submitted_at``, the timestamp the
    # row was written with — NOT ``application_deadline``, which is a fact about
    # the job and not about when they gave it to us.
    #
    # ⚠️ THE END BOUND IS WIDENED TO THE END OF THE DAY. ``submitted_at`` is a
    # timestamptz and the filter is a bare date, so a naive
    # ``submitted_at <= 2026-08-28`` compares against midnight and silently drops
    # everything that arrived during the day the user asked for — the report would
    # be missing its most recent postings, which is the exact failure #771 is
    # about. Same widening ``GET /audit`` already does.
    if filters.submitted_from is not None:
        stmt = stmt.where(OpportunityLink.submitted_at >= _day_start(filters.submitted_from))
    if filters.submitted_to is not None:
        stmt = stmt.where(OpportunityLink.submitted_at <= _day_end(filters.submitted_to))
    return stmt


def _day_start(day: datetime.date) -> datetime.datetime:
    """The first instant of ``day`` in UTC."""
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.UTC)


def _day_end(day: datetime.date) -> datetime.datetime:
    """The last instant of ``day`` in UTC — see the note in :func:`_filtered`."""
    return datetime.datetime.combine(day, datetime.time.max, tzinfo=datetime.UTC)


def build_population_query(filters: OpportunityLinkFilters) -> Select:
    """The SELECT that defines WHICH LINKS a filter set matches. Unordered,
    unpaginated, no projection — just the population.

    ⚠️ THE LIST AND THE EXPORT BOTH START HERE, and that is what makes them
    provably the same set rather than two implementations that agree today. The
    list adds ``ORDER BY``/``LIMIT``/``OFFSET`` and a COUNT over the same
    predicates; the export adds only the ordering. Neither adds a predicate.
    ``tests/test_opportunity_link_export_parity.py`` compiles both and asserts
    identical SQL and identical binds.

    LEFT JOINs ``current_employment`` so an ``is_own_company`` row is filterable
    and searchable by the employer name it displays under — otherwise "filter by
    company" would silently miss exactly the rows the feature is named after. The
    join is part of the population definition, not of the list's presentation, so
    it lives here: an export that dropped it would return a different set for the
    same ``company=`` box.
    """
    return _filtered(
        select(OpportunityLink).outerjoin(
            CurrentEmployment,
            CurrentEmployment.alumni_id == OpportunityLink.alumni_id,
        ),
        filters,
    )


def build_population_count(filters: OpportunityLinkFilters) -> Select:
    """The COUNT matching :func:`build_population_query`, same predicates."""
    return _filtered(
        select(func.count(OpportunityLink.opportunity_link_id)).outerjoin(
            CurrentEmployment,
            CurrentEmployment.alumni_id == OpportunityLink.alumni_id,
        ),
        filters,
    )


# The list's ordering, applied identically by the export so a CSV reads in the
# same order as the screen it was launched from. Newest first, id as the
# tie-break so a page boundary is deterministic when two rows share a timestamp.
def _ordered(stmt: Select) -> Select:
    return stmt.order_by(
        OpportunityLink.submitted_at.desc(),
        OpportunityLink.opportunity_link_id.desc(),
    )


def _like_term(value: str) -> str:
    """A contains-match LIKE pattern with the caller's wildcards neutralised."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


async def list_links(
    session: AsyncSession,
    filters: OpportunityLinkFilters,
    *,
    limit: int = 50,
    offset: int = 0,
) -> OpportunityLinkPage:
    """The staff Links tab: a filtered, paginated page of links, newest first.

    Takes the SAME ``OpportunityLinkFilters`` the CSV export takes and runs the
    SAME :func:`build_population_query` over it. Paging is the only difference
    between what this returns and what the export writes.
    """
    total = await session.scalar(build_population_count(filters)) or 0
    rows = (
        (
            await session.execute(
                _ordered(build_population_query(filters)).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return OpportunityLinkPage(
        items=await _project(session, list(rows)),
        total=total,
        limit=limit,
        offset=offset,
    )


# ------------------------------------------------------------------ export ----

#: Ceiling on one CSV. The table is small today, but an export builds the whole
#: file in memory before the response is written and this runs on a serverless
#: function with a hard body cap (the 4.5 MB limit the memory index records as
#: "the fake CORS error"). Over the cap the route asks the caller to narrow the
#: dates rather than truncating — a report silently missing its tail is worse
#: than a report that refuses.
MAX_EXPORT_ROWS = 10_000

#: The CSV header, in order. ONE definition, used to write the file and asserted
#: by the parity test, so "the export grew a column" cannot happen by accident.
EXPORT_COLUMNS: tuple[str, ...] = (
    "Link ID",
    "Date received",
    "Submitted by",
    "Alumni ID",
    "Company",
    "Own company",
    "Role type",
    "URL",
    "City",
    "State",
    "Country",
    "Application deadline",
    "Details",
    "Status",
    "Source",
    "Reviewed by",
    "Reviewed at",
)

def _cell(value: object) -> str:
    """Render one CSV cell, neutralising spreadsheet formula injection.

    ⚠️ EVERY free-text column in this export is PUBLIC INPUT — company,
    details, url and location all arrive on the token-gated survey path, and
    a pending row has not been moderated yet. A cell opening with ``=`` is
    executed by Excel and Sheets when the file is opened, so the value is
    prefixed with a tab, exactly as ``alumni_export._fmt`` does. Same threat,
    same fix, deliberately not a second cleverer one.

    Timestamps are rendered in UTC with the zone spelled out rather than as a
    bare ISO string: this is the "date received" column the whole report is
    named after, and a reader comparing it to a screenshot needs to know which
    clock it is on.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, datetime.datetime):
        return value.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    if isinstance(value, datetime.date):
        return value.isoformat()
    text = str(value)
    if text and text[0] in _FORMULA_LEAD:
        return "\t" + text
    return text


def _export_row(link: OpportunityLinkRead) -> list[str]:
    """One CSV line, built from the SAME ``OpportunityLinkRead`` projection the
    list renders — so a resolved "my company" employer name, and a NULL that
    shows as a dash on screen, mean the same thing in the file."""
    return [
        _cell(link.opportunity_link_id),
        _cell(link.submitted_at),
        _cell(link.submitted_by),
        _cell(link.alumni_id),
        _cell(link.company_name),
        _cell(link.is_own_company),
        _cell(link.role_type),
        _cell(link.url),
        _cell(link.location_city),
        _cell(link.location_state),
        _cell(link.location_country),
        _cell(link.application_deadline),
        _cell(link.details),
        _cell(link.status),
        _cell(link.source),
        _cell(link.reviewed_by),
        _cell(link.reviewed_at),
    ]


async def count_links(
    session: AsyncSession, filters: OpportunityLinkFilters
) -> int:
    """How many links a filter set matches. The export's pre-flight, and the same
    count the list's ``total`` reports."""
    return int(await session.scalar(build_population_count(filters)) or 0)


async def export_csv(
    session: AsyncSession,
    filters: OpportunityLinkFilters,
    *,
    actor_user_id: int,
) -> str:
    """The dated report (#771): EXACTLY the filtered list, as CSV.

    ⚠️ SAME OBJECT, SAME QUERY, NO PAGING. It takes the identical
    ``OpportunityLinkFilters`` the list took, runs the identical
    :func:`build_population_query`, applies the identical ordering, and projects
    each row through the identical ``_project``/``OpportunityLinkRead`` the list
    renders. The only thing it drops is ``LIMIT``/``OFFSET``, because a report is
    the whole set and a page is not. There is no second predicate anywhere in
    this function, which is the property
    ``tests/test_opportunity_link_export_parity.py`` pins.

    Audit-logged as ``export_opportunity_links`` with the row count and the
    filters that produced it — WHAT left the system and under which selection,
    never the rows themselves. Mirrors the event-attendee and alumni exports.
    """
    rows = (
        (await session.execute(_ordered(build_population_query(filters))))
        .scalars()
        .all()
    )
    items = await _project(session, list(rows))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(EXPORT_COLUMNS))
    for item in items:
        writer.writerow(_export_row(item))

    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="export_opportunity_links",
            entity_type="opportunity_link",
            entity_id=None,
            new_value=f"rows={len(items)}; {describe_filters(filters)}",
        )
    )
    await session.commit()
    return buffer.getvalue()


def describe_filters(filters: OpportunityLinkFilters) -> str:
    """The applied filters as one audit-safe string.

    Every set value, none of the unset ones — so the trail records the SELECTION
    that produced a disclosure and can be replayed, which is the whole reason the
    read is audited. Free-text values (``company``, ``search``) are included:
    they are what the staff member typed, not alumni data.
    """
    pairs = (
        ("status", filters.status),
        ("role_type", filters.role_type),
        ("company", filters.company),
        ("q", filters.search),
        (
            "submitted_from",
            filters.submitted_from.isoformat() if filters.submitted_from else None,
        ),
        (
            "submitted_to",
            filters.submitted_to.isoformat() if filters.submitted_to else None,
        ),
    )
    return ";".join(f"{k}={v}" for k, v in pairs if v is not None)


async def get_link(session: AsyncSession, link_id: int) -> OpportunityLinkRead:
    link = await _load(session, link_id)
    items = await _project(session, [link])
    return items[0]


async def _load(session: AsyncSession, link_id: int) -> OpportunityLink:
    link = await session.get(OpportunityLink, link_id)
    if link is None:
        raise NotFoundError(f"Opportunity link {link_id} not found.")
    return link


# ------------------------------------------------------------- staff writes ---


async def create_link(
    session: AsyncSession,
    payload: OpportunityLinkCreate,
    actor_user_id: int | None,
) -> OpportunityLinkRead:
    """Staff manual entry. Lands ``approved``.

    A staff member typing a link in IS the review — there is no second person to
    send it to, and a manual entry that sat in a pending queue would just be work
    the same person has to come back and click. The reviewer is stamped as the
    creator so the audit trail still names who vouched for it.

    404s if the alumnus does not exist: an opportunity with no provenance is not
    a record worth keeping.
    """
    alum = await session.get(Alumni, payload.alumni_id)
    if alum is None:
        raise NotFoundError(f"Alumni {payload.alumni_id} not found.")
    fields = _validated_fields(payload)
    link = OpportunityLink(
        alumni_id=payload.alumni_id,
        status="approved",
        source="staff",
        created_by_user_id=actor_user_id,
        reviewed_by_user_id=actor_user_id,
        reviewed_at=_now(),
        **fields,
    )
    session.add(link)
    await session.flush()
    _audit(session, actor_user_id, "add_opportunity_link", link, new_value=_summary(link))
    await session.commit()
    await session.refresh(link)
    items = await _project(session, [link])
    return items[0]


async def update_link(
    session: AsyncSession,
    link_id: int,
    payload: OpportunityLinkUpdate,
    actor_user_id: int | None,
) -> OpportunityLinkRead:
    """Staff edit of an existing link.

    Only the fields present in the request body are touched (``model_fields_set``
    distinguishes "omitted" from "sent as null"), and the merged result is
    re-validated as a whole — so an edit cannot leave the row in a state the
    create path would have refused, e.g. flipping ``is_own_company`` off without
    supplying a name.

    Editing does NOT change ``status``. Moderation has its own endpoints; a staff
    member fixing a typo in a pending link must not silently approve it.

    ⚠️ THE DEADLINE RULE IS CONDITIONAL HERE, and that is the subtle part. Create
    and the public submit refuse a deadline in the past outright. On an edit the
    same rule is applied only when the deadline is actually CHANGING: a posting
    whose deadline has since passed must stay editable, or a reviewer could never
    correct a wrong URL on an expired listing and the row would be frozen by
    nothing but the calendar. Setting a NEW past deadline is still refused.
    """
    link = await _load(session, link_id)
    sent = payload.model_fields_set

    def merged(name: str):
        """The value a field should end up with: the submitted one if the client
        sent the key at all, otherwise what is already stored."""
        return getattr(payload, name) if name in sent else getattr(link, name)

    def merged_required(name: str):
        """Same, for the three fields that cannot be nulled. ``null`` on one of
        these is read as "leave it alone" rather than as a clear, because the
        column is NOT NULL and there is no sensible empty value for a url, a role
        type, or the own-company flag."""
        value = getattr(payload, name) if name in sent else None
        return getattr(link, name) if value is None else value

    candidate = SimpleNamespace(
        is_own_company=merged_required("is_own_company"),
        company_name=merged("company_name"),
        url=merged_required("url"),
        location_city=merged("location_city"),
        location_state=merged("location_state"),
        location_country=merged("location_country"),
        role_type=merged_required("role_type"),
        application_deadline=merged("application_deadline"),
        details=merged("details"),
    )

    # "The deadline must not be in the past" is checked ONLY when the deadline is
    # actually moving. Two comparisons, both necessary: the key must have been
    # sent at all (`model_fields_set`, so an omitted deadline is untouched), AND
    # the sent value must differ from the stored one (so a client that PATCHes the
    # whole object back, deadline included, is not refused for re-sending what is
    # already there). Clearing it to `null` differs from a stored date and so
    # counts as a change — and `validate_application_deadline(None)` allows it,
    # because "no closing date" is a real answer.
    deadline_changed = (
        "application_deadline" in sent
        and payload.application_deadline != link.application_deadline
    )

    before = _summary(link)
    fields = _validated_fields(candidate, check_deadline=deadline_changed)
    for key, value in fields.items():
        setattr(link, key, value)
    link.updated_at = _now()
    _audit(
        session,
        actor_user_id,
        "update_opportunity_link",
        link,
        old_value=before,
        new_value=_summary(link),
    )
    await session.commit()
    await session.refresh(link)
    items = await _project(session, [link])
    return items[0]


def _stage_delete_audit(
    session: AsyncSession, link: OpportunityLink, actor_user_id: int | None
) -> None:
    """Snapshot a link into the audit trail, immediately before it is removed.

    The snapshot is the WHOLE point of this helper existing: once the row is
    gone, the audit entry is the only thing left that can answer "what did we
    delete?". It is taken while the object is still populated — after
    ``session.delete`` the instance is expired and ``_summary`` would read
    detached attributes. Shared by the single delete and the bulk delete so the
    two produce identical trails; a bulk delete is N ordinary deletions in the
    log, not one opaque event.
    """
    _audit(
        session,
        actor_user_id,
        "delete_opportunity_link",
        link,
        old_value=_summary(link),
    )


async def delete_link(
    session: AsyncSession, link_id: int, actor_user_id: int | None
) -> None:
    """Delete a link. The row is snapshotted into the audit trail first, so a
    later review can still answer what was removed and by whom."""
    link = await _load(session, link_id)
    _stage_delete_audit(session, link, actor_user_id)
    await session.delete(link)
    await session.commit()


async def delete_links(
    session: AsyncSession, link_ids: list[int], actor_user_id: int | None
) -> tuple[list[int], list[int]]:
    """Delete several links at once. Returns ``(deleted_ids, missing_ids)``.

    BEST-EFFORT BY DESIGN. An id that no longer resolves is REPORTED, not raised
    on: the caller multi-selected from a list their browser rendered seconds ago,
    and the commonest reason an id is stale is that the row is already gone —
    i.e. already in the state they asked for. Refusing the batch over that would
    make the button less reliable the more rows you select, and would leave the
    caller to work out by hand which id was the problem. Both lists come back
    sorted so the response is stable regardless of the order the ids were sent.

    ONE TRANSACTION, though. Best-effort is about which ids are *attempted*, not
    about half-finishing: every resolvable row and every one of its audit rows
    commit together, so the trail can never disagree with the table.

    Duplicate ids collapse — one row, one audit entry, one entry in
    ``deleted_ids``. The rows are fetched in a single ``IN`` query rather than
    one ``get`` per id; the caller's list is capped
    (``MAX_LINKS_PER_BULK_DELETE``) so that query is bounded.
    """
    wanted = sorted(set(link_ids))
    if not wanted:
        return [], []
    rows = (
        (
            await session.execute(
                select(OpportunityLink).where(
                    OpportunityLink.opportunity_link_id.in_(wanted)
                )
            )
        )
        .scalars()
        .all()
    )
    found = {link.opportunity_link_id: link for link in rows}
    # Snapshot EVERY row before deleting ANY of them. Interleaving would still be
    # correct today, but this keeps "the audit row is written from a live object"
    # true by construction rather than by delete-ordering luck.
    for link_id in wanted:
        link = found.get(link_id)
        if link is not None:
            _stage_delete_audit(session, link, actor_user_id)
    for link_id in wanted:
        link = found.get(link_id)
        if link is not None:
            await session.delete(link)
    await session.commit()
    deleted_ids = [i for i in wanted if i in found]
    missing_ids = [i for i in wanted if i not in found]
    return deleted_ids, missing_ids


# -------------------------------------------------------------- moderation ----


async def moderate_link(
    session: AsyncSession,
    link_id: int,
    *,
    approve: bool,
    actor_user_id: int | None,
) -> OpportunityLinkRead:
    """Approve or reject a link, stamping who decided and when.

    Deliberately NOT restricted to ``pending`` rows. A staff-entered link that
    turns out to be dead or wrong should be rejectable without deleting it (the
    row is the record that we once circulated it), and a rejected submission
    should be recoverable if the rejection was a mistake. Every transition is
    audited with both the old and the new status, so the history is legible.

    ⚠️ APPROVING IS NOT A SECURITY CHECK. It records that a named person took
    responsibility for a link. It does not, and cannot, establish that the URL is
    not a phishing page — see ``validate_opportunity_url``.
    """
    link = await _load(session, link_id)
    old_status = link.status
    link.status = "approved" if approve else "rejected"
    link.reviewed_by_user_id = actor_user_id
    link.reviewed_at = _now()
    link.updated_at = _now()
    _audit(
        session,
        actor_user_id,
        "approve_opportunity_link" if approve else "reject_opportunity_link",
        link,
        old_value=old_status,
        new_value=f"{link.status} | {_summary(link)}",
    )
    await session.commit()
    await session.refresh(link)
    items = await _project(session, [link])
    return items[0]


# --------------------------------------------------- public survey write ------


async def submit_links(
    session: AsyncSession, token: str, payload: OpportunityLinkSubmitRequest
) -> OpportunityLinkSubmitResult:
    """PUBLIC (token-gated, no login): stage an alum's opportunity links.

    The alumnus is resolved from the SIGNED TOKEN, never from the body — a
    submitter cannot attach links to somebody else's record. An invalid, tampered
    or expired token, an archived alum and a deleted alum all produce the SAME
    404, exactly as the other respond routes do: distinguishing them would
    confirm to a prober that a token was once a real alum's credential.

    Every link lands ``pending``. Nothing here is visible as an approved link
    until a staff member moderates it.

    The whole batch is validated BEFORE anything is added to the session, so a
    submission is all-or-nothing: an alum never has to guess which of their five
    entries landed. A bad value raises ``ValueError``, which the route surfaces as
    a 422 rather than silently dropping the field — the opposite disposition from
    the survey field whitelist, and correct here for the reason given in
    ``_validate_short_text``: there is no existing good value to protect.
    """
    alumni_id = verify_survey_token(token)
    if alumni_id is None:
        raise NotFoundError(LINK_DEAD_MESSAGE)
    alum = await session.get(Alumni, alumni_id)
    if alum is None or alum.archived:
        raise NotFoundError(LINK_DEAD_MESSAGE)

    validated = [_validated_fields(item) for item in payload.links]
    staged: list[OpportunityLink] = []
    for fields in validated:
        row = OpportunityLink(
            alumni_id=alumni_id,
            status="pending",
            source="survey",
            # No actor: the public path has no logged-in user. `created_by_user_id`
            # stays NULL and `source='survey'` is what says the text came from
            # outside — do not backfill either with the alum's user record
            # (alumni do not have one).
            created_by_user_id=None,
            **fields,
        )
        staged.append(row)
        session.add(row)
    await session.commit()

    # ------------------------------------------------------------------ #771 --
    # TELL SOMEBODY. Before this, a survey posting landed pending and waited for a
    # staff member to think to open the Links tab; the owner's ask was "so we
    # never miss".
    #
    # ⚠️ AFTER THE COMMIT, AND IT CANNOT FAIL THE SUBMISSION. The rows above are
    # already durable, and `notify_new_links` swallows every exception, is
    # time-boxed, and returns immediately when no alerting channel is configured
    # (which is the case in tests, CI and every preview deployment). A Slack
    # outage or a Resend rejection costs one missed message; it must never cost an
    # alum their posting, and it must never turn a 200 into a 500 on a public,
    # token-gated endpoint. See app/services/opportunity_link_alert.py, including
    # why this is awaited rather than fired and forgotten, and why the message
    # deliberately carries no name, no company and no URL.
    await opportunity_link_alert.notify_new_links(staged)
    return OpportunityLinkSubmitResult(staged=True, link_count=len(validated))

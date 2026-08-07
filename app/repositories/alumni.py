"""Data access for alumni core records.

Thin query layer — business rules (soft-delete, manual-edit stamping) live in
the service. ``build_alumni_query`` is a pure function (no IO) so the filter
logic can be unit-tested by compiling the statement.

Free-text search (``q``) covers the alumni core table (names, external ids,
designations) AND, since #620, the related employment record — current employer,
title, city, state, country and industry, plus past employers — through
correlated EXISTS subqueries. See ``app.core.search_terms`` (parsing) and
``app.repositories.alumni_search`` (matching + ranking SQL).
"""

import datetime

from sqlalchemy import Select, and_, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import email_reach
from app.core.dropdowns import (
    DESIGNATION_NEGATIVES,
    EMPLOYER_NOT_APPLICABLE_BY_LOWER,
    WHEEL_INDUSTRIES,
    engagement_flag_for_tag,
)
from app.core.search_terms import parse_free_text
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.crm import Interaction, Survey
from app.models.duplicate import DuplicateCandidate
from app.models.employment import (
    CurrentEmployment,
    EducationHistory,
    EmploymentHistory,
)
from app.models.engagement import AlumniProgramEngagement, FinanceSocietyLeadership
from app.models.event import Event, EventAttendance
from app.models.tags import AlumniStatusLabel, AlumniTag, StatusLabel, Tag
from app.repositories.alumni_search import q_conditions, relevance_expression
from app.utils.sql import escape_like

# Biennial survey cadence (#160): an alumnus is DUE for surveying when their
# most-recent completed survey is older than this, or they have never completed
# one. Expressed in days so the cutoff arithmetic stays in stdlib; a 2-year
# staleness window is insensitive to leap-year calendar drift. Callers compute
# the cutoff as ``now() - SURVEY_CADENCE`` and pass it as ``survey_due_before``.
SURVEY_CADENCE = datetime.timedelta(days=365 * 2)

# The 14 dashboard "wheel" industries. Powers the ``industry_group`` filters so
# the alumni-list drill-downs match the dashboard bars exactly:
#   * ``unknown`` — no current-employment row names a (non-blank) primary industry,
#                   OR the primary industry is the explicit "Unknown" value (#295),
#                   which the dashboard merges into the same "Unknown" bar.
#   * ``other``   — has a primary industry, but it isn't one of the 14 wheel
#                   industries (literal "Other", a non-wheel finance value like
#                   Law/Corporate Banking, "Military" (#608), or any non-canonical
#                   value) — EXCLUDING the two values the dashboard breaks out
#                   into their own buckets: "Graduate Student" (#294, own bar) and
#                   "Unknown" (#295, merged into the Unknown bar).
# Lower-cased for a case-insensitive DB comparison (industries are stored as
# free-text varchars, so casing can drift on import).
_FINANCE_INDUSTRIES_LOWER: frozenset[str] = frozenset(
    i.lower() for i in WHEEL_INDUSTRIES
)
# Non-wheel values the dashboard pulls OUT of the "Other" bucket into their own
# bars, so ``industry_group=other`` must exclude them and their own drill-downs
# (exact ``industry=Graduate Student`` / ``industry_group=unknown``) own them.
_GRADUATE_STUDENT_LOWER = "graduate student"
_EXPLICIT_UNKNOWN_LOWER = "unknown"
# "Military" (#608) is NOT one of those. Jake chose to keep the industry chart
# about finance sectors, so Military simply FOLDS INTO the "Other" bar like every
# other non-wheel value — no bar of its own, and therefore no exclusion here.
# The constant exists only for the primary-OR-secondary search widening below.
_MILITARY_LOWER = "military"
_OTHER_EXCLUDED_LOWER: frozenset[str] = frozenset(
    {_GRADUATE_STUDENT_LOWER, _EXPLICIT_UNKNOWN_LOWER}
)


# --- missing employer (#608) --------------------------------------------------


def has_employer_or_not_applicable():
    """Correlated predicate: an employer is on file, OR none is expected.

    THE shared definition of "not missing an employer", used by the alumni-list /
    export ``missing_employer`` filter AND (imported) by the dashboard summary +
    Data-quality counts, so the KPI, the drill-down list and the CSV export can
    never describe different populations — the parity bug class this codebase
    keeps hitting.

    ``employment_status`` is a free-text varchar with no write validation, so the
    status comparison is on the trimmed, lower-cased value. A NULL status is NOT
    exempt (``coalesce`` to ''), because a blank status tells us nothing about
    whether the blank employer was intentional.
    """
    has_employer = (
        select(CurrentEmployment.current_employment_id)
        .where(
            CurrentEmployment.alumni_id == Alumni.alumni_id,
            CurrentEmployment.current_employer.is_not(None),
        )
        .exists()
    )
    not_applicable = func.lower(
        func.trim(func.coalesce(Alumni.employment_status, ""))
    ).in_(sorted(EMPLOYER_NOT_APPLICABLE_BY_LOWER))
    return or_(has_employer, not_applicable)


# Free-text ``q`` (#281, #404, #620). Tokenization, filler removal, preposition
# routing and normalization live in ``app.core.search_terms``; the matching and
# ranking SQL lives in ``app.repositories.alumni_search``. Both are pure, so
# ``build_alumni_query`` stays IO-free and the export can compile it without a
# database session.
#
# The semantics that were true before and are still true:
#   * Names are stored atomized (first_name 'Kyle', last_name 'Marsh'), so the
#     query is split into tokens and the tokens AND-ed, each token OR-ing across
#     every searchable column — "Kyle Marsh" matches 'Kyle' on first_name and
#     'Marsh' on last_name (#281), and "Marsh, Kyle" is the same query.
#   * Every token searches birth_name too, so a maiden-name lookup works (#216).
#   * other_designations is free text that legitimately CONTAINS spaces
#     ("Series 7"), so it stays in the per-token set (#404).
#
# What #620 added: the searchable columns now also include the current employer,
# title, city, state, country and industry plus past employers; every comparison
# is on a normalized (case-, accent- and punctuation-free) form so a missing
# space or a typo stops being a way to miss a row; filler words are dropped
# instead of becoming required matches; and "at ..." / "in ..." route the words
# that follow them to a narrower field group.


def _as_values(value: "str | list[str] | None") -> list[str]:
    """Normalize a filter input to a clean list of non-empty strings.

    Accepts a single string (legacy / single deep-link), a list (multi-select),
    or None. Lets every text filter support multi-select via one repeated query
    param while a single-value link (``?employer=X``) keeps working unchanged.
    """
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [v for v in value if v and v.strip()]


def _ilike_any(column, values: list[str]):
    """OR of case-insensitive exact matches (literal %/_ escaped) for `values`."""
    return or_(*[column.ilike(escape_like(v), escape="\\") for v in values])


def has_status_label_exists(labels: "str | list[str]"):
    """Correlated EXISTS: the alumnus carries ANY of these status labels.

    Public because outbound sends need it in BOTH polarities — ``~`` of it to
    EXCLUDE suppressed alumni from a send (what ``suppress_labels`` does below),
    and the plain form to COUNT how many were suppressed so the survey console
    can report that number separately from "we have no address for them" (#392).
    Sharing one definition keeps the excluded set and the reported set identical.
    """
    return (
        select(AlumniStatusLabel.alumni_status_label_id)
        .join(
            StatusLabel,
            StatusLabel.status_label_id == AlumniStatusLabel.status_label_id,
        )
        .where(
            AlumniStatusLabel.alumni_id == Alumni.alumni_id,
            _ilike_any(StatusLabel.status_label_name, _as_values(labels)),
        )
        .exists()
    )


# The negatives, lower-cased and pre-sorted for a stable SQL literal, with ""
# folded in so a blank/whitespace-only cell is excluded by the same NOT IN.
_DESIGNATION_NEGATIVES_SQL: tuple[str, ...] = tuple(
    sorted(DESIGNATION_NEGATIVES | {""})
)


def _holds_designation(column):
    """Predicate: a designation flag column says the alumnus HOLDS it.

    The ``cfp/cfa/cpa_designation`` columns on ``alumni_program_engagement`` are
    free-text flags, not booleans (#404), and nothing enforces the "marker string
    or NULL" convention — the intake sheet's columns are headed "(Yes/No)", so a
    human can type "No" straight into one. A presence test alone would then count
    that alumnus as holding the certification, so this excludes the negatives too.

    SQL-side twin of :func:`app.core.dropdowns.holds_designation`, built from the
    SAME ``DESIGNATION_NEGATIVES`` set so the filter, the importer and the survey
    pre-fill can never disagree. Stays a SQL predicate (never a Python filter over
    fetched rows) — this runs over 8,000+ alumni.
    """
    return and_(
        column.isnot(None),
        func.lower(func.trim(column)).notin_(_DESIGNATION_NEGATIVES_SQL),
    )


# Professional certifications stored as flag columns on the program-engagement
# profile (#404). Maps the canonical (upper-case) token to its column.
_DESIGNATION_COLUMNS = {
    "CFP": AlumniProgramEngagement.cfp_designation,
    "CFA": AlumniProgramEngagement.cfa_designation,
    "CPA": AlumniProgramEngagement.cpa_designation,
}


def _designation_holder_exists(token: str):
    """Holder-EXISTS for a token that IS a certification (CFP / CFA / CPA), else None.

    A free-text token naming a certification also matches alumni who HOLD it on
    their program-engagement profile (#404). Passed into the free-text builder
    so the EXISTS sits INSIDE that token's OR group: "CFA Marsh" reads as "holds
    a CFA (or has 'CFA' written on the record) AND is named Marsh" — the cert
    token satisfies its own group via the EXISTS and never has to match a name
    column too.

    Matched on the WHOLE token, never as a substring: a surname like
    "Cpapadopoulos" contains "cpa" and would otherwise silently fold every CPA
    holder into a name search, with nothing in the result list explaining why
    they are there. A token that is not a certification returns ``None``, so a
    plain name search adds no engagement join at all.
    """
    cert_column = _DESIGNATION_COLUMNS.get(token.upper())
    if cert_column is None:
        return None
    return (
        select(AlumniProgramEngagement.engagement_profile_id)
        .where(
            AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
            _holds_designation(cert_column),
        )
        .exists()
    )


async def get(session: AsyncSession, alumni_id: int) -> Alumni | None:
    return await session.get(Alumni, alumni_id)


# Sort options exposed by the list UI, mapped to their ORDER BY columns. Kept as
# a pure module-level function (no IO) so the mapping can be unit-tested by
# compiling the clause — this is the one place the grad-year direction is decided
# and it MUST stay: grad_desc = most-recent grad year FIRST (DESC, nulls last),
# grad_asc = oldest FIRST (ASC, nulls last). The frontend dropdown labels
# ("newest" -> grad_desc, "oldest" -> grad_asc) and the route's `sort` enum stay
# in lockstep with these tokens. Unknown/legacy values fall back to name.
def alumni_order_by(
    sort: str | None,
    *,
    industry=None,
    city=None,
    state=None,
    employer=None,
    relevance=None,
) -> tuple:
    """Return the ORDER BY tuple for a list ``sort`` token.

    Base tokens order by columns on the alumni row: ``name`` | ``grad_desc`` |
    ``grad_asc`` | ``gender`` (A→Z, NULLs last) | ``updated`` (most-recently
    edited first, i.e. ``updated_at`` DESC). The related-data tokens (#357/#495)
    order by an expression the caller supplies — ``industry`` (current industry),
    ``city``, ``state``, ``employer`` (current employer) — each a correlated
    scalar subquery built in ``list_page`` because those values live on related
    tables, not the alumni row. Related tokens sort ASC with NULLs last, always
    tie-broken by last name then the unique PK so tied rows have a total order and
    OFFSET paging can't duplicate/skip across a page boundary (#183). Unknown/
    legacy values — or a related token whose expression wasn't supplied — fall
    back to name.

    ``relevance`` (#620) is the free-text ranking expression and sorts DESC (best
    first). It gets the SAME last-name + PK tiebreakers as every other token, and
    for the same reason: a score is not unique, and OFFSET paging over a
    non-total order silently repeats rows on page 2 and drops others."""
    mapping: dict[str, tuple] = {
        "name": (Alumni.last_name.asc(), Alumni.alumni_id.asc()),
        "grad_desc": (
            Alumni.graduation_year.desc().nulls_last(),
            Alumni.last_name.asc(),
            Alumni.alumni_id.asc(),
        ),
        "grad_asc": (
            Alumni.graduation_year.asc().nulls_last(),
            Alumni.last_name.asc(),
            Alumni.alumni_id.asc(),
        ),
        # Gender (#495) — coarse A→Z on the stored value, NULLs last.
        "gender": (
            Alumni.gender.asc().nulls_last(),
            Alumni.last_name.asc(),
            Alumni.alumni_id.asc(),
        ),
        # Last updated (#495) — most-recently edited first. `updated_at` is
        # NOT NULL (ORM-stamped on every write), so nulls_last is a no-op guard.
        "updated": (
            Alumni.updated_at.desc().nulls_last(),
            Alumni.alumni_id.asc(),
        ),
    }
    for token, expr in (
        ("industry", industry),
        ("city", city),
        ("state", state),
        ("employer", employer),
    ):
        if expr is not None:
            mapping[token] = (
                expr.asc().nulls_last(),
                Alumni.last_name.asc(),
                Alumni.alumni_id.asc(),
            )
    if relevance is not None:
        mapping["relevance"] = (
            relevance.desc(),
            Alumni.last_name.asc(),
            Alumni.alumni_id.asc(),
        )
    return mapping.get(sort or "name", (Alumni.last_name.asc(), Alumni.alumni_id.asc()))


def build_alumni_query(
    *,
    q: str | None = None,
    # Per-field search (dashboard Quick / Advanced search). Each is a
    # case-insensitive PARTIAL match, AND-combined with the others, and ignored
    # when blank — so an empty box never narrows the results.
    net_id: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    preferred_name: str | None = None,
    email: str | None = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    # Exact "Class of" (Marriott) year — used by the cohort-update export to pull a
    # cohort by class year instead of graduation_year.
    graduation_class: int | None = None,
    deceased: bool | None = None,
    # Gender (#360): a coarse ``M`` / ``F`` facet, AND-combined with every other
    # filter (e.g. the industry facet). Matches on the first letter of the stored
    # gender value (case-insensitive) so "Male"/"M" and "Female"/"F" both match.
    gender: str | None = None,
    # Industry bucket (#351/#352): ``unknown`` (blank/missing primary industry)
    # or ``other`` (a primary industry that isn't a canonical finance industry).
    # Distinct from the exact ``industry`` facet below, which matches a specific
    # primary-industry name.
    industry_group: str | None = None,
    # Location proximity (#358). A SQLAlchemy predicate (built by
    # ``geo_search.alumni_location_filter`` from a resolved location query) matched
    # inside a correlated EXISTS over the alumnus's contact-info row. ``None`` ->
    # no location filter; the geo module returns a match-nothing predicate for an
    # empty city set, so a resolved-but-empty query never widens the results.
    location_filter=None,
    # Text facets accept a single value (legacy / deep-link) OR a list
    # (multi-select). Each is matched case-insensitively, exact, literal-escaped.
    employer: "str | list[str] | None" = None,
    past_employer: "str | list[str] | None" = None,
    # Industry facets (#584). ``industry`` matches the PRIMARY industry only and
    # ``secondary_industry`` the SECONDARY one — they are separate boxes in the
    # search UI. Jake, 2026-08-03: one param matching "primary OR secondary" made
    # a primary-industry search silently pull in people whose *secondary* industry
    # happened to match, so a "Consulting" search couldn't be trusted to mean
    # "works in Consulting". Splitting them narrows ``industry`` deliberately.
    industry: "str | list[str] | None" = None,
    secondary_industry: "str | list[str] | None" = None,
    title: "str | list[str] | None" = None,
    seniority: "str | list[str] | None" = None,
    # Employment status (#584) — the person-level ``alumni.employment_status``
    # column (Full-time / Graduate Student / Unemployed / ...), not an employment
    # -record field. Free text that also holds off-list legacy values from the
    # intake sheet ("Employed", "Stay at home parent"), which are deliberate, so
    # this is a plain exact (case-insensitive) match with no normalization —
    # whatever is stored is what the filter-options list offers and matches.
    employment_status: "str | list[str] | None" = None,
    city: "str | list[str] | None" = None,
    state: "str | list[str] | None" = None,
    # Work country + region. `country` sits on the employment record alongside
    # `city` / `state`; `region` is the derived US grouping (#283) and lives on
    # the contact row, which is where `derive_region` writes it. Both were
    # searchable in the free-text sentence and in the stored schema but had no
    # query parameter at all, so a search for either returned the unfiltered
    # list rather than nothing — the worse of the two failures.
    country: "str | list[str] | None" = None,
    region: "str | list[str] | None" = None,
    # Previous job title, the employment-history twin of `past_employer`.
    past_title: "str | list[str] | None" = None,
    # Education facets. `graduate_degree` (further down) only answers "do they
    # hold one at all"; these match the actual education-history rows, so
    # "who studied Accounting" and "who has a JD" become answerable.
    university: "str | list[str] | None" = None,
    degree: "str | list[str] | None" = None,
    major: "str | list[str] | None" = None,
    # "Held any role covering this calendar year." Open-ended history rows (no
    # end year) count as still running — see the predicate for why that is the
    # only reading that doesn't lose current jobs.
    worked_in_year: int | None = None,
    tag: "str | list[str] | None" = None,
    status_label: "str | list[str] | None" = None,
    # Suppression (opt-in, #survey): EXCLUDE alumni carrying any of these status
    # labels — the inverse of ``status_label`` above. Used by outbound sends
    # (see ``dropdowns.SUPPRESSED_CONTACT_STATUS_LABELS``) so "must not be
    # emailed" is one SQL predicate shared by the sender and any preview of it,
    # rather than a filter re-implemented per caller. Off unless passed, so no
    # existing list/export query changes.
    suppress_labels: "str | list[str] | None" = None,
    leadership_role: "str | list[str] | None" = None,
    survey_status: "str | list[str] | None" = None,
    # "Needs surveying" (#160): alumni who are DUE for the biennial survey —
    # never completed one, or whose most-recent completion is older than the
    # survey cadence. The threshold is computed server-side and passed in (the
    # route owns "now"); a ``None`` threshold disables the filter.
    needs_survey: bool = False,
    survey_due_before: datetime.datetime | None = None,
    # Last-contacted (derived from interactions): contacted on/after a date,
    # NOT contacted since a date (stale), or never contacted at all.
    contacted_after: datetime.date | None = None,
    contacted_before: datetime.date | None = None,
    never_contacted: bool = False,
    attended_event: bool = False,
    # Guest-speaker-AT-AN-EVENT window (drives the dashboard "Guest speakers this
    # month" tile's deep-link). Distinct from ``guest_speaker_willing`` (the
    # alumnus-level willing flag, no date) — these select alumni who actually
    # served as a speaker at an event whose date falls in the window.
    spoke_after: datetime.date | None = None,
    spoke_before: datetime.date | None = None,
    donor: bool = False,
    mentor_willing: bool = False,
    guest_speaker_willing: bool = False,
    # Professional-certification flags on the program-engagement profile. Each
    # narrows to alumni who hold that designation (correlated EXISTS). All three
    # go through ``_holds_designation``, so a cell typed "No" never counts (#362).
    cfp: bool = False,
    cfa: bool = False,
    cpa: bool = False,
    # Designation facet (#404): a list of certification tokens (CFP / CFA / CPA).
    # Returns alumni holding ANY of the requested designations (OR). Accepts a
    # single value (legacy / deep-link) or a list; tokens are matched
    # case-insensitively.
    designations: "str | list[str] | None" = None,
    # Only alumni who have a graduate degree recorded.
    graduate_degree: bool = False,
    missing_email: bool = False,
    missing_employer: bool = False,
    missing_phone: bool = False,
    duplicate: bool = False,
    # Friends of the finance program (#218). ``True`` -> alumni only, ``False``
    # -> friends only, ``None`` -> both. The list route defaults this to ``True``
    # so the Alumni page is unchanged; friends are opt-in.
    is_alumni: bool | None = True,
    include_archived: bool = False,
) -> Select:
    """Build the filtered ``SELECT alumni`` statement (without limit/offset).

    The missing-data filters mirror the dashboard's KPI logic exactly so the
    "Review" deep-links land on the same population the dashboard counted:
      * ``missing_email``    — no contact-info row with a personal/work email
      * ``missing_employer`` — no current-employment row naming an employer, AND
        an employer is applicable: alumni whose ``employment_status`` is one of
        ``EMPLOYER_NOT_APPLICABLE_STATUSES`` (Unemployed / Not in the Labor
        Force / Graduate Student) are NOT missing one (#608). Military IS still
        counted — a branch of service is an employer.
      * ``missing_phone``    — no contact-info row with a phone number
      * ``duplicate``        — the alumnus appears on either side of a
        ``duplicate_candidates`` pair
    All conditions are correlated EXISTS subqueries so filtering stays in
    PostgreSQL (never client-side) and the plan is stable at scale.
    """
    conditions = []
    if not include_archived:
        conditions.append(Alumni.archived.is_(False))
    # Alumni / friends split (#218). None -> no predicate (return both).
    if is_alumni is not None:
        conditions.append(Alumni.is_alumni.is_(is_alumni))
    # Free-text search (#620). The sentence is parsed into routed, normalized
    # segments (see app.core.search_terms) and each segment becomes one
    # condition; segments are AND-ed. Both this list query and the CSV export go
    # through here, so they can never describe different populations — the
    # ranking that makes the LIST readable is applied separately in ``list_page``
    # and touches no predicate.
    parsed_q = parse_free_text(q)
    if parsed_q:
        conditions.extend(q_conditions(parsed_q, extra=_designation_holder_exists))
    # Per-field partial matches (AND-combined; blanks ignored).
    def _field_like(value: str | None, column) -> None:
        if value and value.strip():
            conditions.append(
                column.ilike(f"%{escape_like(value.strip())}%", escape="\\")
            )

    _field_like(net_id, Alumni.net_id)
    _field_like(first_name, Alumni.first_name)
    # Last-name field search also matches the maiden / birth name (#216) so an
    # alumna looked up by her birth name is found even via the dedicated
    # last-name box, not just the free-text ``q`` search.
    if last_name and last_name.strip():
        like = f"%{escape_like(last_name.strip())}%"
        conditions.append(
            or_(
                Alumni.last_name.ilike(like, escape="\\"),
                Alumni.birth_name.ilike(like, escape="\\"),
            )
        )
    _field_like(preferred_name, Alumni.preferred_first_name)
    if email and email.strip():
        like = f"%{escape_like(email.strip())}%"
        conditions.append(
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                or_(
                    AlumniContactInfo.personal_email.ilike(like, escape="\\"),
                    AlumniContactInfo.work_email.ilike(like, escape="\\"),
                ),
            )
            .exists()
        )
    if graduation_year is not None:
        conditions.append(Alumni.graduation_year == graduation_year)
    if grad_year_min is not None:
        conditions.append(Alumni.graduation_year >= grad_year_min)
    if grad_year_max is not None:
        conditions.append(Alumni.graduation_year <= grad_year_max)
    if graduation_class is not None:
        conditions.append(Alumni.graduation_class == graduation_class)
    if deceased is not None:
        conditions.append(Alumni.deceased.is_(deceased))
    if gender and gender.strip():
        # First-letter, case-insensitive match so "Male"/"M" and "Female"/"F"
        # both match a single-letter ``M`` / ``F`` filter value. NULL/blank gender
        # rows never match (substr of a trimmed empty string is empty).
        conditions.append(
            func.upper(func.substr(func.trim(Alumni.gender), 1, 1))
            == gender.strip()[0].upper()
        )
    if industry_group == "unknown":
        # Merged "Unknown" bar (#295): either NO current-employment row names a
        # non-blank primary industry, OR the primary industry is the explicit
        # "Unknown" value — matching how the dashboard counts this bar.
        has_industry = (
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                CurrentEmployment.current_industry.is_not(None),
                func.trim(CurrentEmployment.current_industry) != "",
            )
            .exists()
        )
        has_explicit_unknown = (
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                func.lower(func.trim(CurrentEmployment.current_industry))
                == _EXPLICIT_UNKNOWN_LOWER,
            )
            .exists()
        )
        conditions.append(or_(~has_industry, has_explicit_unknown))
    elif industry_group == "other":
        # Has a primary industry, but it isn't one of the canonical finance
        # industries AND isn't one of the values the dashboard breaks out into
        # their own bars ("Graduate Student", "Unknown") — so this drill-down
        # matches the dashboard "Other" bar exactly. "Military" (#608) is NOT
        # broken out, so it belongs in here, same as Law or FP&A.
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                CurrentEmployment.current_industry.is_not(None),
                func.trim(CurrentEmployment.current_industry) != "",
                func.lower(func.trim(CurrentEmployment.current_industry)).notin_(
                    _FINANCE_INDUSTRIES_LOWER | _OTHER_EXCLUDED_LOWER
                ),
            )
            .exists()
        )
    if location_filter is not None:
        # Proximity search (#358): the geo module built the (city, state) match
        # predicate over the alumnus's WORK location (#287); correlate it to THIS
        # alumnus via a current-employment EXISTS, mirroring the ``city`` /
        # ``state`` facets below. An empty-key predicate matches nothing, so a
        # resolved-but-empty location never widens results.
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                location_filter,
            )
            .exists()
        )
    employers = _as_values(employer)
    if employers:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.current_employer, employers),
            )
            .exists()
        )
    past_employers = _as_values(past_employer)
    if past_employers:
        conditions.append(
            select(EmploymentHistory.employment_history_id)
            .where(
                EmploymentHistory.alumni_id == Alumni.alumni_id,
                _ilike_any(EmploymentHistory.employer_name, past_employers),
            )
            .exists()
        )
    industries = _as_values(industry)
    if industries:
        # PRIMARY industry only (#584) — see the param docs above. Passing both
        # ``industry`` and ``secondary_industry`` AND-s them (two separate
        # conditions), which is what the two-box UI implies: "primary is X and
        # secondary is Y", not "either column mentions X or Y".
        #
        # EXCEPT "Military" (#608). Jake's reservist case: someone can serve AND
        # hold a civilian job, e.g. primary = Investment Banking, secondary =
        # Military. employment_status can't express that (it is one value);
        # industry has two slots, which is the whole reason Military lives here.
        # So a Military search has to look at BOTH slots or it misses every
        # reservist — the people the option was added for.
        #
        # THIS WIDENING IS MILITARY-ONLY AND MUST STAY THAT WAY. Every other
        # industry stays primary-only per Jake's 2026-08-03 decision (#584): a
        # "Consulting" search that quietly returned people whose *secondary* was
        # Consulting is exactly the bug that decision removed. Do not generalize
        # this to the rest of the list; ``tests/test_alumni_search.py`` pins both
        # halves of that rule.
        military = [v for v in industries if v.strip().lower() == _MILITARY_LOWER]
        primary_only = [
            v for v in industries if v.strip().lower() != _MILITARY_LOWER
        ]
        matches = []
        if primary_only:
            matches.append(
                _ilike_any(CurrentEmployment.current_industry, primary_only)
            )
        if military:
            matches.append(
                or_(
                    _ilike_any(CurrentEmployment.current_industry, military),
                    _ilike_any(
                        CurrentEmployment.current_industry_secondary, military
                    ),
                )
            )
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                # Multi-select ORs the requested industries together, unchanged.
                matches[0] if len(matches) == 1 else or_(*matches),
            )
            .exists()
        )
    secondary_industries = _as_values(secondary_industry)
    if secondary_industries:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(
                    CurrentEmployment.current_industry_secondary, secondary_industries
                ),
            )
            .exists()
        )
    titles = _as_values(title)
    if titles:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.current_title, titles),
            )
            .exists()
        )
    seniorities = _as_values(seniority)
    if seniorities:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.seniority_level, seniorities),
            )
            .exists()
        )
    employment_statuses = _as_values(employment_status)
    if employment_statuses:
        # Lives on the alumni row itself, so no EXISTS is needed — a direct
        # column predicate keeps the plan flat.
        conditions.append(_ilike_any(Alumni.employment_status, employment_statuses))
    # City / state are the alumnus's WORK location (current_employment) — the
    # employer's address is the only address this system holds, and it is what
    # the geography map plots and the List's City/State columns show (#287).
    cities = _as_values(city)
    if cities:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.current_city, cities),
            )
            .exists()
        )
    states = _as_values(state)
    if states:
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.current_state, states),
            )
            .exists()
        )
    countries = _as_values(country)
    if countries:
        # Same source as city/state above: the work location on the employment
        # record, which is the only address this system holds (#287).
        conditions.append(
            select(CurrentEmployment.current_employment_id)
            .where(
                CurrentEmployment.alumni_id == Alumni.alumni_id,
                _ilike_any(CurrentEmployment.current_country, countries),
            )
            .exists()
        )
    regions = _as_values(region)
    if regions:
        # Region is DERIVED from the work state (#283) and stored on the contact
        # row — that is where `hygiene.derive_region` writes it, so filtering the
        # stored column is what keeps this agreeing with the map's shading rather
        # than re-deriving the mapping in a second place.
        conditions.append(
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                _ilike_any(AlumniContactInfo.region, regions),
            )
            .exists()
        )
    past_titles = _as_values(past_title)
    if past_titles:
        conditions.append(
            select(EmploymentHistory.employment_history_id)
            .where(
                EmploymentHistory.alumni_id == Alumni.alumni_id,
                _ilike_any(EmploymentHistory.employment_title, past_titles),
            )
            .exists()
        )
    universities = _as_values(university)
    if universities:
        conditions.append(
            select(EducationHistory.education_id)
            .where(
                EducationHistory.alumni_id == Alumni.alumni_id,
                _ilike_any(EducationHistory.university, universities),
            )
            .exists()
        )
    degrees = _as_values(degree)
    if degrees:
        conditions.append(
            select(EducationHistory.education_id)
            .where(
                EducationHistory.alumni_id == Alumni.alumni_id,
                _ilike_any(EducationHistory.degree, degrees),
            )
            .exists()
        )
    majors = _as_values(major)
    if majors:
        conditions.append(
            select(EducationHistory.education_id)
            .where(
                EducationHistory.alumni_id == Alumni.alumni_id,
                _ilike_any(EducationHistory.major, majors),
            )
            .exists()
        )
    if worked_in_year is not None:
        # A history row covers the year when it started on or before it and
        # ended on or after it. A NULL end year means "still there", so it must
        # count as covering every year from the start onward — treating NULL as
        # "unknown, exclude" would drop precisely the roles people still hold,
        # which is the opposite of what anyone asking this question wants. A NULL
        # START year is genuinely unusable (there is no year to compare), so
        # those rows are excluded rather than guessed at.
        conditions.append(
            select(EmploymentHistory.employment_history_id)
            .where(
                EmploymentHistory.alumni_id == Alumni.alumni_id,
                EmploymentHistory.start_year.is_not(None),
                EmploymentHistory.start_year <= worked_in_year,
                or_(
                    EmploymentHistory.end_year.is_(None),
                    EmploymentHistory.end_year >= worked_in_year,
                ),
            )
            .exists()
        )
    tags = _as_values(tag)
    if tags:
        # A tag name resolves to exactly ONE predicate (#629). The nine "ways to
        # get involved" are backed by their ``alumni_program_engagement``
        # boolean; every other tag is backed by an ``alumni_tags`` row. Routing
        # each name through ``engagement_flag_for_tag`` is what stops
        # `tag=Mentor` and the `mentor_willing` flag from resolving to two
        # different half-populated lists, which is what they did before.
        #
        # Note the nine deliberately do NOT also match a leftover
        # ``alumni_tags`` row of the same name. Matching both would resurrect
        # the fork from the other side: an alumnus who answered NO would keep
        # matching on the strength of a stale row. The migration
        # ``2026-08-05_engagement_tag_backfill.sql`` folds the pre-existing
        # hand-applied Mentor/Speaker rows into the flags so nobody is dropped
        # by that choice.
        flag_columns = [
            getattr(AlumniProgramEngagement, column)
            for column in (
                engagement_flag_for_tag(name) for name in tags
            )
            if column is not None
        ]
        plain_tags = [name for name in tags if engagement_flag_for_tag(name) is None]
        # OR within the facet, matching every other multi-value filter here.
        tag_clauses = []
        if plain_tags:
            tag_clauses.append(
                select(AlumniTag.alumni_tag_id)
                .join(Tag, Tag.tag_id == AlumniTag.tag_id)
                .where(
                    AlumniTag.alumni_id == Alumni.alumni_id,
                    _ilike_any(Tag.tag_name, plain_tags),
                )
                .exists()
            )
        if flag_columns:
            tag_clauses.append(
                select(AlumniProgramEngagement.engagement_profile_id)
                .where(
                    AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                    or_(*[column.is_(True) for column in flag_columns]),
                )
                .exists()
            )
        conditions.append(or_(*tag_clauses) if len(tag_clauses) > 1 else tag_clauses[0])
    status_labels = _as_values(status_label)
    if status_labels:
        conditions.append(
            select(AlumniStatusLabel.alumni_status_label_id)
            .join(
                StatusLabel,
                StatusLabel.status_label_id == AlumniStatusLabel.status_label_id,
            )
            .where(
                AlumniStatusLabel.alumni_id == Alumni.alumni_id,
                _ilike_any(StatusLabel.status_label_name, status_labels),
            )
            .exists()
        )
    suppressed = _as_values(suppress_labels)
    if suppressed:
        # NOT EXISTS, so it stays a SQL-level predicate over the whole cohort —
        # never a post-query filter in Python (8,000+ alumni).
        conditions.append(~has_status_label_exists(suppressed))
    leadership_roles = _as_values(leadership_role)
    if leadership_roles:
        conditions.append(
            select(FinanceSocietyLeadership.finance_society_leadership_id)
            .where(
                FinanceSocietyLeadership.alumni_id == Alumni.alumni_id,
                _ilike_any(
                    FinanceSocietyLeadership.leadership_role, leadership_roles
                ),
            )
            .exists()
        )
    survey_statuses = _as_values(survey_status)
    if survey_statuses:
        conditions.append(
            select(Survey.survey_id)
            .where(
                Survey.alumni_id == Alumni.alumni_id,
                _ilike_any(Survey.survey_status, survey_statuses),
            )
            .exists()
        )
    if needs_survey and survey_due_before is not None:
        # DUE = no survey COMPLETED within the cadence window. A correlated NOT
        # EXISTS over a recent completion captures both cases in one predicate:
        #   * never surveyed (no survey rows, or rows with a NULL completed_at)
        #     -> nothing satisfies the inner EXISTS -> DUE, and
        #   * most-recent completion older than the threshold -> still no row
        #     with completed_at >= threshold -> DUE.
        # ``survey_due_before`` is the server-computed cutoff (now - cadence);
        # comparing completed_at to it avoids re-deriving "most recent" — any
        # completion on/after the cutoff means NOT due. Stays in PostgreSQL with
        # a stable plan (indexable on surveys.alumni_id, completed_at).
        conditions.append(
            ~select(Survey.survey_id)
            .where(
                Survey.alumni_id == Alumni.alumni_id,
                Survey.completed_at.is_not(None),
                Survey.completed_at >= survey_due_before,
            )
            .exists()
        )
    if contacted_after is not None:
        conditions.append(
            select(Interaction.interaction_id)
            .where(
                Interaction.alumni_id == Alumni.alumni_id,
                Interaction.interaction_date_time >= contacted_after,
            )
            .exists()
        )
    if contacted_before is not None:
        # "Not contacted since": no interaction on/after the date (i.e. stale).
        conditions.append(
            ~select(Interaction.interaction_id)
            .where(
                Interaction.alumni_id == Alumni.alumni_id,
                Interaction.interaction_date_time >= contacted_before,
            )
            .exists()
        )
    if never_contacted:
        conditions.append(
            ~select(Interaction.interaction_id)
            .where(Interaction.alumni_id == Alumni.alumni_id)
            .exists()
        )
    if attended_event:
        has_attended = (
            select(EventAttendance.event_attendance_id)
            .where(EventAttendance.alumni_id == Alumni.alumni_id)
            .exists()
        )
        conditions.append(has_attended)
    if spoke_after is not None or spoke_before is not None:
        # Alumni who served as a guest speaker at an event in the window. Mirrors
        # the dashboard ``guest_speakers_this_month`` KPI exactly (same
        # attendance_status ILIKE '%speaker%' + event_date bounds) so the tile's
        # count equals this deep-linked list's length. Correlated EXISTS over the
        # attendance→event join keeps it in PostgreSQL with a stable plan.
        spoke = (
            select(EventAttendance.event_attendance_id)
            .join(Event, Event.event_id == EventAttendance.event_id)
            .where(
                EventAttendance.alumni_id == Alumni.alumni_id,
                EventAttendance.attendance_status.ilike("%speaker%"),
            )
        )
        if spoke_after is not None:
            spoke = spoke.where(Event.event_date >= spoke_after)
        if spoke_before is not None:
            spoke = spoke.where(Event.event_date <= spoke_before)
        conditions.append(spoke.exists())
    if donor:
        is_donor = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.piff_donor.is_(True),
            )
            .exists()
        )
        conditions.append(is_donor)
    if mentor_willing:
        is_mentor = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.mentor_willing.is_(True),
            )
            .exists()
        )
        conditions.append(is_mentor)
    if guest_speaker_willing:
        is_speaker = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                AlumniProgramEngagement.guest_speaker_willing.is_(True),
            )
            .exists()
        )
        conditions.append(is_speaker)
    if cfp:
        is_cfp = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                _holds_designation(AlumniProgramEngagement.cfp_designation),
            )
            .exists()
        )
        conditions.append(is_cfp)
    if cfa:
        is_cfa = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                _holds_designation(AlumniProgramEngagement.cfa_designation),
            )
            .exists()
        )
        conditions.append(is_cfa)
    if cpa:
        is_cpa = (
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                _holds_designation(AlumniProgramEngagement.cpa_designation),
            )
            .exists()
        )
        conditions.append(is_cpa)
    designation_tokens = {v.strip().upper() for v in _as_values(designations)}
    designation_columns = [
        column
        for token, column in _DESIGNATION_COLUMNS.items()
        if token in designation_tokens
    ]
    if designation_columns:
        # ANY semantics (#404): an alumnus matches if they hold at least one of
        # the requested designations. Single correlated EXISTS OR-ing the flags.
        conditions.append(
            select(AlumniProgramEngagement.engagement_profile_id)
            .where(
                AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
                or_(*[_holds_designation(c) for c in designation_columns]),
            )
            .exists()
        )
    if graduate_degree:
        conditions.append(
            and_(
                Alumni.graduate_degree.isnot(None),
                func.trim(Alumni.graduate_degree) != "",
            )
        )
    if missing_email:
        # Same definition as the dashboard's `missing_email` KPI (shared via
        # `email_reach`), so the tile and the list it deep-links to cannot report
        # different populations. Blank/whitespace now counts as MISSING (#392).
        has_email = (
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                email_reach.has_email_value_sql(
                    AlumniContactInfo.personal_email, AlumniContactInfo.work_email
                ),
            )
            .exists()
        )
        conditions.append(~has_email)
    if missing_employer:
        conditions.append(~has_employer_or_not_applicable())
    if missing_phone:
        has_phone = (
            select(AlumniContactInfo.contact_info_id)
            .where(
                AlumniContactInfo.alumni_id == Alumni.alumni_id,
                AlumniContactInfo.phone.is_not(None),
            )
            .exists()
        )
        conditions.append(~has_phone)
    if duplicate:
        is_duplicate = (
            select(DuplicateCandidate.duplicate_candidate_id)
            .where(
                or_(
                    DuplicateCandidate.alumni_id_1 == Alumni.alumni_id,
                    DuplicateCandidate.alumni_id_2 == Alumni.alumni_id,
                )
            )
            .exists()
        )
        conditions.append(is_duplicate)

    stmt = select(Alumni)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    return stmt


async def list_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    **filters,
) -> tuple[list[Alumni], int]:
    """Return a filtered page of alumni and the total count for that filter.

    Each returned Alumni also gets ``current_employer`` / ``current_industry``
    (from ``current_employment``) and ``current_city`` / ``current_state`` (from
    ``alumni_contact_info`` — the same table the geography map shades by, so the
    list and the map agree on a record's location) set as plain instance
    attributes via correlated scalar subqueries, so the list view can show them
    without an N+1 or a row-multiplying join. The single-record schema ignores
    these.
    """
    # ``sort`` unset means "pick the sensible default": relevance for a free-text
    # search (the best answer belongs on page 1, not wherever the alphabet puts
    # it), last name otherwise. An explicit token from the UI always wins.
    relevance = relevance_expression(parse_free_text(filters.get("q")))
    sort = filters.pop("sort", None) or ("relevance" if relevance is not None else "name")
    base = build_alumni_query(**filters)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))

    current_employer = (
        select(CurrentEmployment.current_employer)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    current_industry = (
        select(CurrentEmployment.current_industry)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    # Secondary (non-finance) industry — shown in the Other drill-down.
    current_industry_secondary = (
        select(CurrentEmployment.current_industry_secondary)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    # Location for the list's City/State columns: the alumnus's WORK city/state
    # (current_employment.current_city/current_state). The employer's address is
    # the only address this system holds — there is no residence data — and this
    # is the same column the geography map shades off, so the list and the map
    # agree (#287).
    current_city = (
        select(CurrentEmployment.current_city)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    current_state = (
        select(CurrentEmployment.current_state)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )
    projection = [
        Alumni,
        current_employer,
        current_industry,
        current_industry_secondary,
        current_city,
        current_state,
    ]
    # The relevance score is SELECTed (as a label) and ordered by NAME rather
    # than repeated inline in the ORDER BY: the expression is large, and
    # repeating it makes PostgreSQL evaluate it twice per row for no benefit.
    order_relevance = None
    if relevance is not None:
        projection.append(relevance.label("relevance"))
        order_relevance = literal_column("relevance")
    rows_stmt = select(*projection)
    if base.whereclause is not None:
        rows_stmt = rows_stmt.where(base.whereclause)

    # Sort options exposed by the list UI (see alumni_order_by). The related-data
    # sorts (industry / city / state, #357) order by the SAME correlated scalar
    # subqueries the row projection uses, so the ordering matches the displayed
    # column exactly. Unknown values fall back to name.
    #
    # Relevance ranking (#620) is applied ONLY here, in the ORDER BY — never as a
    # predicate — so the population the list shows stays byte-identical to the
    # one the CSV export builds from the same filters.
    rows_stmt = (
        rows_stmt.order_by(
            *alumni_order_by(
                sort,
                industry=current_industry,
                city=current_city,
                state=current_state,
                employer=current_employer,
                relevance=order_relevance,
            )
        )
        .limit(limit)
        .offset(offset)
    )
    result = await session.execute(rows_stmt)
    items: list[Alumni] = []
    for row in result.all():
        alumnus, employer, industry, industry_secondary, city, state = row[:6]
        alumnus.current_employer = employer
        alumnus.current_industry = industry
        alumnus.current_industry_secondary = industry_secondary
        alumnus.current_city = city
        alumnus.current_state = state
        items.append(alumnus)
    return items, int(total or 0)

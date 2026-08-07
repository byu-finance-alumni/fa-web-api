"""Canonical dropdown option lists (controlled vocabularies).

Source of truth: ``database/dropdowns.md``. Mirror any change there here AND in
the frontend (``fa-web-app/src/constants/dropdowns.ts``). Values are stored as
exact, case-sensitive strings so filtering/grouping stays consistent.

Per the deliberate design in ``dropdowns.md`` these lists are enforced at the
*application* layer, not the database — the columns stay ``varchar`` so the
options can change without a migration. Wire :func:`validate_industry` into the
Pydantic schema of any field that writes an industry value (current industry,
secondary industry, employment-history industry) once a career / import write
path exists. There is no such write path today, so nothing imports this yet.
"""

from __future__ import annotations

from app.core.errors import InvalidRequestError

# Industries — current_industry, current_industry_secondary, employment_industry.
#
# ORDER IS THE DROPDOWN ORDER and is mirrored by ``vocabulary_terms.sort_order``
# (category 'industry'), where sort_order == the index below EXCEPT for the three
# pinned tail options: "Unknown" (97), "Graduate Student" (98) and "Other" (99),
# which are held at the bottom out of alphabetical order.
# ``tests/test_industry_vocab.py`` parses the migrations and fails if the two
# sources drift — do NOT reorder this tuple without a matching migration.
#
# Sorted case-insensitively ("Financial Services" before "FP&A"), with the
# "Unknown" value, the "Graduate Student" indicator and the "Other" catch-all
# pinned last, in that order (#295 / #294 / #282).
#
# "Military" (#608) sits in the ALPHABETICAL BODY between "Law" and "Private
# Banking" — unlike "Graduate Student" it is a real answer to "what do you do",
# not a status indicator, so it is not pinned to the tail. Inserting it there
# shifts every later body index by one, which is why its migration re-upserts
# the whole body's sort_order rather than appending a single row.
INDUSTRIES: tuple[str, ...] = (
    "Asset Management",
    "Commercial Banking",
    "Consulting",
    "Corporate Banking",
    "Corporate Finance",
    "Credit Risk",
    "Equity Research",
    "Financial Services",
    "FP&A",
    "Investment Banking",
    "Law",
    "Military",
    "Private Banking",
    "Private Credit",
    "Private Equity",
    "Real Estate",
    "Sales",
    "Sales and Trading",
    "Valuation & Advisory",
    "Venture Capital",
    "Wealth Management",
    "Unknown",
    "Graduate Student",
    "Other",
)

# Mentor industries — the same list plus Law/Government (multi-select field).
MENTOR_INDUSTRIES: tuple[str, ...] = (*INDUSTRIES, "Law/Government")

# --- primary vs secondary industry (#282) ------------------------------------
# Tanya, 2026-07-16: these four aren't dashboard industries and shouldn't be
# offered as an alumnus's PRIMARY industry — but they must stay available as a
# SECONDARY industry, so they are hidden from the primary dropdown rather than
# deleted from the vocabulary.
#
# This is a DROPDOWN-VISIBILITY split only. :func:`validate_industry` still
# accepts all of :data:`INDUSTRIES` for either field, matching the established
# soft-delete semantics in ``app/api/routes/vocabulary.py`` ("a value still on
# existing records stays valid, it just disappears from new-entry dropdowns").
# Records that keep one of these as their primary — the conflict rows the #282
# data migration deliberately skips — must stay editable, not 422 on save.
_PRIMARY_EXCLUDED_INDUSTRIES = frozenset(
    {"Law", "Corporate Banking", "Sales and Trading", "Credit Risk"}
)
_PRIMARY_EXCLUDED_BY_LOWER = frozenset(
    v.lower() for v in _PRIMARY_EXCLUDED_INDUSTRIES
)

# Options for the PRIMARY industry dropdown (current_industry).
PRIMARY_INDUSTRIES: tuple[str, ...] = tuple(
    i for i in INDUSTRIES if i not in _PRIMARY_EXCLUDED_INDUSTRIES
)
# Options for the SECONDARY industry dropdown (current_industry_secondary) —
# the full vocabulary, including the four hidden from primary.
SECONDARY_INDUSTRIES: tuple[str, ...] = INDUSTRIES


def filter_primary_industries(values: list[str]) -> list[str]:
    """Drop the primary-excluded industries from *values*, preserving order.

    Applied to the DB-backed ``vocabulary_terms`` payload so the primary
    dropdown hides them even though they remain live vocabulary terms. Matching
    is case-insensitive because term casing can drift from admin edits.
    """
    return [v for v in values if v.strip().lower() not in _PRIMARY_EXCLUDED_BY_LOWER]


# Dashboard wheel: the 15 finance industries Tanya wants shown as their own slice
# (2026-07-11). Everything else in INDUSTRIES (Law, Corporate Banking, FP&A,
# Sales and Trading, Credit Risk, Military, Unknown, Graduate Student) plus any
# non-vocab value folds into "Other". Both the dashboard breakdown AND the
# alumni-list ``industry_group=other`` filter key off this set so the wheel slice
# and its drill-down stay in sync.
#
# "Graduate Student" (#294) is a non-wheel industry — it does NOT get its own
# wheel slice — but the frontend dashboard surfaces it as its own clickable
# indicator at the BOTTOM of the industry list, separate from the "Other" fold.
#
# "Military" (#608) is non-wheel and gets NO bar of its own — Jake chose to keep
# the industry chart about FINANCE SECTORS, so it simply folds into the "Other"
# catch-all like Law or FP&A. That fold is the default behaviour of this set, so
# there is deliberately no special case for it anywhere in the breakdown; the
# only Military-specific logic in the codebase is the primary-OR-secondary search
# widening in ``repositories/alumni.py`` (the reservist case).
#
# "Unknown" (#295) is likewise a non-wheel industry and simply folds into "Other"
# on the wheel — it gets NO separate dashboard indicator. It is distinct from a
# blank/unset industry ("not yet collected"): "Unknown" means "we checked and it
# is genuinely unknown".
_NON_WHEEL_INDUSTRIES = frozenset(
    {
        "Law",
        "Corporate Banking",
        "FP&A",
        "Sales and Trading",
        "Credit Risk",
        "Military",
        "Unknown",
        "Graduate Student",
        "Other",
    }
)
# The bar ORDER on the dashboard industry breakdown is this tuple's order, so it
# is PINNED here rather than derived from INDUSTRIES — #282 alphabetized the
# dropdown and must not silently reshuffle the dashboard. MEMBERSHIP is still
# asserted to equal ``INDUSTRIES - _NON_WHEEL_INDUSTRIES`` by
# ``tests/test_industry_vocab.py``, so adding a wheel industry to INDUSTRIES
# without listing it here fails CI.
WHEEL_INDUSTRIES: tuple[str, ...] = (
    "Asset Management",
    "Commercial Banking",
    "Consulting",
    "Corporate Finance",
    "Equity Research",
    "Investment Banking",
    "Private Banking",
    "Private Credit",
    "Private Equity",
    "Real Estate",
    "Sales",
    "Valuation & Advisory",
    "Venture Capital",
    "Wealth Management",
    "Financial Services",
)

_INDUSTRIES_SET = frozenset(INDUSTRIES)
# Case-insensitive lookup -> canonical casing, so a CSV/HR export that varies
# case ("investment banking") resolves to the stored value ("Investment Banking")
# instead of hard-rejecting the row.
_INDUSTRIES_BY_LOWER = {v.lower(): v for v in INDUSTRIES}


def validate_industry(value: str | None) -> str | None:
    """Return the canonical industry casing if *value* matches (or ``None``/empty).

    Trims whitespace and normalizes empty strings to ``None``. Matching is
    CASE-INSENSITIVE and returns the canonical-cased value. Raises ``ValueError``
    — which Pydantic surfaces as a 422 field error — when a non-empty value isn't
    one of :data:`INDUSTRIES`. Use as a ``mode="before"`` field validator on
    industry-writing schemas.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    canonical = _INDUSTRIES_BY_LOWER.get(value.lower())
    if canonical is None:
        raise ValueError("Must be one of: " + ", ".join(INDUSTRIES))
    return canonical


# --- employment status (#568 / #377) -----------------------------------------
#
# ``alumni.employment_status`` — what an alumnus is currently doing. Mirror any
# change here in ``database/dropdowns.md`` and in the frontend's
# ``EMPLOYMENT_STATUS_OPTIONS`` (fa-web-app/src/constants/dropdowns.ts);
# ``tests/test_employment_status_vocab.py`` machine-checks this tuple against the
# doc.
#
# DELIBERATELY NOT ENFORCED ON WRITE. The column is a plain ``varchar(50)`` and
# the schemas only bound its LENGTH, so a record that already stores something
# off-list ("Employed", "Stay at home parent") stays editable instead of 422-ing
# on an unrelated field. This tuple is the canonical list the dropdowns, the
# filter and this module's documentation are built from — not an allow-list.
# There is intentionally no ``validate_employment_status``.
#
# ORDER IS THE DROPDOWN ORDER and is Tanya's (#568), not alphabetical, with
# "Unknown" pinned last (#377) the same way it is in :data:`INDUSTRIES`.
EMPLOYMENT_STATUSES: tuple[str, ...] = (
    "Full-time",
    "Part-time",
    "Self-Employed",
    "Graduate Student",
    "Military",
    "Not in the Labor Force",
    "Unemployed",
    "Unknown",
)

# Statuses that are a recorded NON-ANSWER rather than a real one (#572 / #377).
#
# "Unknown" means "we asked and we don't know". It became a first-class option in
# #377 because the 2026-08-04 prod cleanup consolidated the misspelled
# "unkown"/"UNKOWN" rows onto the literal ``Unknown``, and a value ~65 alumni hold
# has to be selectable, filterable and importable like any other.
EMPLOYMENT_STATUS_PLACEHOLDERS: frozenset[str] = frozenset({"Unknown"})

# What the SURVEY offers an alumnus for their OWN status: the canonical list
# minus the placeholders. "Unknown" is meaningless as a self-description — asking
# someone to describe themselves as unknown re-collects the very non-answer the
# survey exists to clear — so it is storable/editable/filterable everywhere but
# never a choice the alum can pick. Mirrors
# ``SURVEY_EMPLOYMENT_STATUS_OPTIONS`` in the frontend.
SURVEY_EMPLOYMENT_STATUSES: tuple[str, ...] = tuple(
    v for v in EMPLOYMENT_STATUSES if v not in EMPLOYMENT_STATUS_PLACEHOLDERS
)

# --- statuses for which a blank employer is COMPLETE data (#608) --------------
#
# The "missing employer" data-hygiene flag counted every employer-less alumnus,
# regardless of what they told us they were doing. For someone recorded as
# Unemployed that is not a gap in our data — it is the data. Flagging it anyway
# is a false alarm, and a review queue full of false alarms is a review queue
# people stop reading.
#
# These four are the statuses where an employer is inapplicable or optional —
# nothing here is a gap someone should be chasing:
#   * "Military"              — Jake, 2026-08-04 (#608): "the branch does not
#     matter." We still WANT the branch when we know it (it is stored in the
#     ordinary employer field and the profile renders it as "Military/<branch>"),
#     but it is not required and it is not chased, so a serving alumnus with no
#     employer must not count as missing one or as an incomplete profile.
#   * "Unemployed"            — by definition has no employer.
#   * "Not in the Labor Force" — by definition not working (retired, caregiving,
#     etc.). Same as above; the distinction is about job-seeking, not employment.
#   * "Graduate Student"      — enrolled, not employed. The codebase already
#     treats the literal string "graduate student" as a non-employer placeholder
#     in the dashboard's Top-employers chart (``_NON_EMPLOYER_VALUES``), so this
#     only makes the hygiene flag agree with a judgement already made.
#
# DELIBERATELY NOT EXEMPT — "Self-Employed" (their own company is the employer
# and we want its name), "Full-time"/"Part-time" (obviously), and "Unknown" (we
# do not know what they are doing, so we cannot claim the blank employer was
# intentional — that is the very gap the flag exists to surface). Widening past
# these four needs a decision from Jake, not a judgement call here: every status
# added silently removes people from the only worklist that would fix them.
#
# Matched case-insensitively on the trimmed value: employment_status is a plain
# varchar with no write validation, so prod holds casing drift from imports.
EMPLOYER_NOT_APPLICABLE_STATUSES: tuple[str, ...] = (
    "Military",
    "Unemployed",
    "Not in the Labor Force",
    "Graduate Student",
)
EMPLOYER_NOT_APPLICABLE_BY_LOWER: frozenset[str] = frozenset(
    v.lower() for v in EMPLOYER_NOT_APPLICABLE_STATUSES
)


def employer_applies(employment_status: str | None) -> bool:
    """False when *employment_status* means there is no employer to record.

    The single source of truth for the "missing employer" exemption, shared by
    the per-record hygiene warning, the dashboard/data-quality counts and the
    ``?missing_employer=1`` drill-down so all three describe the same population.
    An absent/blank status returns ``True`` (we cannot assume the blank is
    intentional).
    """
    if employment_status is None:
        return True
    return employment_status.strip().lower() not in EMPLOYER_NOT_APPLICABLE_BY_LOWER


# --- military service display (#608) -----------------------------------------
#
# The status value that means "serving". Mirrored by ``MILITARY_STATUS`` in the
# frontend (fa-web-app/src/constants/dropdowns.ts), which owns the
# "Military/<branch>" profile rendering.
MILITARY_STATUS = "Military"


def is_military_status(employment_status: str | None) -> bool:
    """True when *employment_status* records military service.

    Trimmed + case-insensitive: the column has no write validation, so prod holds
    casing drift from the free-text intake sheet.
    """
    return (employment_status or "").strip().lower() == MILITARY_STATUS.lower()


# --- marital status (#647) ---------------------------------------------------
#
# ``alumni.marital_status``. Free text until #647; now a fixed FOUR-option choice
# for anything the SURVEY writes. Mirror any change here in
# ``database/dropdowns.md`` and in the frontend's ``MARITAL_STATUS_OPTIONS``
# (fa-web-app/src/constants/dropdowns.ts); ``tests/test_marital_status_vocab.py``
# machine-checks this tuple against the doc.
#
# WHY A CONSTANT AND NOT A ``vocabulary_terms`` CATEGORY. The same reason
# :data:`EMPLOYMENT_STATUSES` is one: these four are a product decision (Jake,
# #647), not a list an admin should be able to extend at runtime. A DB-backed
# category would cost a seed migration and an admin screen to express a list that
# is not expected to change, and it would make the *survey's* allow-list
# runtime-mutable — i.e. an admin edit could start silently rejecting alumni
# answers. The established pattern in this codebase for a small fixed list is a
# constant here + a mirrored frontend constant + a doc the tests parse, so this
# follows it rather than inventing a third shape.
#
# CONSTRAINED WHERE IT IS WRITTEN, NOT ON THE COLUMN. There is deliberately no
# ``validate_marital_status``, for exactly the reason there is no
# ``validate_employment_status``: the column is a plain ``varchar(50)`` that prod
# has been filling from a free-text intake sheet for years, so it holds off-list
# values ("Separated", "Undeclared", casing drift). An allow-list on the column
# would make every one of those records 422 the moment someone edited an
# unrelated field. Off-list values stay readable, displayable, exportable and
# editable; the four are enforced only where a value arrives from an UNTRUSTED
# source — the public survey, via the ``choice`` field kind in
# ``app/services/survey_responses.py``, which ignores an off-list answer outright
# rather than storing it or blanking what is on file.
#
# ORDER IS THE DROPDOWN ORDER: Single first (the most common answer for a recent
# cohort), then the three that describe a marriage that has ended.
MARITAL_STATUSES: tuple[str, ...] = (
    "Single",
    "Married",
    "Divorced",
    "Widowed",
)


# --- finance designations (CFA / CFP / CPA) ----------------------------------
#
# ``alumni_program_engagement.cfa_designation`` / ``cfp_designation`` /
# ``cpa_designation`` are ``varchar(100)``, NOT booleans. The convention is that
# they hold a marker string ("CFA") when the alumnus HOLDS the designation and
# ``NULL`` when they don't — nothing at the database level enforces it.
#
# That matters because the intake sheet's columns are literally headed "CFA
# designation (Yes/No)", so a human filling it in can and will type "No". Stored
# verbatim, "No" is a non-NULL value, and every presence test ("IS NOT NULL")
# counts that alumnus as HOLDING the designation — an alum explicitly recorded as
# NOT a CFA would show up in the CFA filter and in every designation count. This
# module is the ONE place the codebase decides which written values mean "does
# not hold it", so the import path, the SQL filter and the survey pre-fill can
# never disagree about it.
#
# Matching is on the WHOLE trimmed value, case-insensitively — never a substring.
# "No CFA yet" is not in this set and counts as held: guessing at prose is how
# you silently drop real holders.
#
# IN-PROGRESS VALUES ARE DELIBERATELY NOT INTERPRETED. Production holds
# "CFP Level 1" today, and "CFA Level II Candidate" is the shape to expect next.
# Whether a candidate counts as holding the designation is an open product
# question Jake has not decided (2026-08), so those fall through as non-negative
# == held, exactly as they behave today. This was CONSIDERED, not missed — do not
# "fix" it without that decision.
#
# The groups below, in order: plain negatives (mirroring the importer's
# ``_FALSE_TOKENS``), boolean-ish spellings, and the "no data" placeholders the
# real intake sheets use (mirroring ``import_csv._PLACEHOLDER_TOKENS`` /
# ``_MARITAL_BLANK_TOKENS``) — an unrecorded designation is not a held one.
DESIGNATION_NEGATIVES: frozenset[str] = frozenset(
    {
        "no",
        "n",
        "no.",
        "nope",
        "false",
        "f",
        "0",
        "none",
        "n/a",
        "na",
        "not applicable",
        "unknown",
        "undeclared",
        "-",
        "--",
        "–",  # en dash
        "—",  # em dash
    }
)


def holds_designation(value: str | None) -> bool:
    """True when *value* records that the alumnus HOLDS the designation.

    False for ``None``, blank/whitespace-only, and any of
    :data:`DESIGNATION_NEGATIVES`; True for anything else (the marker strings
    "CFA"/"CFP"/"CPA", a "Yes", and in-progress text like "CFP Level 1" — see the
    note above). Case-insensitive and whitespace-trimmed, because the intake
    sheet is human-typed free text.

    This is the single answer to "does this alumnus hold the CFA?". The SQL-side
    equivalent lives in ``app.repositories.alumni._holds_designation`` and is
    built from the SAME :data:`DESIGNATION_NEGATIVES` set.
    """
    if value is None:
        return False
    text = str(value).strip()
    if not text:
        return False
    return text.lower() not in DESIGNATION_NEGATIVES


def normalize_designation(value: str | None) -> str | None:
    """The value as it should be STORED: the trimmed text when held, else ``None``.

    Jake, 2026-08-01: "i think we have it auto make the nos into blank if entered
    in" — a negative becomes blank rather than being stored as the literal "No".
    """
    if not holds_designation(value):
        return None
    return str(value).strip()


# The designation tokens the ``designations`` FILTER accepts (#404) — the same
# three flag columns ``app.repositories.alumni._DESIGNATION_COLUMNS`` maps.
DESIGNATION_TOKENS: tuple[str, ...] = ("CFP", "CFA", "CPA")


def parse_designation_tokens(values: str | list[str] | None) -> list[str]:
    """Normalize + validate the ``designations`` filter input (#404).

    Accepts a single string, a repeatable list, and/or comma-separated values;
    upper-cases, trims, de-dupes, and validates every token against
    :data:`DESIGNATION_TOKENS`.

    An unknown token raises :class:`InvalidRequestError` (422) rather than being
    dropped. That is load-bearing, not defensive: ``build_alumni_query`` only
    applies the designation EXISTS for tokens it recognizes, so a silently
    dropped token would leave NO predicate at all — a filtered view would widen
    to everyone. This is the ONE parser for the filter, shared by
    ``GET /alumni`` and ``POST /alumni/export`` so the two can never disagree
    about which population a designation filter means (#366).
    """
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    valid = set(DESIGNATION_TOKENS)
    out: list[str] = []
    for raw in values:
        for piece in str(raw).split(","):
            token = piece.strip().upper()
            if not token:
                continue
            if token not in valid:
                raise InvalidRequestError(
                    f"Unknown designation '{piece.strip()}'. "
                    f"Valid values: {', '.join(DESIGNATION_TOKENS)}."
                )
            if token not in out:
                out.append(token)
    return out


# Tags — the fixed, canonical engagement tags an alumnus can be labelled with.
# Free-text is intentionally disallowed so the set stays a clean, filterable
# vocabulary. Mirror in fa-web-app/src/constants/dropdowns.ts.
#
# A tag is backed by ONE of two stores, and which one is decided by
# :data:`ENGAGEMENT_FLAG_TAGS` below, never per call site:
#
#   * the nine "ways to get involved" are backed by their boolean column on
#     ``alumni_program_engagement`` (see ENGAGEMENT_FLAG_TAGS);
#   * every other tag is backed by an ``alumni_tags`` row.
TAGS: tuple[str, ...] = (
    "Mentor",
    "Highly Engaged",
    "Speaker",
    "Recruiter",
    "Donor",
    "Warm Contact",
    "High Value",
    "Club/Recruiting",
    "Finance Orgs",
    "Advisory Boards",
    # The nine "ways to get involved" (#629). "Mentor" and "Speaker" above are
    # part of the nine too — they already existed as hand-applied tags, and
    # reusing them is the point (see ENGAGEMENT_FLAG_TAGS).
    "Women in Finance Mentor",
    "Event Helper",
    "NetTrek Host",
    "Finance Conference",
    "Company Event Sponsor",
    "Case Competition Host",
    "PIFF Donor",
    # Not willingness — what the alum has already done for the program. Tagged
    # for the same reason as the nine: an untagged flag renders only in the
    # editor-only Tags tab, invisible to view-only staff (Jake, 2026-08-05).
    "Hired a Finance Intern",
    "Hired a Finance Grad",
)

# The nine "ways to get involved" the survey asks about, as tag name → the
# ``alumni_program_engagement`` boolean column that IS that tag (#629).
#
# WHY THESE ARE DERIVED RATHER THAN MIRRORED INTO ``alumni_tags``
# ---------------------------------------------------------------
# The willingness flag already had two writers — the survey apply path and the
# staff-facing "Ways to get involved" edit form — so survey answers and
# hand-entry ALREADY converge on one store. Writing a matching ``alumni_tags``
# row would add a third, and every extra store is a chance for the two to
# disagree. Deriving keeps exactly one.
#
# Withdrawal is the deciding argument. An alum who answers NO next year must
# leave the list, or staff email people who already opted out. Derived, that is
# a single flag flip — the chip and the search predicate stop matching in the
# same instant, with nothing to delete and no window in which the two stores
# disagree. Mirrored, withdrawal has to DELETE a tag row, and nothing in
# ``alumni_tags`` records whether a given "Mentor" row came from a survey or
# from a staff member applying it by hand — so withdrawal would either clobber
# hand-applied tags or leave opted-out alumni on the list.
#
# "Mentor" and "Speaker" deliberately map onto the pre-existing hand-applied
# tags instead of getting new names. Before #629 the mentor list was already
# forked in two: `tag=Mentor` matched ``alumni_tags`` and `mentor=1` matched
# ``mentor_willing``, and on dev those returned 39 and 83 people sharing only
# 16. One name resolving to one predicate is what un-forks it.
ENGAGEMENT_FLAG_TAGS: dict[str, str] = {
    "Mentor": "mentor_willing",
    "Women in Finance Mentor": "women_in_finance_mentor_willing",
    "Speaker": "guest_speaker_willing",
    "Event Helper": "help_at_event_willing",
    "NetTrek Host": "nettrek_host_willing",
    "Finance Conference": "finance_conference_willing",
    "Company Event Sponsor": "company_event_sponsor_willing",
    "Case Competition Host": "case_competition_host_willing",
    "PIFF Donor": "piff_donor",
    # Not willingness — facts about what the alum has already done for the
    # program. They belong here anyway: the survey asks them, so leaving them
    # untagged left them visible ONLY inside the editor-only Tags tab, which is
    # the exact invisibility #629 was filed to end (Jake, 2026-08-05).
    "Hired a Finance Intern": "hired_finance_intern",
    "Hired a Finance Grad": "hired_finance_full_time",
}


def engagement_flag_for_tag(name: str) -> str | None:
    """Return the engagement column *name* is backed by, or ``None``.

    Case-insensitive, matching the tag filter's ``ILIKE`` comparison, so a
    deep link carrying `tag=mentor` resolves the same way the UI's `Mentor`
    does. ``None`` means "an ordinary ``alumni_tags`` tag".
    """
    folded = (name or "").strip().casefold()
    for tag_name, column in ENGAGEMENT_FLAG_TAGS.items():
        if tag_name.casefold() == folded:
            return column
    return None

# Status labels — the fixed, canonical record-status flags (alumni_status_labels
# join). Mirror in fa-web-app/src/constants/dropdowns.ts.
STATUS_LABELS: tuple[str, ...] = (
    "Inactive",
    "Deceased",
    "Lost Contact",
    "Retired",
    "Do Not Contact",
)

# Status labels that SUPPRESS outbound bulk contact (the survey send, and any
# future mailing built on the same predicate). A label here means "we must not
# email this record", NOT "we have lost touch with them":
#
#   * ``Deceased``       — the address may still be live and read by a surviving
#                          spouse; a "confirm your information" email carrying
#                          the deceased person's full record is the worst thing
#                          this system can send.
#   * ``Do Not Contact`` — an explicit, recorded request. It is the whole point
#                          of the label.
#
# ``Lost Contact``, ``Retired`` and ``Inactive`` are deliberately NOT here.
# "Lost Contact" means we WANT to reconnect and the survey is the tool for it;
# retired/inactive alumni are still ours to survey. Suppression is a narrow,
# two-value list on purpose — widening it silently un-surveys whole cohorts.
#
# Named here, next to :data:`STATUS_LABELS`, so the survey service and any
# "who would receive this?" preview share one definition and cannot drift.
SUPPRESSED_CONTACT_STATUS_LABELS: tuple[str, ...] = (
    "Deceased",
    "Do Not Contact",
)

_TAGS_SET = frozenset(TAGS)
_STATUS_LABELS_SET = frozenset(STATUS_LABELS)


def validate_tag(value: str) -> str:
    """Return *value* unchanged if it's a canonical tag; else raise ``ValueError``.

    Trims whitespace. Unlike the optional industry field a tag is required and
    must match exactly one of :data:`TAGS`.
    """
    value = (value or "").strip()
    if value not in _TAGS_SET:
        raise ValueError("Must be one of: " + ", ".join(TAGS))
    return value


def validate_status_label(value: str) -> str:
    """Return *value* unchanged if it's a canonical status label; else raise.

    Trims whitespace and requires an exact match against :data:`STATUS_LABELS`.
    """
    value = (value or "").strip()
    if value not in _STATUS_LABELS_SET:
        raise ValueError("Must be one of: " + ", ".join(STATUS_LABELS))
    return value

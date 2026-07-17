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

# Industries — current_industry, current_industry_secondary, employment_industry.
#
# ORDER IS THE DROPDOWN ORDER and is mirrored by ``vocabulary_terms.sort_order``
# (category 'industry'), where sort_order == the index below and "Other" is
# pinned to 99. ``tests/test_industry_vocab.py`` parses the migrations and fails
# if the two drift — do NOT reorder this tuple without a matching migration.
#
# Sorted case-insensitively ("Financial Services" before "FP&A"), with the
# "Other" catch-all pinned last (#282).
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
    "Private Banking",
    "Private Credit",
    "Private Equity",
    "Real Estate",
    "Sales",
    "Sales and Trading",
    "Valuation & Advisory",
    "Venture Capital",
    "Wealth Management",
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
# Sales and Trading, Credit Risk) plus any non-vocab value folds into "Other".
# Both the dashboard breakdown AND the alumni-list ``industry_group=other`` filter
# key off this set so the wheel slice and its drill-down stay in sync.
_NON_WHEEL_INDUSTRIES = frozenset(
    {"Law", "Corporate Banking", "FP&A", "Sales and Trading", "Credit Risk", "Other"}
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


# Tags — the fixed, canonical engagement tags an alumnus can be labelled with
# (alumni_tags join). Free-text is intentionally disallowed so the set stays a
# clean, filterable vocabulary. Mirror in fa-web-app/src/constants/dropdowns.ts.
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
)

# Status labels — the fixed, canonical record-status flags (alumni_status_labels
# join). Mirror in fa-web-app/src/constants/dropdowns.ts.
STATUS_LABELS: tuple[str, ...] = (
    "Inactive",
    "Deceased",
    "Lost Contact",
    "Retired",
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

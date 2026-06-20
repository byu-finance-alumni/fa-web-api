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
# "Other" is the catch-all and stays last.
INDUSTRIES: tuple[str, ...] = (
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
    "Other",
)

# Mentor industries — the same list plus Law/Government (multi-select field).
MENTOR_INDUSTRIES: tuple[str, ...] = (*INDUSTRIES, "Law/Government")

_INDUSTRIES_SET = frozenset(INDUSTRIES)


def validate_industry(value: str | None) -> str | None:
    """Return *value* unchanged if it's a valid industry (or ``None``/empty).

    Trims whitespace and normalizes empty strings to ``None``. Raises
    ``ValueError`` — which Pydantic surfaces as a 422 field error — when a
    non-empty value isn't one of :data:`INDUSTRIES`. Use as a ``mode="before"``
    field validator on industry-writing schemas.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value not in _INDUSTRIES_SET:
        raise ValueError("Must be one of: " + ", ".join(INDUSTRIES))
    return value


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

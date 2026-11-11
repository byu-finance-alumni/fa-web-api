"""Save-time data rules for "Unknown" and "Not in the Labor Force" (api#672/#671/#673).

ONE place decides these rules, and every write path calls it, so the staff form,
the per-row employment endpoints, the survey apply, the new-people approve and
the CSV import (which goes through the staff create/update service) can never
disagree about what gets stored. The one-time cleanup of records that already
exist is hand-run SQL in ``database/maintenance/`` (672-, 671-, 673-), which
restates these same patterns in Postgres regex form -- change one, change both;
``tests/test_data_rules.py`` pins the two against each other.

THE RULES (Jake, 2026-11-11)

* #672 -- a value that is JUST some spelling of "unknown" (any case, a
  misspelling like "unkown"/"unknwn", surrounding spaces) is stored as exactly
  ``Unknown``. Only the WHOLE value: "Unknown startup" is left alone. Same spirit
  as the employment-status cleanup: fix the spelling, change nothing else.
  Applied to the columns in :data:`UNKNOWN_COLUMNS`.
* #671 -- a job whose company is ``Unknown`` (after the #672 fix) gets industry
  ``Unknown``. ALWAYS, even over a real industry such as Investment Banking.
  The secondary industry is not touched.
* #673 -- a job whose company is "Not in the Labor Force" (any case/spacing,
  with or without "the", labor/labour) has the company text set to the canonical
  ``Not in the Labor Force``, industry ``Other``, secondary industry
  ``Not in the Labor Force`` and -- for the CURRENT job only, because employment
  status describes the person, not a past role -- the alum's employment status
  ``Not in the Labor Force``.

Rules apply to every job row being saved: the current job (``current_employment``)
and past jobs (``employment_history``, which has no secondary-industry column, so
only the industry rule applies there).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNKNOWN = "Unknown"
NOT_IN_LABOR_FORCE = "Not in the Labor Force"
OTHER = "Other"

# --- spellings -------------------------------------------------------------
#
# "Unknown" and its typos: lowercased + trimmed, then 5-10 letters drawn only
# from u/n/k/o/w, starting with "u" and containing both "k" and "n". That covers
# "unknown", "unkown", "unknwn", "unknow", "uknown", "unkonwn", "unknownn"... and
# no real English word (the only word spelled from those letters with a k is
# "unknown" itself), so it cannot swallow a genuine value. Mirrored in SQL by
# ``UNKNOWN_SQL_PREDICATE``.
_UNKNOWN_RE = re.compile(r"^u[unkow]{4,9}$")

# "Not in the Labor Force": internal whitespace collapsed, lowercased, optional
# "the", labor/labour, "labor force" / "labor-force" / "laborforce".
_NLF_RE = re.compile(r"^not in (the )?labou?r[ -]?force$")

# Characters treated as "surrounding spaces". Python's ``str.strip()`` also
# strips other Unicode spaces; the SQL side trims this same explicit set.
_TRIM = " \t\n\r\f\v "


def _fold(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip(_TRIM)).lower()


def is_unknown(value: object) -> bool:
    """True when *value* is just some spelling of "unknown" (and nothing else)."""
    if not isinstance(value, str):
        return False
    folded = _fold(value)
    return bool(_UNKNOWN_RE.match(folded)) and "k" in folded and "n" in folded


def is_not_in_labor_force(value: object) -> bool:
    """True when *value* is some spelling of "Not in the Labor Force"."""
    return isinstance(value, str) and bool(_NLF_RE.match(_fold(value)))


def normalize_unknown(value):
    """``Unknown`` for any spelling of unknown; anything else returned unchanged."""
    return UNKNOWN if is_unknown(value) else value


def normalize_not_in_labor_force(value):
    """The canonical ``Not in the Labor Force`` for any variant; else unchanged."""
    return NOT_IN_LABOR_FORCE if is_not_in_labor_force(value) else value


# --- which columns ------------------------------------------------------------
#
# Free-text columns where "unknown" plausibly appears, by table. The maintenance
# SQL (672-unify-unknown.sql) touches EXACTLY these; a test keeps them in step.
# Deliberately absent: names, emails, phones, URLs, notes, and ``gender`` (a
# validated vocabulary that does not include Unknown).
UNKNOWN_COLUMNS: dict[str, tuple[str, ...]] = {
    "alumni": (
        "employment_status",
        "citizenship",
        "marital_status",
        "hometown",
        "home_country",
        "graduate_degree",
        "graduate_school",
        "mba_program",
        "law_school",
        "medical_school",
    ),
    "alumni_contact_info": ("city", "state", "country", "region"),
    "current_employment": (
        "current_employer",
        "current_title",
        "current_industry",
        "current_industry_secondary",
        "current_city",
        "current_state",
        "current_country",
        "seniority_level",
    ),
    "employment_history": (
        "employer_name",
        "employment_title",
        "employment_industry",
        "city",
        "state",
    ),
    "education_history": (
        "university",
        "college",
        "department",
        "degree",
        "major",
        "degree_status",
    ),
}

# Columns whose "Not in the Labor Force" variants are canonicalised (#673): the
# two employer columns, the secondary industry it is written into, and the
# employment status that already uses that exact label.
NLF_COLUMNS: dict[str, tuple[str, ...]] = {
    "alumni": ("employment_status",),
    "current_employment": ("current_employer", "current_industry_secondary"),
    "employment_history": ("employer_name",),
}


def normalize_value(table: str, column: str, value):
    """The value as it should be STORED in ``table.column`` under #672/#673.

    Non-strings and columns not covered by the rules pass through untouched.
    Surrounding spaces are only trimmed when the value IS rewritten -- every
    other trim is the caller's own cleaning, unchanged.
    """
    if not isinstance(value, str):
        return value
    if column in UNKNOWN_COLUMNS.get(table, ()) and is_unknown(value):
        return UNKNOWN
    if column in NLF_COLUMNS.get(table, ()) and is_not_in_labor_force(value):
        return NOT_IN_LABOR_FORCE
    return value


def normalize_values(table: str, values: dict) -> dict:
    """:func:`normalize_value` over a ``{column: value}`` dict (a new dict)."""
    return {column: normalize_value(table, column, v) for column, v in values.items()}


# --- job rules (#671 / #673) --------------------------------------------------


@dataclass(frozen=True)
class JobColumns:
    """Where a job table keeps its employer / industry / secondary industry."""

    table: str
    employer: str
    industry: str
    secondary: str | None


CURRENT_JOB = JobColumns(
    "current_employment",
    "current_employer",
    "current_industry",
    "current_industry_secondary",
)
HISTORY_JOB = JobColumns("employment_history", "employer_name", "employment_industry", None)


def job_rule_updates(employer: object, cols: JobColumns) -> dict[str, str]:
    """The column values the company forces on a job row, or ``{}``.

    Keyed by *cols*' column names. Includes the canonical employer text for a
    "Not in the Labor Force" variant (#673) so the company is stored one way too.
    """
    if is_unknown(employer):
        return {cols.employer: UNKNOWN, cols.industry: UNKNOWN}
    if is_not_in_labor_force(employer):
        updates = {cols.employer: NOT_IN_LABOR_FORCE, cols.industry: OTHER}
        if cols.secondary is not None:
            updates[cols.secondary] = NOT_IN_LABOR_FORCE
        return updates
    return {}


def employment_status_for(employer: object) -> str | None:
    """The employment status the CURRENT job's company forces, or ``None``."""
    return NOT_IN_LABOR_FORCE if is_not_in_labor_force(employer) else None


def apply_job_rules(values: dict, cols: JobColumns, *, stored_employer: object = None) -> dict:
    """Normalise a ``{column: value}`` dict about to be written to a job row.

    *values* may be partial (a PATCH): the employer it is judged by is the one in
    *values* when present, else *stored_employer* (what the row already holds).
    Returns a NEW dict: the #672/#673 spelling fixes, plus the forced
    industry/secondary columns when the company calls for them.
    """
    out = normalize_values(cols.table, values)
    employer = out[cols.employer] if cols.employer in out else stored_employer
    forced = job_rule_updates(employer, cols)
    if cols.employer not in out:
        # Don't rewrite an employer the caller did not send; the stored one is
        # canonicalised by the maintenance SQL, not by an unrelated save.
        forced.pop(cols.employer, None)
    out.update(forced)
    return out


def apply_rules_to_row(row: object, cols: JobColumns) -> dict[str, tuple[object, object]]:
    """Apply the spelling fixes + job rules to an ORM job row IN PLACE.

    Returns ``{column: (old, new)}`` for every column it changed, so the caller
    can audit it. Used where the row is built or edited attribute-by-attribute
    (archiving, the per-row employment endpoints, the survey apply).
    """
    changed: dict[str, tuple[object, object]] = {}

    def _set(column: str, new: object) -> None:
        old = getattr(row, column, None)
        if old != new:
            setattr(row, column, new)
            changed[column] = (old, new)

    for column in UNKNOWN_COLUMNS.get(cols.table, ()):
        old = getattr(row, column, None)
        _set(column, normalize_value(cols.table, column, old))
    for column, new in job_rule_updates(getattr(row, cols.employer, None), cols).items():
        _set(column, new)
    return changed


# --- SQL mirror ---------------------------------------------------------------
#
# The maintenance files restate the two spellings in Postgres. Kept here as
# strings so a test can assert they match the Python patterns above.
SQL_TRIM_CHARS = "E' \\t\\n\\r\\f' || chr(11) || chr(160)"
UNKNOWN_SQL_REGEX = _UNKNOWN_RE.pattern
NLF_SQL_REGEX = _NLF_RE.pattern

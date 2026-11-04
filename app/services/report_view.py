"""How one Outreach people table is sorted and filtered (fa-web-app #917, #918).

Every NetTrek report and Outreach list renders the same table from the same
query. A :class:`ReportView` is the caller's order and filters on top of that
query; ``nettreks.report_select`` is the ONE place it is applied, so the page
and the CSV cannot disagree.

Each column sorts on what the table DISPLAYS for it, which is not always the
stored text: State shows "UT" whether the row says "Utah" or "UT", Gender shows
"M"/"F", and Phone shows a formatted number. Text compares case-insensitively.
A blank is NULL here, so ``NULLS LAST`` keeps blanks last in both directions.

Filters follow the same rule. Industry, Secondary, State, Gender and Grad Year
are exact matches on the displayed value; the rest are case-insensitive "contains"
matches. A State filter compares the stored spellings of the code instead of the
state-code ``CASE``, so a filter adds no copy of that expression (see
``geo_search.alumni_location_filter`` for why that matters).

Gender is a view_only-hidden field (``VIEW_ONLY_HIDDEN_FIELDS``). A hidden value
that still drives the order, a filter or a count can be inferred from it, so
:meth:`ReportView.for_caller` drops it for a caller who cannot edit alumni,
silently, like ``GET /alumni``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace

from sqlalchemy import String, and_, case, cast, func, literal_column, or_

from app.core.us_states import CODE_BY_NAME
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.schemas.nettreks import ReportFilterOptions, ReportSortDir
from app.schemas.nettreks import ReportSortColumn as C
from app.services.geo_search import _alumni_state_code_expr
from app.utils.sql import escape_like

#: The default order: last name, then first name, then id for a total order.
REPORT_ORDER_BY = (Alumni.last_name, Alumni.first_name, Alumni.alumni_id)


def _blank_to_null(column):
    """The trimmed text, or NULL when it is empty."""
    return func.nullif(func.trim(cast(column, String)), "")


def _folded(column):
    return func.lower(_blank_to_null(column))


def _state_key():
    """The folded state code the table's "UT" comes from; NULL when blank."""
    return func.nullif(_alumni_state_code_expr(), "")


def _gender_key():
    """"M", "F" or NULL: the app's ``genderLabel``."""
    initial = func.upper(func.substr(func.trim(cast(Alumni.gender, String)), 1, 1))
    return case((initial.in_(("M", "F")), initial))


def _phone_digits():
    """The digits of the phone, without a US country code: the app's
    ``formatPhone`` on a number it recognizes. NULL when there are no digits."""
    digits = func.regexp_replace(
        cast(AlumniContactInfo.phone, String), "[^0-9]", "", literal_column("'g'")
    )
    return case(
        (
            and_(func.length(digits) == 11, func.substr(digits, 1, 1) == "1"),
            func.substr(digits, 2),
        ),
        else_=func.nullif(digits, ""),
    )


def _sort_key(column: C):
    match column:
        case C.company_state:
            return _state_key()
        case C.gender:
            return _gender_key()
        case C.phone:
            return _phone_digits()
        case C.graduation_year:
            return Alumni.graduation_year
    return _folded(_TEXT_COLUMNS[column])


_TEXT_COLUMNS = {
    C.first_name: Alumni.first_name,
    C.middle_name: Alumni.middle_name,
    C.last_name: Alumni.last_name,
    C.company: CurrentEmployment.current_employer,
    C.industry: CurrentEmployment.current_industry,
    C.secondary_industry: CurrentEmployment.current_industry_secondary,
    C.company_city: CurrentEmployment.current_city,
    C.permanent_email: AlumniContactInfo.personal_email,
    C.work_email: AlumniContactInfo.work_email,
    C.linkedin_url: Alumni.linkedin_url,
}


# --- audit ------------------------------------------------------------------

# Characters an audit value must not carry: control characters (newlines
# included) and ``;``, the field separator of the audit detail line.
_AUDIT_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f;]+")


def audit_safe(value: str) -> str:
    return _AUDIT_UNSAFE.sub(" ", value).strip()


# --- filters ----------------------------------------------------------------

_GENDERS = ("M", "F")

#: Every stored spelling of each state code, lower-cased.
_STATE_SPELLINGS: dict[str, tuple[str, ...]] = {}
for _name, _code in CODE_BY_NAME.items():
    _STATE_SPELLINGS[_code] = (*_STATE_SPELLINGS.get(_code, (_code.lower(),)), _name)


def _fold_text(value: str) -> str:
    return value.strip().lower()


def _fold_state(value: str) -> str:
    """Python's ``_alumni_state_code_expr``: the code, or the upper-cased value."""
    trimmed = value.strip()
    return CODE_BY_NAME.get(trimmed.lower(), trimmed.upper())


def _fold_gender(value: str) -> str:
    """Python's :func:`_gender_key`."""
    initial = value.strip()[:1].upper()
    return initial if initial in _GENDERS else ""


#: How a dropdown column's value compares. The filter value and the options go
#: through the same fold, so a choice always matches what it was built from.
_DROPDOWN_FOLDS = {
    C.industry: _fold_text,
    C.secondary_industry: _fold_text,
    C.company_state: _fold_state,
    C.gender: _fold_gender,
}


def _state_condition(code: str):
    spellings = _STATE_SPELLINGS.get(code, (code.lower(),))
    state = func.lower(func.trim(cast(CurrentEmployment.current_state, String)))
    return state.in_(spellings)


def _contains(column, term: str):
    like = f"%{escape_like(term)}%"
    return _blank_to_null(column).ilike(like, escape="\\")


def _phone_condition(term: str):
    """The phone as typed, or its digits however they are punctuated, so "(801)
    555" finds a stored "801-555-1234"."""
    digits = re.sub(r"[^0-9]", "", term)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    typed = _contains(AlumniContactInfo.phone, term)
    return or_(typed, _phone_digits().like(f"%{digits}%")) if digits else typed


def _condition(column: C, value: str | int):
    match column:
        case C.company_state:
            return _state_condition(str(value))
        case C.gender:
            return _gender_key() == value
        case C.graduation_year:
            return Alumni.graduation_year == value
        case C.industry | C.secondary_industry:
            return _folded(_TEXT_COLUMNS[column]) == value
        case C.phone:
            return _phone_condition(str(value))
    return _contains(_TEXT_COLUMNS[column], str(value))


def clean_filters(raw: Mapping[C, str | int | None]) -> dict[C, str | int]:
    """Drop what is not set (a blank box is no filter) and put each value in
    the form it compares in."""
    out: dict[C, str | int] = {}
    for column, value in raw.items():
        if value is None:
            continue
        if isinstance(value, str):
            value = _DROPDOWN_FOLDS.get(column, str.strip)(value)
            if not value:
                continue
        out[column] = value
    return out


# --- the view -----------------------------------------------------------------


@dataclass(frozen=True)
class ReportView:
    """One request's sort and filters. No ``sort`` is the default order,
    whatever ``dir``. ``filters`` come from :func:`clean_filters`."""

    sort: C | None = None
    direction: ReportSortDir = ReportSortDir.asc
    filters: Mapping[C, str | int] = field(default_factory=dict)

    def for_caller(self, *, can_edit: bool) -> ReportView:
        """This view without what the caller may not see."""
        if can_edit:
            return self
        return replace(
            self,
            sort=None if self.sort is C.gender else self.sort,
            filters={c: v for c, v in self.filters.items() if c is not C.gender},
        )

    @property
    def is_filtered(self) -> bool:
        return bool(self.filters)

    def audit_suffix(self) -> str:
        """``; filters={...}`` for the audit detail line, or nothing with no
        filters: what narrowed the list, never the rows. Every value is caller
        text, so it is reduced to one ``;``-free line and JSON-quoted, and
        cannot add or alter a field of the line."""
        if not self.filters:
            return ""
        used = {c.value: audit_safe(str(v)) for c, v in self.filters.items()}
        return f"; filters={json.dumps(used, ensure_ascii=False)}"

    def conditions(self) -> list:
        """The WHERE clauses of every active filter."""
        return [_condition(c, v) for c, v in self.filters.items()]

    def text_conditions(self) -> list:
        """The filters that are not dropdowns."""
        return [_condition(c, v) for c, v in self.filters.items() if c not in DROPDOWNS]

    def order_by(self) -> tuple:
        """The sort, then the default order so paging never repeats or skips
        anyone (``alumni_id`` makes the order total)."""
        if self.sort is None:
            return REPORT_ORDER_BY
        key = _sort_key(self.sort)
        descending = self.direction is ReportSortDir.desc
        return ((key.desc() if descending else key.asc()).nullslast(), *REPORT_ORDER_BY)


# --- dropdown options -----------------------------------------------------------

#: The dropdown columns: what to read for each, in the order rows carry them.
DROPDOWNS: dict[C, object] = {
    C.industry: CurrentEmployment.current_industry,
    C.secondary_industry: CurrentEmployment.current_industry_secondary,
    C.company_state: CurrentEmployment.current_state,
    C.gender: Alumni.gender,
    C.graduation_year: Alumni.graduation_year,
}
_KEEP_SPELLING = (C.industry, C.secondary_industry)


def _key(column: C, value) -> str | int | None:
    """A row's value as a filter on ``column`` compares it, ``None`` for blank."""
    if value is None or column is C.graduation_year:
        return value
    return _DROPDOWN_FOLDS[column](str(value)) or None


def dropdown_options(
    rows: Iterable[tuple], view: ReportView, *, can_edit: bool
) -> ReportFilterOptions:
    """The choices for each dropdown, from the distinct :data:`DROPDOWNS` rows
    of a list with the view's text filters applied. A column's choices honor
    every OTHER dropdown filter and ignore its own, so someone can change their
    mind without clearing it, and no choice leads to an empty table."""
    columns = tuple(DROPDOWNS)
    active = {c: v for c, v in view.filters.items() if c in DROPDOWNS}
    found: dict[C, dict[str | int, str | int]] = {c: {} for c in columns}
    for row in rows:
        keys = {c: _key(c, v) for c, v in zip(columns, row, strict=True)}
        misses = {c for c, want in active.items() if keys[c] != want}
        for column, value in zip(columns, row, strict=True):
            key = keys[column]
            if key is None or misses - {column}:
                continue
            # Industries keep their stored casing, one choice per fold.
            label = str(value).strip() if column in _KEEP_SPELLING else key
            found[column][key] = min(found[column].get(key, label), label)

    def choices(column: C) -> list:
        return sorted(found[column].values(), key=lambda v: str(v).lower())

    return ReportFilterOptions(
        industry=choices(C.industry),
        secondary_industry=choices(C.secondary_industry),
        company_state=choices(C.company_state),
        gender=choices(C.gender) if can_edit else None,
        graduation_year=sorted(found[C.graduation_year].values(), reverse=True),
    )

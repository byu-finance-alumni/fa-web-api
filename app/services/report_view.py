"""How one Outreach people table is sorted (fa-web-app #917).

Every NetTrek report and Outreach list renders the same table from the same
query. A :class:`ReportView` is the caller's choice of order on top of that
query; ``nettreks.report_select`` is the ONE place it is applied, so the page
and the CSV cannot disagree.

Each column sorts on what the table DISPLAYS for it, which is not always the
stored text: State shows "UT" whether the row says "Utah" or "UT", Gender shows
"M"/"F", and Phone shows a formatted number. Text compares case-insensitively.
A blank is NULL here, so ``NULLS LAST`` keeps blanks last in both directions.

Gender is a view_only-hidden field (``VIEW_ONLY_HIDDEN_FIELDS``). A hidden value
that still drives the order can be inferred from it, so :meth:`ReportView.for_caller`
drops it for a caller who cannot edit alumni, silently, like ``GET /alumni``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from sqlalchemy import String, and_, case, cast, func, literal_column

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.schemas.nettreks import ReportSortColumn as C
from app.schemas.nettreks import ReportSortDir
from app.services.geo_search import _alumni_state_code_expr

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


@dataclass(frozen=True)
class ReportView:
    """One request's sort. No ``sort`` is the default order, whatever ``dir``."""

    sort: C | None = None
    direction: ReportSortDir = ReportSortDir.asc

    def for_caller(self, *, can_edit: bool) -> ReportView:
        """This view without what the caller may not see."""
        if can_edit or self.sort is not C.gender:
            return self
        return replace(self, sort=None)

    def order_by(self) -> tuple:
        """The sort, then the default order so paging never repeats or skips
        anyone (``alumni_id`` makes the order total)."""
        if self.sort is None:
            return REPORT_ORDER_BY
        key = _sort_key(self.sort)
        descending = self.direction is ReportSortDir.desc
        return ((key.desc() if descending else key.asc()).nullslast(), *REPORT_ORDER_BY)

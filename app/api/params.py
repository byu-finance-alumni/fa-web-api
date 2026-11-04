"""Shared path- and query-parameter types for API routes.

Numeric ``{..._id}`` path params are bounded so an id outside the PostgreSQL
``bigint`` range is rejected with a 422 *before* it reaches asyncpg. A bare
``int`` path param has no upper bound, so a value beyond int64 fails at the
asyncpg bind stage and surfaces as a 500 instead of a clean validation error
(#185). ``IdPath`` applies the same ``ge=1`` .. ``INT64_MAX`` bound everywhere;
the parameter name is still taken from the function argument, so one alias
covers every id (``alumni_id``, ``event_id``, ``donation_id``, ...).
"""

from typing import Annotated

from fastapi import Depends, Path, Query

from app.schemas.nettreks import ReportSortColumn, ReportSortDir
from app.services.report_view import ReportView, clean_filters

# PostgreSQL ``bigint`` / ``bigserial`` ceiling — the largest id the DB can hold.
INT64_MAX = 9223372036854775807

# A positive-identity path id, bounded to the bigint range. Ids are 1-based
# identities, so ``ge=1`` also rejects 0 / negative segments up front.
IdPath = Annotated[int, Path(ge=1, le=INT64_MAX)]


# Control characters (NUL, CR/LF, ...) never belong in a filter; refusing them
# here returns a 422 instead of failing in the database layer.
_PLAIN_TEXT = r"^[^\x00-\x1f\x7f-\x9f]*$"
_TextFilter = Annotated[str | None, Query(max_length=100, pattern=_PLAIN_TEXT)]
_ChoiceFilter = Annotated[str | None, Query(max_length=255, pattern=_PLAIN_TEXT)]


def report_view_params(
    sort: ReportSortColumn | None = None,
    direction: Annotated[ReportSortDir, Query(alias="dir")] = ReportSortDir.asc,
    f_first_name: _TextFilter = None,
    f_middle_name: _TextFilter = None,
    f_last_name: _TextFilter = None,
    f_company: _TextFilter = None,
    f_industry: _ChoiceFilter = None,
    f_secondary_industry: _ChoiceFilter = None,
    f_company_city: _TextFilter = None,
    f_company_state: _ChoiceFilter = None,
    f_permanent_email: _TextFilter = None,
    f_work_email: _TextFilter = None,
    f_phone: _TextFilter = None,
    f_linkedin_url: _TextFilter = None,
    f_gender: Annotated[str | None, Query(pattern=r"^[MF]$")] = None,
    f_graduation_year: Annotated[int | None, Query(ge=1, le=9999)] = None,
) -> ReportView:
    """The sort and filters of an Outreach people table, the same on the page
    and the CSV routes of the NetTrek reports and the Outreach lists. ``f_*``
    are named after the columns; a list route's own ``industry`` (which Alumni
    Mentors list) is a different thing, hence the prefix."""
    return ReportView(
        sort=sort,
        direction=direction,
        filters=clean_filters(
            {
                ReportSortColumn.first_name: f_first_name,
                ReportSortColumn.middle_name: f_middle_name,
                ReportSortColumn.last_name: f_last_name,
                ReportSortColumn.company: f_company,
                ReportSortColumn.industry: f_industry,
                ReportSortColumn.secondary_industry: f_secondary_industry,
                ReportSortColumn.company_city: f_company_city,
                ReportSortColumn.company_state: f_company_state,
                ReportSortColumn.permanent_email: f_permanent_email,
                ReportSortColumn.work_email: f_work_email,
                ReportSortColumn.phone: f_phone,
                ReportSortColumn.linkedin_url: f_linkedin_url,
                ReportSortColumn.gender: f_gender,
                ReportSortColumn.graduation_year: f_graduation_year,
            }
        ),
    )


ReportViewParam = Annotated[ReportView, Depends(report_view_params)]

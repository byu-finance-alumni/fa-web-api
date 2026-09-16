"""Employer display fallback (#536): no company -> show the employment status.

Jake's decisions (2026-09-15/16): a DISPLAY rule, stored data untouched; for
grad school the company field holds the school's NAME; the fallback applies to
the alumni list, the profile page and the CSV export (one rule, one place —
export/list parity is a recurring defect here); and it applies to every status
except the ones that mean "employed".

Pinned here:

* the pure function, exhaustively over every dropdown value x every "blank"
  company shape, plus casing/whitespace drift and off-list legacy values;
* the split of the canonical list into employed / fallback statuses;
* ``AlumniListItem.employer_display`` and ``ProfileRead.employer_display``,
  including the no-career-row profile and the view_only minimizers;
* the CSV export's "Current employer" cell equals the list row's
  ``employer_display`` for the same alumnus — the parity the issue exists for;
* the dashboard birthday row and the geography drill-down rows carry it too.

Offline: schemas are exercised as plain objects, the export through the route
with a fake session. No database.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.dropdowns import (
    EMPLOYED_STATUSES,
    EMPLOYER_FALLBACK_BY_LOWER,
    EMPLOYER_FALLBACK_STATUSES,
    EMPLOYMENT_STATUSES,
)
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.employment import CurrentEmployment
from app.schemas.alumni import AlumniListItem, AlumniRead, minimize_alumni_read
from app.schemas.auth import UserContext
from app.schemas.dashboard import BirthdayRow
from app.schemas.geography import CityAlumniRow, GeoAlumniRow, RadiusAlumniRow
from app.schemas.profile import CurrentCareerRead, ProfileRead
from app.services import geography as geography_service
from app.services.employment_display import employer_display
from app.services.profile import _minimize_profile_for_view_only

# --- the status split ---------------------------------------------------------

_EMPLOYED = ("Full-time", "Part-time", "Self-Employed")
_FALLBACK = (
    "Graduate Student",
    "Military",
    "Not in the Labor Force",
    "Unemployed",
    "Unknown",
)
_BLANK_COMPANIES = (None, "", " ", "   ", "\t", "\n ")


def test_employed_statuses_are_exactly_the_three_that_mean_employed() -> None:
    assert EMPLOYED_STATUSES == _EMPLOYED


def test_fallback_statuses_are_every_other_dropdown_value() -> None:
    assert EMPLOYER_FALLBACK_STATUSES == _FALLBACK
    # Every canonical value lands in exactly one of the two sets.
    assert set(EMPLOYED_STATUSES) | set(EMPLOYER_FALLBACK_STATUSES) == set(EMPLOYMENT_STATUSES)
    assert not set(EMPLOYED_STATUSES) & set(EMPLOYER_FALLBACK_STATUSES)


def test_fallback_lookup_maps_lowercase_to_canonical_label() -> None:
    assert EMPLOYER_FALLBACK_BY_LOWER == {v.lower(): v for v in _FALLBACK}


# --- the pure function --------------------------------------------------------


@pytest.mark.parametrize("status", EMPLOYMENT_STATUSES)
@pytest.mark.parametrize("company", _BLANK_COMPANIES)
def test_blank_company_every_dropdown_value(company: str | None, status: str) -> None:
    expected = status if status in _FALLBACK else None
    assert employer_display(company, status) == expected


@pytest.mark.parametrize("status", (*EMPLOYMENT_STATUSES, None, "", "Employed"))
def test_company_present_always_wins(status: str | None) -> None:
    # Returned exactly as stored — no trimming, no case change.
    assert employer_display("Goldman Sachs", status) == "Goldman Sachs"
    assert employer_display("  Goldman Sachs ", status) == "  Goldman Sachs "


@pytest.mark.parametrize("company", _BLANK_COMPANIES)
def test_both_blank_is_none(company: str | None) -> None:
    assert employer_display(company, None) is None
    assert employer_display(company, "") is None
    assert employer_display(company, "   ") is None


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("graduate student", "Graduate Student"),
        ("GRADUATE STUDENT", "Graduate Student"),
        ("  Graduate Student  ", "Graduate Student"),
        ("\tunemployed\n", "Unemployed"),
        ("not in the labor force", "Not in the Labor Force"),
        ("MILITARY", "Military"),
        ("unknown", "Unknown"),
    ],
)
def test_status_is_case_and_whitespace_tolerant_and_canonicalised(stored: str, shown: str) -> None:
    """The column has no write validation, so prod holds casing drift; the
    display uses the dropdown's spelling regardless."""
    assert employer_display(None, stored) == shown


@pytest.mark.parametrize(
    "stored",
    ["full-time", "  PART-TIME ", "self-employed", "Self-employed"],
)
def test_employed_statuses_get_no_fallback_regardless_of_casing(stored: str) -> None:
    assert employer_display(None, stored) is None
    assert employer_display("", stored) is None


@pytest.mark.parametrize(
    "off_list",
    ["Employed", "Stay at home parent", "Retired", "Seeking employment", "In graduate school"],
)
def test_off_list_legacy_values_are_not_guessed(off_list: str) -> None:
    """The rule is an allow-list over the dropdown: an off-list stored value is
    left blank rather than shown as if it were a status we recognise."""
    assert employer_display(None, off_list) is None


def test_grad_school_with_school_name_shows_the_school() -> None:
    """The issue's own case: the company field holds the school's NAME, so the
    fallback only fires when it is empty."""
    assert employer_display("Wharton", "Graduate Student") == "Wharton"
    assert employer_display(None, "Graduate Student") == "Graduate Student"


# --- AlumniListItem -----------------------------------------------------------


def _alumni_model(**kw) -> Alumni:
    now = datetime.datetime(2026, 9, 16, tzinfo=datetime.UTC)
    base = dict(
        alumni_id=1,
        first_name="Jane",
        last_name="Doe",
        graduation_year=2020,
        employment_status="Graduate Student",
        deceased=False,
        is_alumni=True,
        archived=False,
        created_at=now,
        updated_at=now,
    )
    base.update(kw)
    return Alumni(**base)


def _list_item(*, company: str | None, status: str | None) -> AlumniListItem:
    # The repository sets the joined columns as plain instance attributes on the
    # ORM row before the route validates it — mirror that exactly.
    row = _alumni_model(employment_status=status)
    row.current_employer = company
    return AlumniListItem.model_validate(row)


def test_list_item_blank_company_grad_student_shows_status() -> None:
    item = _list_item(company=None, status="Graduate Student")
    assert item.current_employer is None  # stored value untouched
    assert item.employer_display == "Graduate Student"
    assert item.model_dump()["employer_display"] == "Graduate Student"


def test_list_item_company_present_shows_company() -> None:
    item = _list_item(company="Goldman Sachs", status="Graduate Student")
    assert item.employer_display == "Goldman Sachs"


def test_list_item_employed_with_blank_company_is_none() -> None:
    assert _list_item(company="", status="Full-time").employer_display is None
    assert _list_item(company=None, status="Self-Employed").employer_display is None


def test_list_item_no_status_no_company_is_none() -> None:
    assert _list_item(company=None, status=None).employer_display is None


def test_list_item_display_is_read_only_and_derived() -> None:
    """It is computed from the row's own fields, so a caller cannot set it and
    a ``model_copy`` that changes the inputs changes it too."""
    item = _list_item(company=None, status="Unemployed")
    assert item.employer_display == "Unemployed"
    assert item.model_copy(update={"current_employer": "Acme"}).employer_display == "Acme"
    assert "employer_display" not in AlumniListItem.model_fields
    schema = AlumniListItem.model_json_schema(mode="serialization")
    assert schema["properties"]["employer_display"]["readOnly"] is True


def test_list_item_survives_view_only_minimization() -> None:
    """``employment_status`` is deliberately visible to view_only, so the
    display value is too."""
    item = _list_item(company=None, status="Military")
    scoped = minimize_alumni_read(item, can_edit=False)
    assert scoped.employer_display == "Military"
    assert scoped.model_dump()["employer_display"] == "Military"


# --- ProfileRead --------------------------------------------------------------


def _profile(*, company: str | None, status: str | None, career: bool = True) -> ProfileRead:
    alumni = AlumniRead.model_validate(_alumni_model(employment_status=status))
    current_career = (
        CurrentCareerRead(current_employment_id=11, current_employer=company) if career else None
    )
    return ProfileRead(alumni=alumni, current_career=current_career)


def test_profile_blank_company_grad_student_shows_status() -> None:
    profile = _profile(company=None, status="Graduate Student")
    assert profile.current_career.current_employer is None  # stored value untouched
    assert profile.employer_display == "Graduate Student"
    assert profile.model_dump()["employer_display"] == "Graduate Student"


def test_profile_company_present_shows_company() -> None:
    assert _profile(company="Wharton", status="Graduate Student").employer_display == "Wharton"


def test_profile_employed_with_blank_company_is_none() -> None:
    assert _profile(company="  ", status="Part-time").employer_display is None


def test_profile_with_no_career_row_still_falls_back() -> None:
    """The reason the field sits on the aggregate: an alumnus with no
    ``current_employment`` row at all still has a status to show."""
    assert _profile(company=None, status="Unemployed", career=False).employer_display == (
        "Unemployed"
    )
    assert _profile(company=None, status="Full-time", career=False).employer_display is None


def test_profile_display_survives_view_only_minimization() -> None:
    scoped = _minimize_profile_for_view_only(
        _profile(company=None, status="Not in the Labor Force")
    )
    assert scoped.employer_display == "Not in the Labor Force"
    assert scoped.model_dump()["employer_display"] == "Not in the Labor Force"


def test_profile_and_list_agree_for_the_same_alumnus() -> None:
    for company, status in [
        (None, "Graduate Student"),
        ("Acme", "Graduate Student"),
        (None, "Full-time"),
        ("", "unknown"),
        (None, "Stay at home parent"),
    ]:
        assert (
            _profile(company=company, status=status).employer_display
            == _list_item(company=company, status=status).employer_display
        )


# --- CSV export <-> list parity ----------------------------------------------


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles) or ["full_access"],
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """Export loads the alumni page first, then each selected side table."""

    def __init__(self, *, count: int, execute_results):
        self._count = count
        self._results = list(execute_results)
        self.added: list = []

    async def scalar(self, stmt):
        return self._count

    async def execute(self, stmt):
        return _FakeResult(self._results.pop(0) if self._results else [])

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass


def _with_session(session):
    async def _override():
        yield session

    return _override


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _export_rows(client, alumni, career_rows) -> list[list[str]]:
    session = _FakeSession(count=len(alumni), execute_results=[alumni, career_rows])
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    app.dependency_overrides[get_session] = _with_session(session)
    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name", "employment_status", "current_employer"]},
    )
    assert response.status_code == 200, response.text
    assert [a for a in session.added if isinstance(a, AuditLog)]
    return list(csv.reader(io.StringIO(response.text)))


def test_export_employer_column_matches_list_display(client) -> None:
    """One alumnus per case; the CSV's "Current employer" cell must equal the
    list row's ``employer_display`` — and the raw status column stays raw."""
    cases = [
        # (alumni_id, stored company, stored status, expected display)
        (1, None, "Graduate Student", "Graduate Student"),
        (2, "Wharton", "Graduate Student", "Wharton"),
        (3, "", "Full-time", None),
        (4, None, "unemployed", "Unemployed"),
        (5, "   ", "Stay at home parent", None),
        (6, None, None, None),
    ]
    alumni = [
        _alumni_model(alumni_id=i, first_name=f"A{i}", employment_status=status)
        for i, _, status, _ in cases
    ]
    career_rows = [
        CurrentEmployment(alumni_id=i, current_employer=company) for i, company, _, _ in cases
    ]
    rows = _export_rows(client, alumni, career_rows)
    assert rows[0] == ["First name", "Employment status", "Current employer"]
    for (i, company, status, expected), row in zip(cases, rows[1:], strict=True):
        assert row[0] == f"A{i}"
        # The raw status column is untouched by the display rule.
        assert row[1] == (status or "")
        assert row[2] == (expected or ""), (i, company, status)
        # Parity: the same alumnus in the list shows the same value.
        assert _list_item(company=company, status=status).employer_display == expected


def test_export_missing_career_row_falls_back_like_the_list(client) -> None:
    """No ``current_employment`` row at all: the list's correlated subquery
    yields NULL and falls back to the status, so the export must too."""
    alumni = [_alumni_model(alumni_id=9, first_name="Nine", employment_status="Military")]
    rows = _export_rows(client, alumni, career_rows=[])
    assert rows[1] == ["Nine", "Military", "Military"]
    assert _list_item(company=None, status="Military").employer_display == "Military"


def test_export_display_value_is_still_formula_neutralised(client) -> None:
    """The display value goes through the same CSV-injection guard as any other
    free-text cell."""
    alumni = [_alumni_model(alumni_id=1, employment_status="Full-time")]
    career_rows = [CurrentEmployment(alumni_id=1, current_employer='=HYPERLINK("x")')]
    rows = _export_rows(client, alumni, career_rows)
    assert rows[1][2] == '\t=HYPERLINK("x")'


# --- dashboard + geography rows ------------------------------------------------


@pytest.mark.parametrize("model", [BirthdayRow, GeoAlumniRow, RadiusAlumniRow, CityAlumniRow])
def test_result_row_schemas_carry_employer_display(model) -> None:
    assert "employer_display" in model.model_fields
    assert model.model_fields["employer_display"].default is None


class _GeoSession:
    """``get_state_alumni`` / ``get_country_alumni`` / ``get_city_detail`` do
    one ``scalar`` (the count) and then ``execute`` the row query."""

    def __init__(self, rows):
        self._rows = rows

    async def scalar(self, stmt):
        return len(self._rows)

    async def execute(self, stmt):
        return _FakeResult(self._rows)


def _geo_alum(alumni_id: int, status: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        alumni_id=alumni_id,
        first_name="Jane",
        last_name="Doe",
        preferred_first_name=None,
        graduation_year=2020,
        employment_status=status,
    )


def test_geography_state_page_rows_carry_the_display_value() -> None:
    rows = [
        (_geo_alum(1, "Graduate Student"), "Provo", None, "Student"),
        (_geo_alum(2, "Graduate Student"), "Provo", "BYU", "Student"),
        (_geo_alum(3, "Full-time"), "Provo", None, "Analyst"),
    ]
    page = asyncio.run(
        geography_service.get_state_alumni(
            _GeoSession(rows), "UT", {}, limit=50, offset=0, sort="name"
        )
    )
    shown = [(i["current_employer"], i["employer_display"]) for i in page["items"]]
    assert shown == [(None, "Graduate Student"), ("BYU", "BYU"), (None, None)]
    # Validates against the response schema the route declares.
    for item in page["items"]:
        GeoAlumniRow.model_validate(item)

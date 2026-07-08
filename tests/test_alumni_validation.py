"""Field-level (semantic) validation tests for AlumniCreate / AlumniUpdate.

Parameterized queries stop injection from *executing*; these tests assert the
schema also keeps SQL-shaped garbage out of the data, while still accepting
legitimate (including international) names. They exercise the schemas directly
and through POST /alumni so the 422 envelope (with per-field ``fields``) is
covered end to end.
"""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.alumni import AlumniCreate, AlumniRead, AlumniUpdate
from app.schemas.auth import UserContext


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# --- Schema-level tests ------------------------------------------------------


def test_valid_create_passes():
    model = AlumniCreate(
        byu_id="123456789",
        net_id="JDoe12",
        first_name="  Jane  ",
        last_name="Doe",
        graduation_year=2020,
        linkedin_url="https://www.linkedin.com/in/janedoe",
        gender="Female",
    )
    assert model.first_name == "Jane"  # trimmed
    assert model.net_id == "jdoe12"  # lowercased
    assert model.byu_id == "123456789"


def test_sql_injection_name_rejected():
    with pytest.raises(ValidationError) as exc:
        AlumniCreate(first_name="' OR 1=1;--")
    # Fails on the disallowed ; / = characters.
    assert "characters" in str(exc.value)


@pytest.mark.parametrize("name", ["O'Brien", "Anne-Marie", "St. John", "José"])
def test_legitimate_names_accepted(name):
    model = AlumniCreate(last_name=name)
    assert model.last_name == name


def test_digits_only_name_rejected():
    with pytest.raises(ValidationError):
        AlumniCreate(first_name="12345")


def test_control_char_name_rejected():
    with pytest.raises(ValidationError):
        AlumniCreate(first_name="Bad\x00Name")


@pytest.mark.parametrize("name", ["+1+1", "-2", "@SUM(1)", "=cmd|'/c calc'!A1"])
def test_name_leading_formula_char_rejected(name):
    # CSV/formula-injection defense at the source (#169): a name that would
    # become a live spreadsheet formula on export is rejected up front.
    with pytest.raises(ValidationError):
        AlumniCreate(first_name=name)


def test_name_hyphen_midword_still_accepted():
    # Only a LEADING +/-/@ is blocked; hyphenated names remain valid.
    assert AlumniCreate(last_name="Smith-Jones").last_name == "Smith-Jones"


def test_byu_id_with_dashes_is_cleaned():
    # A formatted BYU id is digit-stripped rather than hard-rejected (#176).
    assert AlumniCreate(byu_id="900-11-2233").byu_id == "900112233"


@pytest.mark.parametrize(
    "byu_id", ["12345678", "1234567890", "12345678a", "abcdefghi"]
)
def test_bad_byu_id_rejected(byu_id):
    with pytest.raises(ValidationError):
        AlumniCreate(byu_id=byu_id)


@pytest.mark.parametrize("net_id", ["a", "thisistoolongnetid", "bad id", "bad!"])
def test_bad_net_id_rejected(net_id):
    with pytest.raises(ValidationError):
        AlumniCreate(net_id=net_id, last_name="Doe")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/in/x",
        "ftp://linkedin.com/in/x",
        "https://notlinkedin.com/in/x",
        "https://linkedin.com.evil.com/in/x",
        "not a url",
    ],
)
def test_bad_linkedin_host_rejected(url):
    with pytest.raises(ValidationError):
        AlumniCreate(last_name="Doe", linkedin_url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/in/x",
        "https://www.linkedin.com/in/x",
        "http://uk.linkedin.com/in/x",
    ],
)
def test_good_linkedin_host_accepted(url):
    model = AlumniCreate(last_name="Doe", linkedin_url=url)
    assert model.linkedin_url == url


@pytest.mark.parametrize("year", [1949, datetime.date.today().year + 11, 0])
def test_out_of_range_year_rejected(year):
    with pytest.raises(ValidationError):
        AlumniCreate(last_name="Doe", graduation_year=year)


@pytest.mark.parametrize("month", [1, 4, 8, 12])
def test_valid_graduation_month_accepted(month):
    model = AlumniCreate(last_name="Doe", graduation_month=month)
    assert model.graduation_month == month


def test_graduation_month_defaults_to_none():
    model = AlumniCreate(last_name="Doe")
    assert model.graduation_month is None


@pytest.mark.parametrize("month", [0, 13, -1, 99])
def test_out_of_range_graduation_month_rejected(month):
    with pytest.raises(ValidationError):
        AlumniCreate(last_name="Doe", graduation_month=month)


def test_empty_strings_normalize_to_none():
    model = AlumniUpdate(
        first_name="Doe",
        middle_name="   ",
        gender="",
        notes="",
        linkedin_url="",
    )
    assert model.middle_name is None
    assert model.gender is None
    assert model.notes is None
    assert model.linkedin_url is None


# --- Secondary affiliation / education fields (#47) --------------------------


def test_secondary_affiliation_fields_round_trip():
    model = AlumniCreate(
        last_name="Doe",
        mba_program="  BYU Marriott MBA  ",
        law_school="Harvard Law",
        medical_school="Johns Hopkins",
        graduate_school="MIT",
        startup_involvement="  Co-founded Acme (2021)  ",
        advisory_roles="Board advisor at Foo Inc.",
        secondary_employment="Adjunct professor, evenings",
    )
    # Strings are trimmed; values persist on the model.
    assert model.mba_program == "BYU Marriott MBA"
    assert model.law_school == "Harvard Law"
    assert model.medical_school == "Johns Hopkins"
    assert model.graduate_school == "MIT"
    assert model.startup_involvement == "Co-founded Acme (2021)"
    assert model.advisory_roles == "Board advisor at Foo Inc."
    assert model.secondary_employment == "Adjunct professor, evenings"


def test_secondary_affiliation_blank_normalizes_to_none():
    model = AlumniUpdate(
        mba_program="",
        law_school="   ",
        startup_involvement="",
        advisory_roles="   ",
    )
    assert model.mba_program is None
    assert model.law_school is None
    assert model.startup_involvement is None
    assert model.advisory_roles is None


def test_secondary_affiliation_name_too_long_rejected():
    with pytest.raises(ValidationError):
        AlumniCreate(last_name="Doe", mba_program="x" * 256)


def test_secondary_affiliation_control_char_rejected():
    with pytest.raises(ValidationError):
        AlumniCreate(last_name="Doe", law_school="Harvard\x00Law")


def test_alumni_read_surfaces_secondary_affiliation():
    # GET /{id}/profile serializes the alumni core via AlumniRead.model_validate;
    # confirm the new #47 fields are present on the read model (from_attributes).
    orm_like = SimpleNamespace(
        alumni_id=1,
        deceased=False,
        archived=False,
        created_at=datetime.datetime(2026, 6, 12, tzinfo=datetime.UTC),
        updated_at=datetime.datetime(2026, 6, 12, tzinfo=datetime.UTC),
        mba_program="Wharton MBA",
        law_school="Yale Law",
        medical_school=None,
        graduate_school="Stanford",
        startup_involvement="Founder",
        advisory_roles="Advisor",
        secondary_employment="Consultant",
    )
    read = AlumniRead.model_validate(orm_like)
    assert read.mba_program == "Wharton MBA"
    assert read.law_school == "Yale Law"
    assert read.medical_school is None
    assert read.graduate_school == "Stanford"
    assert read.startup_involvement == "Founder"
    assert read.advisory_roles == "Advisor"
    assert read.secondary_employment == "Consultant"


# --- Route-level tests (422 envelope with per-field details) -----------------


def test_route_rejects_injection_name_with_field_details(client):
    response = client.post(
        "/alumni", json={"first_name": "' OR 1=1;--", "last_name": "Doe"}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    fields = body["error"]["fields"]
    assert isinstance(fields, list) and fields
    assert any(f["field"] == "first_name" for f in fields)
    # The submitted value must never be echoed back.
    assert "OR 1=1" not in response.text


def test_route_accepts_obrien_passes_validation():
    # raise_server_exceptions=False so the no-DB session surfaces as a 500
    # response rather than propagating; the point is only that validation
    # passed (no 422).
    app.dependency_overrides[get_session] = _no_db_session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            response = test_client.post("/alumni", json={"last_name": "O'Brien"})
        assert response.status_code != 422
    finally:
        app.dependency_overrides.clear()


def test_route_rejects_bad_byu_id(client):
    response = client.post("/alumni", json={"byu_id": "123"})
    assert response.status_code == 422
    fields = response.json()["error"]["fields"]
    assert any(f["field"] == "byu_id" for f in fields)


def test_route_rejects_bad_linkedin_host(client):
    response = client.post(
        "/alumni",
        json={"last_name": "Doe", "linkedin_url": "https://evil.com/in/x"},
    )
    assert response.status_code == 422
    fields = response.json()["error"]["fields"]
    assert any(f["field"] == "linkedin_url" for f in fields)

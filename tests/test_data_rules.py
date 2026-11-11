"""Save-time data rules (api#672 / #671 / #673) on every write path.

* #672 -- any spelling of "unknown" that is the WHOLE value is stored as
  ``Unknown``; "Unknown startup" is left alone.
* #671 -- company Unknown -> industry Unknown, ALWAYS (even over a real one).
* #673 -- company "Not in the Labor Force" (variants) -> canonical company text,
  industry Other, secondary "Not in the Labor Force", and (current job)
  employment status "Not in the Labor Force".

One shared module (``app.core.data_rules``) owns the rules; these tests pin
that each write path -- staff create, staff update, the per-row employment and
education endpoints, role archiving, the survey apply and the CSV import --
actually reaches it. Also pins the hand-run maintenance SQL to the same
patterns. Offline throughout.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from app.core import data_rules
from app.core.dropdowns import (
    EMPLOYMENT_STATUSES,
    PRIMARY_INDUSTRIES,
    SECONDARY_INDUSTRIES,
    validate_industry,
)
from app.models.alumni import Alumni
from app.models.employment import CurrentEmployment, EducationHistory, EmploymentHistory
from app.schemas.alumni import (
    AlumniCreateFull,
    AlumniUpdateFull,
    CareerCreate,
    FormerCreate,
)
from app.schemas.profile import (
    EducationCreate,
    EmploymentHistoryCreate,
    EmploymentHistoryUpdate,
)
from app.services import alumni as service
from app.services import hygiene, import_csv
from app.services import profile as profile_service
from tests.test_alumni_import import FakeSession, _csv_bytes, _row_values
from tests.test_audit_field_capture import (
    _alumnus,
    _patch_get,
    _RowSession,
    _SectionSession,
)
from tests.test_employment_archiving import _import, _import_employment, _import_session
from tests.test_survey_apply_audit import _alum as _survey_alum
from tests.test_survey_apply_audit import _apply, _fake_resp, _fields
from tests.test_survey_apply_audit import _Session as _SurveySession

NLF = "Not in the Labor Force"
MAINTENANCE = Path(__file__).resolve().parents[1] / "database" / "maintenance"


# --- the spellings ------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "unknown",
        "Unknown",
        "UNKNOWN",
        "UNKOWN",
        "unkown",
        "unknwn",
        "Unknow",
        "uknown",
        "unkonwn",
        "  Unknown  ",
        "\tUNKNOWN\n",
        "unknownn",
    ],
)
def test_unknown_spellings_are_unknown(value):
    assert data_rules.is_unknown(value)
    assert data_rules.normalize_unknown(value) == "Unknown"


@pytest.mark.parametrize(
    "value",
    [
        "Unknown startup",
        "unknown company",
        "Not unknown",
        "un",
        "unk",
        "known",
        "",
        "   ",
        "Uber",
        "Unwon",
        None,
        42,
    ],
)
def test_other_values_are_left_alone(value):
    assert not data_rules.is_unknown(value)
    assert data_rules.normalize_unknown(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "Not in the Labor Force",
        "not in the labor force",
        "NOT IN THE LABOR FORCE",
        "Not in Labor Force",
        "not in labour force",
        "  Not  in   the labor   force ",
        "Not in the labor-force",
    ],
)
def test_not_in_labor_force_variants(value):
    assert data_rules.is_not_in_labor_force(value)
    assert data_rules.normalize_not_in_labor_force(value) == NLF


@pytest.mark.parametrize(
    "value", ["Labor Force Inc", "Not in the Labor Force yet", "In the labor force", None]
)
def test_not_in_labor_force_rejects_other_text(value):
    assert not data_rules.is_not_in_labor_force(value)


def test_normalize_value_only_touches_listed_columns():
    assert data_rules.normalize_value("current_employment", "current_title", "UNKOWN") == "Unknown"
    assert data_rules.normalize_value("alumni", "first_name", "UNKNOWN") == "UNKNOWN"
    assert data_rules.normalize_value("alumni", "employment_status", "not in labor force") == NLF
    # NLF canonicalisation is limited to the columns that hold that label.
    assert (
        data_rules.normalize_value("current_employment", "current_title", "not in labor force")
        == "not in labor force"
    )


def test_canonical_labels_match_the_vocabularies():
    assert NLF in EMPLOYMENT_STATUSES
    assert "Unknown" in EMPLOYMENT_STATUSES
    assert "Unknown" in PRIMARY_INDUSTRIES
    assert "Other" in PRIMARY_INDUSTRIES
    assert NLF in SECONDARY_INDUSTRIES
    assert NLF not in PRIMARY_INDUSTRIES


def test_validate_industry_accepts_unknown_typos():
    assert validate_industry("UNKOWN") == "Unknown"
    assert validate_industry(" unknown ") == "Unknown"
    assert validate_industry(NLF.lower()) == NLF


# --- job rules (pure) ----------------------------------------------------------


def test_unknown_company_forces_unknown_industry_over_a_real_one():
    out = data_rules.apply_job_rules(
        {"current_employer": "UNKNOWN", "current_industry": "Investment Banking"},
        data_rules.CURRENT_JOB,
    )
    assert out["current_employer"] == "Unknown"
    assert out["current_industry"] == "Unknown"
    assert "current_industry_secondary" not in out


def test_nlf_company_forces_other_and_secondary():
    out = data_rules.apply_job_rules(
        {"current_employer": "not in labor force", "current_industry": "Consulting"},
        data_rules.CURRENT_JOB,
    )
    assert out == {
        "current_employer": NLF,
        "current_industry": "Other",
        "current_industry_secondary": NLF,
    }


def test_partial_values_use_the_stored_employer_and_leave_it_unwritten():
    out = data_rules.apply_job_rules(
        {"current_industry": "Private Equity"},
        data_rules.CURRENT_JOB,
        stored_employer="unkown",
    )
    assert out == {"current_industry": "Unknown"}


def test_history_rows_have_no_secondary():
    out = data_rules.apply_job_rules({"employer_name": NLF}, data_rules.HISTORY_JOB)
    assert out == {"employer_name": NLF, "employment_industry": "Other"}


def test_ordinary_company_changes_nothing():
    values = {"current_employer": "Acme", "current_industry": "Consulting"}
    assert data_rules.apply_job_rules(values, data_rules.CURRENT_JOB) == values
    assert data_rules.employment_status_for("Acme") is None


# --- staff: hygiene / preview ----------------------------------------------------


def test_hygiene_cleans_unknown_spellings_in_every_section():
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(
            employment_status="UNKOWN",
            home_country=" unknown ",
            contact={"country": "UNKNOWN"},
            career=CareerCreate(current_title="unknwn", current_employer="Unknown startup"),
        ),
        jsonable=False,
        stored_state=None,
    )
    assert cleaned["employment_status"] == "Unknown"
    assert cleaned["home_country"] == "Unknown"
    assert cleaned["contact"]["country"] == "Unknown"
    assert cleaned["career"]["current_title"] == "Unknown"
    assert cleaned["career"]["current_employer"] == "Unknown startup"
    assert {c["field"] for c in changes} >= {"employment_status", "home_country", "country"}


def test_hygiene_is_idempotent_on_canonical_values():
    _cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(employment_status="Unknown", career=CareerCreate(current_employer=NLF)),
        jsonable=False,
        stored_state=None,
    )
    assert changes == []


# --- staff: update_alumni ----------------------------------------------------------


def _stored_job(**overrides) -> CurrentEmployment:
    values = {
        "current_employment_id": 3,
        "alumni_id": 1,
        "current_employer": "Acme Corp",
        "current_title": "Analyst",
        "current_industry": "Investment Banking",
        "current_industry_secondary": None,
    }
    values.update(overrides)
    return CurrentEmployment(**values)


def _update(monkeypatch, payload, *, job=None, alum=None):
    alumnus = alum if alum is not None else _alumnus()
    _patch_get(monkeypatch, alumnus)
    session = (
        _SectionSession(current_employment=job) if job is not None else _SectionSession()
    )
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    return session, alumnus


def _audit_map(session):
    from app.models.audit import AuditLog

    return {
        a.field_name: (a.old_value, a.new_value)
        for a in session.added
        if isinstance(a, AuditLog) and a.field_name
    }


def test_staff_update_unknown_company_overwrites_a_real_industry(monkeypatch):
    job = _stored_job()
    session, _ = _update(
        monkeypatch,
        AlumniUpdateFull(
            career=CareerCreate(current_employer="UNKOWN", current_industry="Investment Banking")
        ),
        job=job,
    )
    assert job.current_employer == "Unknown"
    assert job.current_industry == "Unknown"
    audits = _audit_map(session)
    assert audits["career.current_employer"] == ("Acme Corp", "Unknown")
    assert audits["career.current_industry"] == ("Investment Banking", "Unknown")


def test_staff_update_industry_only_is_judged_against_the_stored_company(monkeypatch):
    job = _stored_job(current_employer="Unknown", current_industry="Unknown")
    _update(
        monkeypatch,
        AlumniUpdateFull(career=CareerCreate(current_industry="Private Equity")),
        job=job,
    )
    assert job.current_industry == "Unknown"


def test_staff_update_nlf_company_sets_industry_secondary_and_status(monkeypatch):
    job = _stored_job()
    alum = _alumnus()
    alum.employment_status = "Full-time"
    session, alum = _update(
        monkeypatch,
        AlumniUpdateFull(career=CareerCreate(current_employer="not in labor force")),
        job=job,
        alum=alum,
    )
    assert job.current_employer == NLF
    assert job.current_industry == "Other"
    assert job.current_industry_secondary == NLF
    assert alum.employment_status == NLF
    audits = _audit_map(session)
    assert audits["employment_status"] == ("Full-time", NLF)
    assert audits["career.current_industry_secondary"] == (None, NLF)


def test_staff_status_change_cannot_contradict_an_nlf_company(monkeypatch):
    job = _stored_job(
        current_employer=NLF, current_industry="Other", current_industry_secondary=NLF
    )
    alum = _alumnus()
    alum.employment_status = NLF
    session, alum = _update(
        monkeypatch, AlumniUpdateFull(employment_status="Full-time"), job=job, alum=alum
    )
    assert alum.employment_status == NLF
    # Nothing actually changed, so nothing is audited.
    assert _audit_map(session) == {}


def test_staff_update_ordinary_company_is_untouched(monkeypatch):
    job = _stored_job()
    session, alum = _update(
        monkeypatch,
        AlumniUpdateFull(employment_status="Part-time", career=CareerCreate(current_title="VP")),
        job=job,
    )
    assert job.current_industry == "Investment Banking"
    assert alum.employment_status == "Part-time"


def test_staff_update_status_spelling_is_fixed(monkeypatch):
    _session, alum = _update(monkeypatch, AlumniUpdateFull(employment_status="UNKOWN"))
    assert alum.employment_status == "Unknown"


# --- staff: create_alumni (and the CSV import, which goes through it) -------------


def _created(session, model):
    return [o for o in session.added if isinstance(o, model)]


def test_staff_create_applies_rules_to_current_and_former_jobs():
    session = FakeSession()
    payload = AlumniCreateFull(
        first_name="Jane",
        last_name="Doe",
        employment_status="Full-time",
        career=CareerCreate(current_employer="Not in Labor Force", current_industry="Consulting"),
        former=FormerCreate(employer_name="unknown", employment_industry="Consulting"),
    )
    asyncio.run(service.create_alumni(session, payload, actor_user_id=9))
    (alum,) = _created(session, Alumni)
    (job,) = _created(session, CurrentEmployment)
    (former,) = _created(session, EmploymentHistory)
    assert alum.employment_status == NLF
    assert (job.current_employer, job.current_industry, job.current_industry_secondary) == (
        NLF,
        "Other",
        NLF,
    )
    assert (former.employer_name, former.employment_industry) == ("Unknown", "Unknown")


def test_csv_import_create_applies_the_rules():
    csv = _csv_bytes(
        _row_values(
            first_name="Jane",
            last_name="Doe",
            current_employer="UNKNOWN",
            current_industry="Investment Banking",
        ),
        _row_values(
            first_name="John",
            last_name="Roe",
            current_employer="not in the labor force",
            current_industry="Consulting",
        ),
    )
    rows, _ = import_csv.parse_and_map(csv)
    session = FakeSession()
    result = asyncio.run(import_csv.commit_import(session, rows))
    assert result["imported"] == 2
    jobs = _created(session, CurrentEmployment)
    by_employer = {j.current_employer: j for j in jobs}
    assert by_employer["Unknown"].current_industry == "Unknown"
    assert by_employer[NLF].current_industry == "Other"
    assert by_employer[NLF].current_industry_secondary == NLF
    statuses = {a.first_name: a.employment_status for a in _created(session, Alumni)}
    assert statuses["John"] == NLF


def test_csv_import_update_applies_the_rules():
    employment = _import_employment(current_industry="Investment Banking")
    session = _import_session(employment)
    result = _import(session, current_employer="Unkown")
    assert result["updated"] == 1
    assert employment.current_employer == "Unknown"
    assert employment.current_industry == "Unknown"


def test_csv_import_blanks_misspelt_placeholders_like_the_real_one():
    csv = _csv_bytes(
        _row_values(first_name="Jane", last_name="Doe", current_city="unkown")
    )
    rows, _ = import_csv.parse_and_map(csv)
    assert "current_city" not in (rows[0]["payload"].get("career") or {})


# --- per-row employment / education endpoints -------------------------------------


def test_add_employment_applies_rules():
    session = _RowSession()
    asyncio.run(
        profile_service.add_employment(
            session,
            1,
            EmploymentHistoryCreate(
                employer_name="UNKNOWN", employment_industry="Investment Banking"
            ),
            actor_user_id=9,
        )
    )
    (row,) = _created(session, EmploymentHistory)
    assert (row.employer_name, row.employment_industry) == ("Unknown", "Unknown")


def test_update_employment_industry_only_uses_stored_company():
    row = EmploymentHistory(
        employment_history_id=7,
        alumni_id=1,
        employer_name="Not in the labor force",
        employment_industry="Other",
        is_current=False,
    )
    session = _RowSession(row=row)
    asyncio.run(
        profile_service.update_employment(
            session,
            1,
            7,
            EmploymentHistoryUpdate(employment_industry="Consulting"),
            actor_user_id=9,
        )
    )
    assert row.employment_industry == "Other"


def test_add_education_fixes_unknown_spelling():
    session = _RowSession()
    asyncio.run(
        profile_service.add_education(
            session, 1, EducationCreate(university="UNKOWN", degree="BS"), actor_user_id=9
        )
    )
    (row,) = _created(session, EducationHistory)
    assert row.university == "Unknown"
    assert row.degree == "BS"


def test_archived_role_gets_the_rules():
    session = _RowSession()
    row = asyncio.run(
        service.archive_current_role(
            session,
            1,
            {"current_employer": "unknwn", "current_industry": "Consulting"},
        )
    )
    assert (row.employer_name, row.employment_industry) == ("Unknown", "Unknown")


# --- survey apply (and the new-people approve, which shares it) --------------------


def _survey_job(**overrides):
    import types

    values = {
        "alumni_id": 5,
        "current_employer": "Old Bank",
        "current_title": "Analyst",
        "current_industry": "Investment Banking",
        "current_industry_secondary": None,
        "current_city": "Provo",
        "current_state": "Utah",
        "current_country": None,
        "current_zip": None,
        "seniority_level": None,
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def test_survey_unknown_company_forces_unknown_industry(monkeypatch):
    job = _survey_job()
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(
            payload={
                "employment.current_employer": "unkown",
                "employment.current_industry": "Investment Banking",
            }
        ),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )
    assert job.current_employer == "Unknown"
    assert job.current_industry == "Unknown"
    fields = _fields(session)
    assert fields["career.current_employer"] == ("Old Bank", "Unknown")
    assert fields["career.current_industry"] == ("Investment Banking", "Unknown")


def test_survey_nlf_company_sets_status_and_secondary(monkeypatch):
    job = _survey_job()
    alum = _survey_alum(employment_status="Full-time")
    session = _SurveySession(alum)
    _apply(
        session,
        _fake_resp(
            payload={
                "employment.current_employer": "Not in Labor Force",
                "profile.employment_status": "Part-time",
            }
        ),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )
    assert job.current_employer == NLF
    assert job.current_industry == "Other"
    assert job.current_industry_secondary == NLF
    assert alum.employment_status == NLF
    fields = _fields(session)
    assert fields["employment_status"] == ("Full-time", NLF)
    assert fields["career.current_industry_secondary"] == (None, NLF)


def test_survey_secondary_industry_accepts_nlf(monkeypatch):
    job = _survey_job(current_employer="Acme")
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(payload={"employment.current_industry_secondary": NLF}),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )
    assert job.current_industry_secondary == NLF
    assert job.current_industry == "Investment Banking"


def test_survey_text_fields_fix_unknown_spelling(monkeypatch):
    job = _survey_job(current_employer="Acme")
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(payload={"employment.current_city": "UNKNOWN"}),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )
    assert job.current_city == "Unknown"


# --- the maintenance SQL --------------------------------------------------------


_SQL_FILES = (
    "672-unify-unknown.sql",
    "671-unknown-company-industry.sql",
    "673-not-in-labor-force.sql",
)


@pytest.mark.parametrize("name", _SQL_FILES)
def test_maintenance_sql_shape(name):
    """Hand-run files: transaction opened, COMMIT left for the human, never in
    migrations/ (which auto-runs on prod)."""
    sql = (MAINTENANCE / name).read_text(encoding="utf-8")
    assert not (MAINTENANCE.parent / "migrations" / name).exists()
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert re.search(r"^BEGIN;", code, re.M)
    assert "UPDATE " in code
    assert not re.search(r"^\s*COMMIT;", code, re.M), "COMMIT must stay commented out"
    assert "-- COMMIT;" in sql
    # Balanced parentheses and quotes: a cheap "it at least parses" guard.
    assert code.count("(") == code.count(")")
    assert code.count("'") % 2 == 0


@pytest.mark.parametrize("name", _SQL_FILES)
def test_maintenance_sql_uses_the_python_patterns(name):
    sql = (MAINTENANCE / name).read_text(encoding="utf-8")
    if name != "673-not-in-labor-force.sql":
        assert f"'{data_rules.UNKNOWN_SQL_REGEX}'" in sql
    if name == "673-not-in-labor-force.sql":
        assert f"'{data_rules.NLF_SQL_REGEX}'" in sql


def test_unknown_cleanup_covers_exactly_the_rule_columns():
    sql = (MAINTENANCE / "672-unify-unknown.sql").read_text(encoding="utf-8")
    pairs = set(re.findall(r"UPDATE (\w+)\s+SET (\w+) = 'Unknown'", sql))
    expected = {
        (table, column)
        for table, columns in data_rules.UNKNOWN_COLUMNS.items()
        for column in columns
    }
    assert pairs == expected


@pytest.mark.parametrize(
    "value",
    ["unknown", "UNKOWN", " Unknwn ", "Unknown startup", "uber", "unk", "unknownn", "Uknow"],
)
def test_sql_unknown_regex_agrees_with_python(value):
    """The SQL predicate is: folded value ~ regex AND has a k AND has an n."""
    folded = re.sub(r"\s+", " ", value.strip()).lower()
    sql_says = (
        bool(re.match(data_rules.UNKNOWN_SQL_REGEX, folded))
        and "k" in folded
        and "n" in folded
    )
    assert sql_says == data_rules.is_unknown(value)

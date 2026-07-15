"""Offline tests for alumni service business rules (no database).

A fake session captures add/commit/refresh, and the repository's ``get`` is
monkeypatched, so these exercise the rules (soft-delete, manual-edit stamping)
without touching Postgres.
"""

import asyncio

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.schemas.alumni import (
    AlumniCreate,
    AlumniCreateFull,
    AlumniUpdate,
    AlumniUpdateFull,
    CareerCreate,
    EngagementCreate,
)
from app.schemas.profile import CurrentCareerRead
from app.services import alumni as service


class _EmptyScalars:
    def all(self):
        return []


class _EmptyResult:
    def scalars(self):
        return _EmptyScalars()


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def scalar(self, stmt: object) -> None:
        # The hygiene duplicate check runs on create/update; no fixtures here
        # have duplicates, so every exact/spouse lookup returns nothing.
        return None

    async def execute(self, stmt: object) -> _EmptyResult:
        # Fuzzy duplicate scan -> no matches.
        return _EmptyResult()

    async def flush(self) -> None:
        # create_alumni flushes to obtain the generated alumni_id before
        # attaching related-section rows; no-op for the fake.
        pass

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: object) -> None:
        pass


def _patch_get(monkeypatch, value):
    async def fake_get(session, alumni_id):
        return value

    monkeypatch.setattr(service.repo, "get", fake_get)


def test_create_alumni_sets_fields():
    session = FakeSession()
    payload = AlumniCreate(first_name="Jane", last_name="Doe", graduation_year=2018)
    obj = asyncio.run(service.create_alumni(session, payload))
    assert obj in session.added
    assert (obj.first_name, obj.last_name, obj.graduation_year) == ("Jane", "Doe", 2018)
    assert session.committed == 1


def test_create_alumni_persists_secondary_affiliation():
    # #47: the secondary affiliation / education core columns flow through the
    # create path onto the Alumni row with no special handling.
    session = FakeSession()
    payload = AlumniCreate(
        first_name="Jane",
        last_name="Doe",
        mba_program="BYU Marriott MBA",
        law_school="Harvard Law",
        medical_school="Johns Hopkins",
        graduate_school="MIT",
        startup_involvement="Co-founded Acme",
        advisory_roles="Board advisor at Foo Inc.",
        secondary_employment="Adjunct professor",
    )
    obj = asyncio.run(service.create_alumni(session, payload))
    assert obj.mba_program == "BYU Marriott MBA"
    assert obj.law_school == "Harvard Law"
    assert obj.medical_school == "Johns Hopkins"
    assert obj.graduate_school == "MIT"
    assert obj.startup_involvement == "Co-founded Acme"
    assert obj.advisory_roles == "Board advisor at Foo Inc."
    assert obj.secondary_employment == "Adjunct professor"


def test_update_alumni_persists_secondary_affiliation(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe", archived=False)
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(
        service.update_alumni(
            session,
            1,
            AlumniUpdate(mba_program="Wharton MBA", advisory_roles="Advisor, X"),
        )
    )
    assert obj.mba_program == "Wharton MBA"
    assert obj.advisory_roles == "Advisor, X"
    assert obj.manually_edited_at is not None
    assert session.committed == 1


# --- graduate_graduation_year (distinct from undergrad graduation_year) ------


def test_create_alumni_persists_graduate_graduation_year():
    # The new core column flows through create onto the Alumni row unchanged,
    # independent of the undergrad graduation_year.
    session = FakeSession()
    payload = AlumniCreate(
        first_name="Jane",
        last_name="Doe",
        graduation_year=2018,
        graduate_graduation_year=2021,
    )
    obj = asyncio.run(service.create_alumni(session, payload))
    assert obj.graduation_year == 2018
    assert obj.graduate_graduation_year == 2021


def test_update_alumni_persists_graduate_graduation_year(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe", archived=False)
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(
        service.update_alumni(session, 1, AlumniUpdate(graduate_graduation_year=2022))
    )
    assert obj.graduate_graduation_year == 2022
    assert obj.manually_edited_at is not None
    assert session.committed == 1


def test_graduate_graduation_year_out_of_range_is_422():
    # Same year-range validator as graduation_year: below _YEAR_MIN and above
    # _YEAR_MAX both raise a pydantic ValidationError (surfaced as a 422).
    with pytest.raises(ValidationError):
        AlumniCreate(first_name="Jane", graduate_graduation_year=1800)
    with pytest.raises(ValidationError):
        AlumniCreate(first_name="Jane", graduate_graduation_year=99999)


# --- company_address is now writable via the career section ------------------


def test_create_alumni_persists_company_address():
    # #366: company_address is exposed for READ but was not writable. Now that
    # CareerCreate carries it, the create path persists it onto the current-
    # employment row, and CurrentCareerRead reads it straight back.
    session = FakeSession()
    payload = AlumniCreateFull(
        first_name="Jane",
        last_name="Doe",
        career=CareerCreate(
            current_employer="Acme Capital",
            company_address="123 Market St, San Francisco, CA",
        ),
    )
    asyncio.run(service.create_alumni(session, payload))
    employment = next(o for o in session.added if isinstance(o, CurrentEmployment))
    assert employment.company_address == "123 Market St, San Francisco, CA"
    # Round-trips back out through the read schema (the DB would assign the PK on
    # insert; the fake session doesn't, so stand one in for validation).
    employment.current_employment_id = 1
    read = CurrentCareerRead.model_validate(employment)
    assert read.company_address == "123 Market St, San Francisco, CA"


def test_update_alumni_persists_company_address(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe", archived=False)
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdateFull(
        career=CareerCreate(company_address="500 Boylston St, Boston, MA")
    )
    asyncio.run(service.update_alumni(session, 1, payload))
    # No existing employment row (fake scalar returns None), so the upsert inserts
    # one carrying the address.
    employment = next(o for o in session.added if isinstance(o, CurrentEmployment))
    assert employment.company_address == "500 Boylston St, Boston, MA"
    assert existing.manually_edited_at is not None


# --- engagement boolean toggle-off persists (all-explicit section) -----------


class _EngagementSession(FakeSession):
    """Fake session that returns a pre-seeded program-engagement row for the
    section upsert query, so an update can flip an existing True flag to False."""

    def __init__(self, engagement: AlumniProgramEngagement) -> None:
        super().__init__()
        self._engagement = engagement

    async def scalar(self, stmt: object) -> object | None:
        # Only the engagement section's SELECT targets alumni_program_engagement;
        # the byu/net-id duplicate lookups target the alumni table (-> None).
        if "alumni_program_engagement" in str(
            stmt.compile(dialect=postgresql.dialect())
        ):
            return self._engagement
        return None


def test_update_engagement_toggles_true_flag_to_false(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe", archived=False)
    _patch_get(monkeypatch, existing)
    # Seed an engagement profile with two flags already True.
    engagement_row = AlumniProgramEngagement(
        engagement_profile_id=7,
        alumni_id=1,
        mentor_willing=True,
        piff_donor=True,
    )
    session = _EngagementSession(engagement_row)
    # The frontend sends the FULL explicit set: mentor_willing flipped to False,
    # piff_donor left True. has_values() is True (piff_donor), so the whole
    # section is applied and the explicit False persists.
    payload = AlumniUpdateFull(
        engagement=EngagementCreate(mentor_willing=False, piff_donor=True)
    )
    asyncio.run(service.update_alumni(session, 1, payload))
    assert engagement_row.mentor_willing is False
    assert engagement_row.piff_donor is True
    assert session.committed == 1


def test_get_alumni_missing_raises(monkeypatch):
    _patch_get(monkeypatch, None)
    with pytest.raises(NotFoundError):
        asyncio.run(service.get_alumni(FakeSession(), 999))


def test_update_alumni_stamps_manual_edit(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe", archived=False)
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(service.update_alumni(session, 1, AlumniUpdate(last_name="Smith")))
    assert obj.last_name == "Smith"
    assert obj.manually_edited_at is not None
    assert session.committed == 1


def test_update_alumni_no_changes_is_noop(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(service.update_alumni(session, 1, AlumniUpdate()))
    assert session.committed == 0
    assert obj.manually_edited_at is None


def test_archive_alumni_soft_deletes(monkeypatch):
    existing = Alumni(alumni_id=1, last_name="Doe", archived=False)
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(service.archive_alumni(session, 1))
    assert obj.archived is True
    assert obj.manually_edited_at is not None
    assert session.committed == 1


def test_archive_alumni_idempotent(monkeypatch):
    existing = Alumni(alumni_id=1, last_name="Doe", archived=True)
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(service.archive_alumni(session, 1))
    assert obj.archived is True
    assert session.committed == 0


def test_restore_alumni(monkeypatch):
    existing = Alumni(alumni_id=1, last_name="Doe", archived=True)
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(service.restore_alumni(session, 1))
    assert obj.archived is False
    assert session.committed == 1


# --- filter_options: population-scoped + secondary industry (#184) -----------
#
# No live DB in CI (see tests/conftest.py), so a fake session returns canned,
# per-column rows and captures the compiled SQL of every distinct query. That
# lets these assert (a) each option query is scoped to the visible population
# and (b) the industries list unions primary + secondary values, all offline.


class _RowsResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FilterOptionsSession:
    """Fake async session for ``filter_options``.

    ``by_fragment`` is an ordered list of ``(sql_fragment, values)``; the FIRST
    fragment found in a query's compiled SQL supplies its rows. Order matters —
    put the more specific fragment first (e.g. ``current_industry_secondary``
    before ``current_industry``).
    """

    def __init__(self, by_fragment: list[tuple[str, list]] | None = None) -> None:
        self.by_fragment = by_fragment or []
        self.seen: list[str] = []

    async def execute(self, stmt: object) -> _RowsResult:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        self.seen.append(sql)
        for fragment, values in self.by_fragment:
            if fragment in sql:
                return _RowsResult([(v,) for v in values])
        return _RowsResult([])


def test_filter_options_scopes_every_query_to_visible_population():
    # #184: options must not offer archived / friend-only values, so every
    # distinct query is gated to non-archived graduates (archived=false AND
    # is_alumni=true) — directly on the alumni row or via a correlated EXISTS.
    session = _FilterOptionsSession()
    asyncio.run(service.filter_options(session))
    assert session.seen  # sanity: queries actually ran
    for sql in session.seen:
        assert "archived IS false" in sql
        assert "is_alumni IS true" in sql


def test_filter_options_industries_union_primary_and_secondary():
    # #184: the list filter matches primary OR secondary industry, so the option
    # list unions both columns, deduped + sorted.
    session = _FilterOptionsSession(
        [
            ("current_industry_secondary", ["Private Equity", "Investment Banking"]),
            ("current_industry", ["Investment Banking", "Asset Management"]),
        ]
    )
    opts = asyncio.run(service.filter_options(session))
    # Deduped across the two columns and sorted; the secondary-only value is
    # present even though it never appears in current_industry.
    assert opts["industries"] == [
        "Asset Management",
        "Investment Banking",
        "Private Equity",
    ]
    # Both the primary and the secondary industry column are actually queried.
    assert any("current_industry_secondary" in sql for sql in session.seen)

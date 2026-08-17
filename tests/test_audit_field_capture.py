"""Field-level audit capture for nested sections and per-row rows (#45).

`audit_logs` has had field_name / old_value / new_value since the schema was
written, but only the alumni CORE row ever populated them. Every nested section
(contact, career, education, engagement) recorded a single bare "something
changed" row, and the per-row employment/education endpoints recorded not even
that much detail — so a corrected employer, a fixed email or a deleted degree
left nothing to compare against. History cannot be reconstructed after the fact:
whatever these paths fail to capture is gone permanently, which is what makes
these tests worth having rather than nice to have.

Three behaviours are pinned here:

  * **Section fields produce real old -> new rows**, namespaced
    ``contact.personal_email`` / ``career.current_employer`` so the contact row's
    ``region`` is never confused with the alumni row's own.
  * **A core field AND a section field in the SAME save both get recorded.** This
    was a live bug: the old ``if section_written and not applied`` guard emitted
    the section's bare row ONLY when no core field had changed, so the most
    ordinary edit of all — change a name and an email together — recorded the
    section change nowhere at all.
  * **One save = one change set**, and every row carries its provenance
    (``manual`` vs ``import``), because timestamps cannot group a bulk import
    (one transaction, one ``now()``, thousands of records) and a later restore
    must not revert a value an import legitimately corrected.

Offline throughout — fake sessions in the style of
``tests/test_alumni_section_writes.py``; no Postgres.
"""

import asyncio

import pytest
from sqlalchemy.dialects import postgresql

from app.core.audit_context import (
    AUDIT_SOURCE_IMPORT,
    AUDIT_SOURCE_MANUAL,
    audit_source,
    audit_source_scope,
    reset_audit_source,
)
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import (
    CurrentEmployment,
    EducationHistory,
    EmploymentHistory,
)
from app.schemas.alumni import (
    AlumniUpdate,
    AlumniUpdateFull,
    CareerCreate,
    ContactCreate,
)
from app.schemas.profile import (
    EducationCreate,
    EducationUpdate,
    EmploymentHistoryCreate,
    EmploymentHistoryUpdate,
)
from app.services import alumni as service
from app.services import import_csv
from app.services import profile as profile_service
from tests.test_alumni_import import _csv_bytes, _row_values
from tests.test_alumni_update_import import FakeUpdateSession


@pytest.fixture(autouse=True)
def _clean_audit_source():
    """The write-source contextvar is module-global; a leaked ``import`` scope
    would silently mislabel every later test's rows."""
    reset_audit_source()
    yield
    reset_audit_source()


# --- Fakes -------------------------------------------------------------------


class _EmptyScalars:
    def all(self):
        return []


class _EmptyResult:
    def scalars(self):
        return _EmptyScalars()


class _FakeSession:
    """Every lookup returns nothing, so no fixture ever looks duplicated."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def scalar(self, stmt: object) -> object | None:
        return None

    async def execute(self, stmt: object) -> _EmptyResult:
        return _EmptyResult()

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: object) -> None:
        pass


class _SectionSession(_FakeSession):
    """Returns pre-seeded section rows for the section upsert queries, keyed by
    table name against the compiled SQL (the duplicate lookups target ``alumni``
    and still fall through to None)."""

    def __init__(self, **rows: object) -> None:
        super().__init__()
        self._rows = rows

    async def scalar(self, stmt: object) -> object | None:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        for table, row in self._rows.items():
            if table in sql:
                return row
        return None


class _RowSession:
    """Session for the per-row employment / education handlers.

    ``get`` returns the seeded row regardless of model (each handler asks for at
    most the alumnus and its own row), ``flush`` assigns the surrogate id
    Postgres would generate, and mutations are recorded for assertions.
    """

    def __init__(self, row: object | None = None, alumnus: object | None = None) -> None:
        self._row = row
        self._alumnus = alumnus if alumnus is not None else Alumni(alumni_id=1)
        self.added: list[object] = []
        self.deleted: list[object] = []
        self.committed = 0
        self.flushes = 0

    async def get(self, model, ident):
        if model is Alumni:
            return self._alumnus
        return self._row

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def delete(self, obj: object) -> None:
        self.deleted.append(obj)

    async def flush(self) -> None:
        self.flushes += 1
        for obj in self.added:
            if isinstance(obj, EmploymentHistory) and obj.employment_history_id is None:
                obj.employment_history_id = 77
            if isinstance(obj, EducationHistory) and obj.education_id is None:
                obj.education_id = 88

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: object) -> None:
        pass


def _patch_get(monkeypatch, value):
    async def fake_get(session, alumni_id):
        return value

    monkeypatch.setattr(service.repo, "get", fake_get)


def _alumnus() -> Alumni:
    return Alumni(alumni_id=1, first_name="Jane", last_name="Doe", archived=False)


def _audits(session) -> list[AuditLog]:
    return [obj for obj in session.added if isinstance(obj, AuditLog)]


def _by_field(session) -> dict[str, tuple[str | None, str | None]]:
    return {a.field_name: (a.old_value, a.new_value) for a in _audits(session)}


# --- Section fields now carry old -> new -------------------------------------


def test_contact_section_change_records_the_old_value(monkeypatch):
    """THE gap. Correcting a stored email must record what it used to be — the
    one moment that value still exists anywhere."""
    _patch_get(monkeypatch, _alumnus())
    contact_row = AlumniContactInfo(
        contact_info_id=5, alumni_id=1, personal_email="old@example.com"
    )
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(
        contact=ContactCreate(personal_email="new@example.com")
    )

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert _by_field(session) == {
        "contact.personal_email": ("old@example.com", "new@example.com")
    }


def test_career_section_change_records_the_old_value(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_title="Analyst"
    )
    session = _SectionSession(current_employment=employment_row)
    payload = AlumniUpdateFull(career=CareerCreate(current_title="Associate"))

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert _by_field(session) == {"career.current_title": ("Analyst", "Associate")}


def test_section_fields_are_namespaced_away_from_core_fields(monkeypatch):
    """``region`` exists on BOTH the alumni row and the contact row. Without the
    section prefix the two would collide into one indistinguishable history."""
    _patch_get(monkeypatch, _alumnus())
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(contact=ContactCreate(region="Northeast"))

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert set(_by_field(session)) == {"contact.region"}


def test_unchanged_section_field_writes_no_audit_row(monkeypatch):
    """A form re-submitting every field as loaded must not manufacture history."""
    _patch_get(monkeypatch, _alumnus())
    employment_row = CurrentEmployment(
        current_employment_id=3,
        alumni_id=1,
        current_employer="Acme Corp",
        current_title="Analyst",
    )
    session = _SectionSession(current_employment=employment_row)
    payload = AlumniUpdateFull(
        career=CareerCreate(current_employer="Acme Corp", current_title="Analyst")
    )

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert _audits(session) == []
    assert session.committed == 0


def test_new_section_row_audits_each_populated_field_from_none(monkeypatch):
    """First-ever contact row: every field that carries something is a change
    FROM nothing, and blanks stay out of the trail."""
    _patch_get(monkeypatch, _alumnus())
    session = _FakeSession()  # no contact row exists yet
    payload = AlumniUpdateFull(
        contact=ContactCreate(
            personal_email="new@example.com", city="Provo", country=None
        )
    )

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert _by_field(session) == {
        "contact.personal_email": (None, "new@example.com"),
        "contact.city": (None, "Provo"),
    }


# --- The bug: a core field AND a section field in one save -------------------


def test_core_and_section_in_one_save_are_both_recorded(monkeypatch):
    """THE regression. Under the old ``if section_written and not applied``
    guard, changing a name and an email together recorded the name and
    dropped the email entirely — no row, no field, no old value."""
    _patch_get(monkeypatch, _alumnus())
    contact_row = AlumniContactInfo(
        contact_info_id=5, alumni_id=1, personal_email="old@example.com"
    )
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(
        first_name="Janet",
        contact=ContactCreate(personal_email="new@example.com"),
    )

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    captured = _by_field(session)
    assert captured["first_name"] == ("Jane", "Janet")
    assert captured["contact.personal_email"] == (
        "old@example.com",
        "new@example.com",
    )


def test_section_only_save_no_longer_writes_a_bare_row(monkeypatch):
    """The bare no-field row was a placeholder for the values we couldn't
    capture. Now that we capture them, it must not linger as a duplicate."""
    _patch_get(monkeypatch, _alumnus())
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, city="Provo")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(contact=ContactCreate(city="Orem"))

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert [a.field_name for a in _audits(session)] == ["contact.city"]


def test_two_sections_in_one_save_are_both_recorded(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, city="Provo")
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_title="Analyst"
    )
    session = _SectionSession(
        alumni_contact_info=contact_row, current_employment=employment_row
    )
    payload = AlumniUpdateFull(
        contact=ContactCreate(city="Orem"),
        career=CareerCreate(current_title="Associate"),
    )

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert set(_by_field(session)) == {"contact.city", "career.current_title"}


# --- Change set: one save = one version --------------------------------------


def test_one_save_shares_a_single_change_set_id(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, city="Provo")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(
        first_name="Janet", last_name="Roe", contact=ContactCreate(city="Orem")
    )

    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    rows = _audits(session)
    assert len(rows) == 3
    ids = {a.change_set_id for a in rows}
    assert len(ids) == 1
    assert ids != {None}


def test_separate_saves_get_separate_change_sets(monkeypatch):
    """Grouping is only useful if two saves are distinguishable — the case
    ``created_at`` cannot handle inside one import transaction."""
    alumnus = _alumnus()
    _patch_get(monkeypatch, alumnus)
    session = _FakeSession()

    asyncio.run(
        service.update_alumni(session, 1, AlumniUpdate(first_name="Janet"), actor_user_id=9)
    )
    asyncio.run(
        service.update_alumni(session, 1, AlumniUpdate(first_name="Jan"), actor_user_id=9)
    )

    ids = [a.change_set_id for a in _audits(session)]
    assert len(ids) == 2
    assert ids[0] != ids[1]


def test_a_no_op_save_mints_no_change_set(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    session = _FakeSession()

    asyncio.run(service.update_alumni(session, 1, AlumniUpdate(), actor_user_id=9))

    assert _audits(session) == []
    assert session.committed == 0


# --- Provenance ---------------------------------------------------------------


def test_a_hand_edit_is_recorded_as_manual(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    session = _FakeSession()

    asyncio.run(
        service.update_alumni(session, 1, AlumniUpdate(first_name="Janet"), actor_user_id=9)
    )

    assert [a.source for a in _audits(session)] == [AUDIT_SOURCE_MANUAL]


def test_an_import_scoped_write_is_recorded_as_import(monkeypatch):
    """What a later restore reads to avoid reverting a spreadsheet correction."""
    _patch_get(monkeypatch, _alumnus())
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, city="Provo")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(first_name="Janet", contact=ContactCreate(city="Orem"))

    with audit_source_scope(AUDIT_SOURCE_IMPORT):
        asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))

    assert {a.source for a in _audits(session)} == {AUDIT_SOURCE_IMPORT}


def test_the_import_scope_does_not_leak_past_its_block(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    session = _FakeSession()

    with audit_source_scope(AUDIT_SOURCE_IMPORT):
        pass
    asyncio.run(
        service.update_alumni(session, 1, AlumniUpdate(first_name="Janet"), actor_user_id=9)
    )

    assert [a.source for a in _audits(session)] == [AUDIT_SOURCE_MANUAL]


# --- Per-row employment handlers ---------------------------------------------


def _employment_row() -> EmploymentHistory:
    return EmploymentHistory(
        employment_history_id=12,
        alumni_id=1,
        employer_name="Acme Corp",
        employment_title="Analyst",
        city="Provo",
        state="Utah",
        start_year=2015,
        end_year=2018,
        is_current=False,
    )


def test_add_employment_snapshots_the_new_row():
    session = _RowSession()
    payload = EmploymentHistoryCreate(
        employer_name="Acme Corp", employment_title="Analyst"
    )

    asyncio.run(profile_service.add_employment(session, 1, payload, actor_user_id=9))

    audit = _audits(session)[0]
    # The row id comes from the pre-audit flush, so the trail names WHICH role.
    assert audit.field_name == "employment[77]"
    assert "employer_name='Acme Corp'" in (audit.new_value or "")
    assert audit.old_value is None


def test_update_employment_records_each_changed_field():
    row = _employment_row()
    session = _RowSession(row=row)
    payload = EmploymentHistoryUpdate(
        employer_name="Acme Corp",  # unchanged -> no row
        employment_title="Associate",
        city="Orem",
    )

    asyncio.run(
        profile_service.update_employment(session, 1, 12, payload, actor_user_id=9)
    )

    captured = _by_field(session)
    assert captured == {
        "employment[12].employment_title": ("Analyst", "Associate"),
        "employment[12].city": ("Provo", "Orem"),
    }
    # One save, one version.
    assert len({a.change_set_id for a in _audits(session)}) == 1


def test_update_employment_that_changes_nothing_writes_no_audit_row():
    row = _employment_row()
    session = _RowSession(row=row)
    payload = EmploymentHistoryUpdate(employer_name="Acme Corp")

    asyncio.run(
        profile_service.update_employment(session, 1, 12, payload, actor_user_id=9)
    )

    assert _audits(session) == []


def test_delete_employment_snapshots_what_was_removed():
    """A hard delete loses the role irrecoverably unless the trail keeps it."""
    row = _employment_row()
    session = _RowSession(row=row)

    asyncio.run(profile_service.delete_employment(session, 1, 12, actor_user_id=9))

    audit = _audits(session)[0]
    assert audit.field_name == "employment[12]"
    assert "employer_name='Acme Corp'" in (audit.old_value or "")
    assert "employment_title='Analyst'" in (audit.old_value or "")
    assert audit.new_value is None
    assert session.deleted == [row]


# --- Per-row education handlers ----------------------------------------------


def _education_row() -> EducationHistory:
    return EducationHistory(
        education_id=34,
        alumni_id=1,
        university="Brigham Young University",
        degree="BS",
        major="Finance",
        degree_year=2018,
    )


def test_add_education_snapshots_the_new_row():
    session = _RowSession()
    payload = EducationCreate(university="Brigham Young University", degree="BS")

    asyncio.run(profile_service.add_education(session, 1, payload, actor_user_id=9))

    audit = _audits(session)[0]
    assert audit.field_name == "education[88]"
    assert "degree='BS'" in (audit.new_value or "")


def test_update_education_records_each_changed_field():
    row = _education_row()
    session = _RowSession(row=row)
    payload = EducationUpdate(degree="MBA", major="Finance")  # major unchanged

    asyncio.run(
        profile_service.update_education(session, 1, 34, payload, actor_user_id=9)
    )

    assert _by_field(session) == {"education[34].degree": ("BS", "MBA")}


def test_delete_education_snapshots_what_was_removed():
    row = _education_row()
    session = _RowSession(row=row)

    asyncio.run(profile_service.delete_education(session, 1, 34, actor_user_id=9))

    audit = _audits(session)[0]
    assert audit.field_name == "education[34]"
    assert "degree='BS'" in (audit.old_value or "")
    assert "university='Brigham Young University'" in (audit.old_value or "")


def test_per_row_handlers_record_provenance():
    row = _employment_row()
    session = _RowSession(row=row)

    asyncio.run(profile_service.delete_employment(session, 1, 12, actor_user_id=9))

    assert _audits(session)[0].source == AUDIT_SOURCE_MANUAL


# --- The bulk CSV import still behaves identically ---------------------------
#
# `update_alumni` is the highest-blast-radius function in the repo and the bulk
# update path runs straight through it, so the capture work must change what is
# AUDITED and nothing about what is WRITTEN. These reuse the real bulk-update
# harness rather than a bespoke fake, so a divergence shows up here.


def test_bulk_import_writes_the_same_record_changes_as_before():
    """The apply outcome — counts, ids, the values landed on the record — is
    untouched; only the audit rows alongside it are richer."""
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    existing = Alumni(
        alumni_id=1,
        byu_id="123456789",
        first_name="Jane",
        last_name="Doe",
        archived=False,
    )
    employment = CurrentEmployment(
        current_employment_id=5, alumni_id=1, current_employer="Old Co"
    )
    session = FakeUpdateSession(
        index_rows=index,
        alumni={1: existing},
        sections={"current_employment": employment},
    )
    csv = _csv_bytes(
        _row_values(byu_id="123456789", last_name="Smith", current_employer="New Co")
    )
    rows, errors = import_csv.parse_and_map(csv)
    assert errors == []

    result = asyncio.run(import_csv.commit_update(session, rows, actor_user_id=9))

    assert result["updated"] == 1
    assert result["unchanged"] == 0
    assert result["errors"] == 0
    assert result["updated_ids"] == [1]
    assert session.committed == 1
    assert existing.last_name == "Smith"
    assert existing.first_name == "Jane"  # blank cell never clears
    assert employment.current_employer == "New Co"
    assert existing.manually_edited_at is not None


def test_bulk_import_captures_core_and_section_values_as_import():
    """The same run, seen from the audit side: a core cell AND a section cell in
    ONE imported row now both leave an old value — and are labelled ``import``
    so a later restore can tell them from a hand edit."""
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    existing = Alumni(
        alumni_id=1,
        byu_id="123456789",
        first_name="Jane",
        last_name="Doe",
        archived=False,
    )
    employment = CurrentEmployment(
        current_employment_id=5, alumni_id=1, current_employer="Old Co"
    )
    session = FakeUpdateSession(
        index_rows=index,
        alumni={1: existing},
        sections={"current_employment": employment},
    )
    csv = _csv_bytes(
        _row_values(byu_id="123456789", last_name="Smith", current_employer="New Co")
    )
    rows, _ = import_csv.parse_and_map(csv)

    asyncio.run(import_csv.commit_update(session, rows, actor_user_id=9))

    captured = _by_field(session)
    assert captured["last_name"] == ("Doe", "Smith")
    assert captured["career.current_employer"] == ("Old Co", "New Co")
    assert {a.source for a in _audits(session)} == {AUDIT_SOURCE_IMPORT}
    # One imported RECORD is one version, even though the whole file commits in a
    # single transaction sharing one created_at.
    assert len({a.change_set_id for a in _audits(session)}) == 1


def test_bulk_import_gives_each_record_its_own_change_set():
    """The reason change_set_id exists: created_at cannot separate these two."""
    index = [
        (1, "111111111", None, "Jane", "Doe", 2018, False),
        (2, "222222222", None, "John", "Roe", 2019, False),
    ]
    alumni = {
        1: Alumni(alumni_id=1, byu_id="111111111", last_name="Doe", archived=False),
        2: Alumni(alumni_id=2, byu_id="222222222", last_name="Roe", archived=False),
    }
    session = FakeUpdateSession(index_rows=index, alumni=alumni)
    csv = _csv_bytes(
        _row_values(byu_id="111111111", last_name="Smith"),
        _row_values(byu_id="222222222", last_name="Jones"),
    )
    rows, _ = import_csv.parse_and_map(csv)

    result = asyncio.run(import_csv.commit_update(session, rows, actor_user_id=9))

    assert result["updated"] == 2
    assert len({a.change_set_id for a in _audits(session)}) == 2


def test_the_import_source_does_not_leak_out_of_commit_update():
    """A bulk run must not leave the process labelling later hand edits as
    imports — the failure mode a module-global would produce."""
    index = [(1, "123456789", None, "Jane", "Doe", 2018, False)]
    alumni = {1: Alumni(alumni_id=1, byu_id="123456789", last_name="Doe", archived=False)}
    session = FakeUpdateSession(index_rows=index, alumni=alumni)
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Smith"))
    rows, _ = import_csv.parse_and_map(csv)
    asyncio.run(import_csv.commit_update(session, rows, actor_user_id=9))

    assert audit_source() == AUDIT_SOURCE_MANUAL

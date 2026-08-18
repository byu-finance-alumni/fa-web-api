"""Demoting the outgoing current role into employment history (#446).

Until this shipped, NOTHING archived employment anywhere: `_upsert_section`
overwrote `current_employment` in place and the previous employer, title and
industry were simply gone. The request is that they move DOWN into
`employment_history` instead.

The hard part was never the write, it was the TRIGGER. Archiving on any change
to the career section would mean that fixing a misspelled employer manufactures
a bogus prior role, and a data-cleanup pass would mint hundreds of them --
strictly worse than losing the history. Inferring "the employer string changed,
therefore a job change" is wrong in both directions: it misses a promotion at the
same company, and it still invents history on a spelling fix.

So the trigger is an EXPLICIT `archive_previous_role` flag on the update payload,
off by default. These tests pin the things that decision is worth nothing
without:

  * it archives when the flag is set,
  * it archives NOTHING when the flag is absent -- the default path, every
    ordinary edit, must behave exactly as it did before,
  * the archived row is the OUTGOING role, and the write nobody typed is visible
    in the audit trail.

Offline throughout -- fake sessions, no Postgres.
"""

import asyncio
import datetime
import types

import pytest
from pydantic import ValidationError

from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.employment import CurrentEmployment, EmploymentHistory
from app.schemas.alumni import AlumniCreateFull, AlumniUpdateFull, CareerCreate
from app.services import alumni as service
from app.services import hygiene, import_csv, survey_responses
from tests.test_alumni_import import _csv_bytes, _row_values
from tests.test_alumni_update_import import FakeUpdateSession
from tests.test_audit_field_capture import _alumnus, _patch_get, _SectionSession
from tests.test_survey_apply_audit import _alum as _survey_alum
from tests.test_survey_apply_audit import _apply, _fake_resp
from tests.test_survey_apply_audit import _Session as _SurveySession


class _ArchiveSession(_SectionSession):
    """`_SectionSession` whose flush assigns the surrogate id Postgres would.

    The archive path flushes to learn the new `employment_history_id` before it
    writes the audit row (so the trail names the row it created), and the base
    fake's flush is a no-op -- without this the audit would read
    `employment[None]` and the test would pass while pinning nothing.
    """

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, EmploymentHistory) and obj.employment_history_id is None:
                obj.employment_history_id = 77


def _stored_role(**overrides: object) -> CurrentEmployment:
    values: dict[str, object] = {
        "current_employment_id": 3,
        "alumni_id": 1,
        "current_employer": "Acme Corp",
        "current_title": "Analyst",
        "current_industry": "Investment Banking",
        "current_city": "Provo",
        "current_state": "Utah",
        "seniority_level": "Individual Contributor",
        "current_country": "United States",
        "current_zip": "84604",
    }
    values.update(overrides)
    return CurrentEmployment(**values)


def _history(session) -> list[EmploymentHistory]:
    return [obj for obj in session.added if isinstance(obj, EmploymentHistory)]


def _audits(session) -> list[AuditLog]:
    return [obj for obj in session.added if isinstance(obj, AuditLog)]


def _archive_audits(session) -> list[AuditLog]:
    return [a for a in _audits(session) if a.action_type == "archive_current_role"]


def _run(session, payload):
    return asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))


# --- The flag is what decides -------------------------------------------------


def test_flagged_role_change_writes_the_old_role_into_history(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(
            current_employer="Beta Capital",
            current_title="Associate",
            current_city="New York",
            current_state="New York",
        ),
    )

    _run(session, payload)

    (archived,) = _history(session)
    assert archived.alumni_id == 1
    assert archived.employer_name == "Acme Corp"
    assert archived.employment_title == "Analyst"
    assert archived.employment_industry == "Investment Banking"
    assert archived.city == "Provo"
    assert archived.state == "Utah"
    assert archived.is_current is False


def test_unflagged_role_change_archives_nothing(monkeypatch):
    """The default path. Every ordinary edit lands here, so this is the test that
    protects the decision: an employer that changed is NOT on its own a job
    change, and nothing may be inferred from it."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(career=CareerCreate(current_employer="Acme Corportation"))

    _run(session, payload)

    assert _history(session) == []
    assert _archive_audits(session) == []
    # ...and the correction itself still audits exactly as it always did.
    assert [(a.field_name, a.old_value, a.new_value) for a in _audits(session)] == [
        ("career.current_employer", "Acme Corp", "Acme Corportation")
    ]


def test_flag_defaults_to_off():
    """Off by default is the whole decision, not a detail of it: defaulting on
    would archive on every routine save, which is the outcome the explicit
    checkbox exists to avoid."""
    assert AlumniUpdateFull().archive_previous_role is False


def test_flag_with_no_career_section_archives_nothing(monkeypatch):
    """A flag ticked on a save that never touches employment has nothing to act
    on -- and, because it is a write control rather than a column, it must not be
    mistaken for an alumni field and `setattr` onto the record."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(archive_previous_role=True, first_name="Janet")

    alumnus = _run(session, payload)

    assert _history(session) == []
    assert not hasattr(alumnus, "archive_previous_role")


# --- No-op saves never manufacture history ------------------------------------


def test_flagged_but_unchanged_career_archives_nothing(monkeypatch):
    """A mis-ticked box on a save that changed nothing would otherwise clone the
    current role into history, leaving the record reading "left Acme this year,
    currently at Acme"."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Acme Corp", current_title="Analyst"),
    )

    _run(session, payload)

    assert _history(session) == []
    assert _audits(session) == []


def test_no_stored_role_archives_nothing(monkeypatch):
    """An alum whose FIRST employer is being entered has no previous role to
    demote. The flag is then simply nothing to act on, not an error."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession()  # no current_employment row on file
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital"),
    )

    _run(session, payload)

    assert _history(session) == []


def test_blank_stored_role_archives_nothing(monkeypatch):
    """An empty stored row is not a role. Archiving it would leave a blank entry
    on the Employment panel that a human then has to delete."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(
        current_employment=_stored_role(
            current_employer=None,
            current_title=None,
            current_industry=None,
            current_city=None,
            current_state=None,
        )
    )
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital"),
    )

    _run(session, payload)

    assert _history(session) == []


# --- The archived row is the OUTGOING one -------------------------------------


def test_archived_row_holds_the_outgoing_values_not_the_incoming_ones(monkeypatch):
    """Regression guard for the identity map. `_upsert_section` mutates the very
    `current_employment` row the archive reads from, and SQLAlchemy hands both
    the SAME object -- so a snapshot taken by reference rather than by value
    would archive the NEW role and lose the old one, silently, while every other
    assertion here still passed."""
    _patch_get(monkeypatch, _alumnus())
    stored = _stored_role()
    session = _ArchiveSession(current_employment=stored)
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital", current_title="Associate"),
    )

    _run(session, payload)

    (archived,) = _history(session)
    assert archived.employer_name == "Acme Corp"
    # And the current row really did move on.
    assert stored.current_employer == "Beta Capital"


# --- Dates: what we synthesise, and what we refuse to -------------------------


def test_archived_row_gets_no_start_year_and_this_years_end_year(monkeypatch):
    """`current_employment` has no start column, so a start year would be pure
    invention -- and the "worked in year X" filter reads `start_year` as a hard
    bound. `end_year` IS supplied, because a NULL end year already means "still
    held" everywhere it is read: an archived role without one would go on
    counting as a job the alum currently holds."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital"),
    )

    _run(session, payload)

    (archived,) = _history(session)
    assert archived.start_year is None
    assert archived.end_year == datetime.datetime.now(datetime.UTC).year


# --- The write nobody typed is in the audit trail -----------------------------


def test_archiving_writes_its_own_audit_row(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital"),
    )

    _run(session, payload)

    (row,) = _archive_audits(session)
    assert row.user_id == 9
    assert row.entity_type == "alumni"
    assert row.entity_id == 1
    # The row id rides in field_name -- audit_logs has no row-id column, and this
    # is the convention the per-row employment endpoints already use.
    assert row.field_name == "employment[77]"
    assert "employer_name='Acme Corp'" in row.new_value
    assert "end_year=" in row.new_value


def test_archive_audit_preserves_the_columns_history_cannot_hold(monkeypatch):
    """`employment_history` has no seniority / country / ZIP / secondary-industry
    columns, so those parts of the outgoing role would vanish on demotion. The
    audit snapshot is where they survive."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital"),
    )

    _run(session, payload)

    (row,) = _archive_audits(session)
    assert "seniority_level='Individual Contributor'" in row.old_value
    assert "current_country='United States'" in row.old_value
    assert "current_zip='84604'" in row.old_value


def test_archive_audit_shares_the_saves_change_set(monkeypatch):
    """One save = one version. The demotion and the new role's field changes are
    the same act, so they must group together rather than read as two unrelated
    events."""
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital"),
    )

    _run(session, payload)

    change_sets = {a.change_set_id for a in _audits(session)}
    assert len(change_sets) == 1
    assert None not in change_sets


def test_archive_audit_carries_write_provenance(monkeypatch):
    _patch_get(monkeypatch, _alumnus())
    session = _ArchiveSession(current_employment=_stored_role())
    payload = AlumniUpdateFull(
        archive_previous_role=True,
        career=CareerCreate(current_employer="Beta Capital"),
    )

    _run(session, payload)

    (row,) = _archive_audits(session)
    assert row.source == "manual"


# --- The other write paths are untouched --------------------------------------


def test_create_payload_rejects_the_flag():
    """A create has no outgoing role to archive, so the flag is REFUSED there
    (`extra="forbid"`) rather than accepted and quietly ignored -- a client that
    sent it would otherwise believe archiving had been considered."""
    with pytest.raises(ValidationError):
        AlumniCreateFull(
            first_name="Jane",
            last_name="Doe",
            byu_id="123456789",
            archive_previous_role=True,
        )


def test_the_flag_is_not_treated_as_data_to_clean_or_preview():
    """`/preview` renders `cleaned` as the record-to-be. A write control showing
    up in it would claim a checkbox is a stored field."""
    cleaned, changes = hygiene.clean_alumni_payload(
        AlumniUpdateFull(
            archive_previous_role=True,
            career=CareerCreate(current_employer="Beta Capital"),
        )
    )

    assert "archive_previous_role" not in cleaned
    assert all(c["field"] != "archive_previous_role" for c in changes)


def test_control_key_sets_agree():
    """Two modules hold the list because hygiene is imported BY the alumni
    service and cannot import back. Drift would be invisible: the cleaner would
    keep passing a control key through and the write path would try to `setattr`
    it onto the Alumni row."""
    assert service.CONTROL_KEYS == hygiene._CONTROL_KEYS


# =============================================================================
# The employer-change rule (survey + import trigger)
# =============================================================================
#
# The staff path asks a human. The survey and the import cannot, and the product
# owner decided on 2026-08-18 that they should INFER from the employer moving
# rather than never archive. He was shown the cost -- a typo fix, a company
# rename, or a cleanup pass can mint a prior role nobody held -- and accepted it.
# These tests pin how narrowly the comparison is drawn to limit that cost.


@pytest.mark.parametrize(
    "old, new",
    [
        ("Acme Corp", "Beta Capital"),  # the real case
        ("Acme Corp", "Acme Corporation"),  # indistinguishable from a real move
    ],
)
def test_employer_changed_is_true_for_a_different_name(old, new):
    assert service.employer_changed(old, new) is True


@pytest.mark.parametrize(
    "old, new, why",
    [
        ("Acme Corp", "Acme Corp", "identical"),
        ("Acme Corp", "ACME CORP", "casing cleanup"),
        ("Acme Corp", "  Acme   Corp  ", "whitespace cleanup"),
        (None, "Acme Corp", "first employer ever recorded"),
        ("", "Acme Corp", "blank stored value"),
        ("Acme Corp", None, "question skipped"),
        ("Acme Corp", "   ", "empty spreadsheet cell"),
    ],
)
def test_employer_changed_is_false(old, new, why):
    """Each of these is a mass-edit shape that would otherwise archive at scale.

    A casing or spacing pass is the single most likely bulk edit to this column.
    Blank on either side is the dangerous one: named -> blank on an 8,000-row
    sheet with one empty Employer column would demote every alum at once.
    """
    assert service.employer_changed(old, new) is False, why


# =============================================================================
# Survey apply path
# =============================================================================


def _survey_job(**overrides):
    values = {
        "alumni_id": 5,
        "current_employer": "Old Bank",
        "current_title": "Analyst",
        "current_industry": None,
        "current_industry_secondary": None,
        "current_city": "Provo",
        "current_state": "Utah",
        "current_country": None,
        "current_zip": None,
        "seniority_level": "Individual Contributor",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def test_survey_apply_archives_when_the_employer_moves(monkeypatch):
    job = _survey_job()
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(payload={"employment.current_employer": "Goldman Sachs"}),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )

    (archived,) = _history(session)
    assert archived.alumni_id == 5
    assert archived.employer_name == "Old Bank"
    assert archived.employment_title == "Analyst"
    assert archived.city == "Provo"
    assert archived.is_current is False
    assert archived.start_year is None
    assert archived.end_year == datetime.datetime.now(datetime.UTC).year
    # ...and the current row really did move on.
    assert job.current_employer == "Goldman Sachs"


def test_survey_apply_does_not_archive_on_a_no_op_reconfirm(monkeypatch):
    """The dominant case by far. An alum re-confirming their details resubmits
    every answer, so if a re-confirm archived, an ordinary confirmation round
    would duplicate the current role into history for the whole cohort."""
    job = _survey_job()
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(
            payload={
                "employment.current_employer": "Old Bank",
                "employment.current_title": "Analyst",
            }
        ),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )

    assert _history(session) == []
    assert _archive_audits(session) == []


def test_survey_apply_does_not_archive_on_a_title_only_change(monkeypatch):
    """A promotion at the same employer is a real role change that this path
    deliberately does NOT catch -- the trigger is the EMPLOYER, and inferring
    from the title too would fire on every job-title tidy-up."""
    job = _survey_job()
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(payload={"employment.current_title": "Associate"}),
        monkeypatch,
        side_rows=({}, {5: job}, {}),
    )

    assert _history(session) == []


def test_survey_apply_does_not_archive_a_first_ever_employer(monkeypatch):
    """An alum with no employment row at all has nothing to demote. The row is
    CREATED by the apply, so archiving it would file the brand-new job as one
    they had already left."""
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(payload={"employment.current_employer": "Goldman Sachs"}),
        monkeypatch,
        side_rows=({}, {}, {}),
    )

    assert _history(session) == []


def test_survey_archive_audit_is_stamped_survey_not_manual(monkeypatch):
    """`source` is what lets a later restore tell an alum's own answer from a
    staff edit from a spreadsheet overwrite. A demotion nobody typed is exactly
    the row that needs it."""
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(payload={"employment.current_employer": "Goldman Sachs"}),
        monkeypatch,
        side_rows=({}, {5: _survey_job()}, {}),
    )

    (row,) = _archive_audits(session)
    assert row.source == "survey"
    assert row.field_name == "employment[77]"
    assert "seniority_level='Individual Contributor'" in row.old_value


def test_survey_archive_shares_the_approvals_change_set(monkeypatch):
    """The summary row, the field rows and the demotion are one approval, so
    version history must read them as one version."""
    session = _SurveySession(_survey_alum())
    _apply(
        session,
        _fake_resp(payload={"employment.current_employer": "Goldman Sachs"}),
        monkeypatch,
        side_rows=({}, {5: _survey_job()}, {}),
    )

    change_sets = {a.change_set_id for a in _audits(session)}
    assert len(change_sets) == 1
    assert None not in change_sets


# =============================================================================
# CSV import path
# =============================================================================


class _ImportSession(FakeUpdateSession):
    """`FakeUpdateSession` whose flush also assigns `employment_history_id`."""

    async def flush(self) -> None:
        await super().flush()
        for obj in self.added:
            if isinstance(obj, EmploymentHistory) and obj.employment_history_id is None:
                obj.employment_history_id = 77


def _import_session(employment) -> _ImportSession:
    return _ImportSession(
        index_rows=[(1, "123456789", None, "Jane", "Doe", 2018, False)],
        alumni={
            1: Alumni(alumni_id=1, byu_id="123456789", first_name="Jane", archived=False)
        },
        sections={"current_employment": employment} if employment is not None else {},
    )


def _import_employment(**overrides) -> CurrentEmployment:
    values: dict[str, object] = {
        "current_employment_id": 5,
        "alumni_id": 1,
        "current_employer": "Old Co",
        "current_title": "Analyst",
        "seniority_level": "Individual Contributor",
    }
    values.update(overrides)
    return CurrentEmployment(**values)


def _import(session, **cells):
    csv = _csv_bytes(_row_values(byu_id="123456789", **cells))
    rows, _ = import_csv.parse_and_map(csv)
    # An actor is REQUIRED for the audit assertions: `_audit` no-ops when the
    # acting user is unknown, so an actor-less import would "pass" the audit tests
    # by writing no rows at all.
    return asyncio.run(
        import_csv.commit_update(session, rows, actor_user_id=9)
    )


def test_import_archives_when_the_sheet_names_a_different_employer():
    employment = _import_employment()
    session = _import_session(employment)

    result = _import(session, current_employer="New Co")

    assert result["updated"] == 1
    (archived,) = _history(session)
    assert archived.employer_name == "Old Co"
    assert archived.employment_title == "Analyst"
    assert archived.is_current is False
    assert employment.current_employer == "New Co"


def test_import_does_not_archive_when_the_employer_cell_is_unchanged():
    """A round-trip export/edit resubmits the employer on every row. If that
    archived, one unrelated cell edit would demote the whole file's roles."""
    employment = _import_employment()
    session = _import_session(employment)

    _import(session, current_employer="Old Co", current_city="New York")

    assert _history(session) == []
    assert _archive_audits(session) == []
    # The row really was applied -- this is a no-archive, not a no-write.
    assert employment.current_city == "New York"


def test_import_does_not_archive_on_a_blank_employer_cell():
    """A blank cell means "unchanged" in update mode -- `_build_update_model`
    merges the stored value back in. This is the failure that would hurt most at
    scale: a sheet exported without the Employer column would otherwise demote
    every alum in it."""
    employment = _import_employment()
    session = _import_session(employment)

    _import(session, current_city="New York")

    assert _history(session) == []
    assert employment.current_employer == "Old Co"
    assert employment.current_city == "New York"


def test_import_archive_audit_is_stamped_import_not_manual():
    session = _import_session(_import_employment())

    _import(session, current_employer="New Co")

    (row,) = _archive_audits(session)
    assert row.source == "import"
    assert row.field_name == "employment[77]"
    assert "seniority_level='Individual Contributor'" in row.old_value
    assert "employer_name='Old Co'" in row.new_value


def test_import_archive_shares_the_rows_change_set():
    session = _import_session(_import_employment())

    _import(session, current_employer="New Co")

    change_sets = {a.change_set_id for a in _audits(session)}
    assert len(change_sets) == 1
    assert None not in change_sets


# =============================================================================
# All three paths agree
# =============================================================================


def test_every_path_uses_the_one_archive_implementation():
    """Three triggers, ONE writer. A second implementation for the survey or the
    import would drift -- different columns copied, a different end-year rule --
    and the Employment panel would show rows whose shape depended on which path
    happened to demote them."""
    assert survey_responses.archive_current_role is service.archive_current_role
    assert survey_responses.audit_role_archive is service.audit_role_archive
    assert import_csv.alumni_service.employer_changed is service.employer_changed

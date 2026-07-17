"""Section-write rules on the alumni update path — offline, no database.

Two fixes that meet in ``_upsert_section``:

  * **#283 — the derived region must actually reach the DB.** ``hygiene`` derives
    ``region`` from the work state into ``cleaned["contact"]["region"]``, but the
    section write is driven by the raw payload. An employment-only edit sends no
    contact section, so without the merge in ``update_alumni`` the region is
    dropped on write while ``/preview`` still promises it — the failure mode these
    tests exist to catch, since it looks like it worked.
  * **#285 — a no-op save must not bump "last updated".** Opening Edit ->
    Employment and saving unchanged submits a full, populated section; it must not
    stamp ``updated_at`` / ``manually_edited_at`` / ``profile_updated_by_user_id``.

Mirrors tests/test_alumni_service.py: a fake session captures add/commit/refresh
and returns a pre-seeded section row for the upsert query, so the rules are
exercised without touching Postgres.
"""

import asyncio
import datetime

from sqlalchemy.dialects import postgresql

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.schemas.alumni import (
    AlumniUpdate,
    AlumniUpdateFull,
    CareerCreate,
    ContactCreate,
)
from app.services import alumni as service


class _EmptyScalars:
    def all(self):
        return []


class _EmptyResult:
    def scalars(self):
        return _EmptyScalars()


class FakeSession:
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


class _SectionSession(FakeSession):
    """Returns pre-seeded section rows for the section upsert queries.

    Keyed by table name against the compiled SQL — the byu/net-id duplicate
    lookups target the ``alumni`` table and still fall through to None.
    """

    def __init__(self, **rows: object) -> None:
        super().__init__()
        self._rows = rows

    async def scalar(self, stmt: object) -> object | None:
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        for table, row in self._rows.items():
            if table in sql:
                return row
        return None


def _patch_get(monkeypatch, value):
    async def fake_get(session, alumni_id):
        return value

    monkeypatch.setattr(service.repo, "get", fake_get)


def _alumnus() -> Alumni:
    return Alumni(alumni_id=1, first_name="Jane", last_name="Doe", archived=False)


# --- #283: the derived region reaches the DB --------------------------------


def test_employment_only_edit_writes_the_derived_region(monkeypatch):
    """THE regression. Tanya changes only the work state; no contact section is
    sent, yet the region must be derived AND persisted onto the contact row."""
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_state="Utah"
    )
    session = _SectionSession(
        alumni_contact_info=contact_row, current_employment=employment_row
    )
    payload = AlumniUpdateFull(career=CareerCreate(current_state="TX"))
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert employment_row.current_state == "Texas"  # cleaned to the full name
    assert contact_row.region == "Southwest"  # derived from the WORK state
    assert session.committed == 1


def test_derived_region_inserts_a_contact_row_when_none_exists(monkeypatch):
    # No contact row yet: the derived region is real data, so it gets one.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdateFull(career=CareerCreate(current_state="Texas"))
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    contact = next(o for o in session.added if isinstance(o, AlumniContactInfo))
    assert contact.region == "Southwest"


def test_derived_region_stamps_updated_by_and_manual_edit(monkeypatch):
    # A region that only moves because the state moved is still a real change, so
    # the provenance stamps must fire (#285) even though the alumni core row and
    # the submitted section both look untouched on the contact side.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(career=CareerCreate(current_state="Texas"))
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert obj.profile_updated_by_user_id == 9
    assert obj.manually_edited_at is not None


def test_explicit_region_wins_over_the_derived_one(monkeypatch):
    # The escape hatch (#283): when the caller supplies a region, the map never
    # overrides it — even though the work state would derive a different one.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(
        career=CareerCreate(current_state="Texas"),  # would derive Southwest
        contact=ContactCreate(region="Northeast"),
    )
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.region == "Northeast"


def test_contact_edit_alongside_a_state_change_keeps_both(monkeypatch):
    # A contact section that carries other fields but no region: the sent fields
    # are written AND the derived region is merged in, not dropped by either side.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    contact_row = AlumniContactInfo(
        contact_info_id=5, alumni_id=1, city="Provo", region="West"
    )
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(
        career=CareerCreate(current_state="Texas"),
        contact=ContactCreate(city="Austin"),
    )
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.city == "Austin"
    assert contact_row.region == "Southwest"


def test_edit_that_does_not_touch_the_work_state_leaves_region_alone(monkeypatch):
    # No current_state in the payload -> nothing to derive from, so the stored
    # region must not move (and the contact row must not be touched at all).
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(career=CareerCreate(current_title="Associate"))
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.region == "West"


def test_non_us_work_state_does_not_blank_the_region(monkeypatch):
    # The five regions are US-only: an unrecognized state derives nothing rather
    # than clearing what's stored.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(alumni_contact_info=contact_row)
    payload = AlumniUpdateFull(career=CareerCreate(current_state="Ontario"))
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.region == "West"


# --- #285: a no-op save must not bump "last updated" ------------------------


def test_unchanged_section_save_does_not_bump_anything(monkeypatch):
    """THE regression. Tanya opens Edit -> Employment, changes nothing, saves.
    The profile must not read "updated today by Tanya"."""
    existing = _alumnus()
    existing.profile_updated_by_user_id = 3
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3,
        alumni_id=1,
        current_employer="Acme Corp",
        current_title="Analyst",
        current_city="Boston",
        current_state="Massachusetts",
    )
    # The state is re-submitted unchanged, so the region never even re-derives
    # (see test_unchanged_state_leaves_a_contradicting_region_alone).
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="Northeast")
    session = _SectionSession(
        current_employment=employment_row, alumni_contact_info=contact_row
    )
    # The form re-submits every field exactly as loaded.
    payload = AlumniUpdateFull(
        career=CareerCreate(
            current_employer="Acme Corp",
            current_title="Analyst",
            current_city="Boston",
            current_state="Massachusetts",
        )
    )
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert obj.manually_edited_at is None  # no manual-edit stamp
    assert obj.profile_updated_by_user_id == 3  # credit not stolen
    assert session.committed == 0  # nothing written -> updated_at can't bump


def test_real_section_edit_still_bumps_everything(monkeypatch):
    # The other half: a genuine change must still stamp all three, or the fix
    # above would have broken the feature it is protecting.
    existing = _alumnus()
    existing.profile_updated_by_user_id = 3
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3,
        alumni_id=1,
        current_employer="Acme Corp",
        current_title="Analyst",
    )
    session = _SectionSession(current_employment=employment_row)
    payload = AlumniUpdateFull(
        career=CareerCreate(current_employer="Acme Corp", current_title="Associate")
    )
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert employment_row.current_title == "Associate"
    assert obj.manually_edited_at is not None
    assert obj.profile_updated_by_user_id == 9
    assert session.committed == 1


def test_unchanged_save_after_cleaning_does_not_bump(monkeypatch):
    # The typed value differs from the stored one only by whitespace that hygiene
    # collapses away ("  Acme   Corp " -> "Acme Corp"): still a no-op.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_employer="Acme Corp"
    )
    session = _SectionSession(current_employment=employment_row)
    payload = AlumniUpdateFull(career=CareerCreate(current_employer="  Acme   Corp "))
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert obj.manually_edited_at is None
    assert session.committed == 0


def test_unchanged_state_leaves_a_contradicting_region_alone(monkeypatch):
    """A region that contradicts the map may be a DELIBERATE override (a remote
    worker, someone relocating, a regional-team assignment), so re-saving the
    work state unchanged must leave it exactly as-is.

    #283 is auto-fill-but-overridable: an override that evaporates the next time
    someone opens the Employment card isn't an override. Deriving only on a real
    state change is what makes it durable — and keeps #285's date honest, since
    an untouched record must not read "updated today".
    """
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_state="Texas"
    )
    contact_row = AlumniContactInfo(
        contact_info_id=5, alumni_id=1, region="West"  # deliberate override
    )
    session = _SectionSession(
        current_employment=employment_row, alumni_contact_info=contact_row
    )
    payload = AlumniUpdateFull(career=CareerCreate(current_state="Texas"))
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.region == "West"  # her call, untouched
    assert obj.manually_edited_at is None  # and nothing was "updated"
    assert session.committed == 0


def test_state_code_for_the_same_stored_state_is_not_a_change(monkeypatch):
    # "TX" against a stored "Texas" is the same state: no derive, no bump.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_state="Texas"
    )
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(
        current_employment=employment_row, alumni_contact_info=contact_row
    )
    payload = AlumniUpdateFull(career=CareerCreate(current_state="TX"))
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.region == "West"
    assert obj.manually_edited_at is None
    assert session.committed == 0


def test_changed_state_derives_over_a_contradicting_region(monkeypatch):
    # The other half: once the state genuinely MOVES, the map takes over again —
    # the override was for the old state, not a permanent opt-out.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_state="Utah"
    )
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(
        current_employment=employment_row, alumni_contact_info=contact_row
    )
    payload = AlumniUpdateFull(career=CareerCreate(current_state="Texas"))
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.region == "Southwest"
    assert obj.manually_edited_at is not None


def test_explicit_region_wins_on_a_changed_state(monkeypatch):
    # Setting the state AND the region in one save: hers wins over the map.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_state="Utah"
    )
    contact_row = AlumniContactInfo(contact_info_id=5, alumni_id=1, region="West")
    session = _SectionSession(
        current_employment=employment_row, alumni_contact_info=contact_row
    )
    payload = AlumniUpdateFull(
        career=CareerCreate(current_state="Texas"),  # would derive Southwest
        contact=ContactCreate(region="West"),
    )
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert contact_row.region == "West"


def test_no_op_section_save_leaves_the_row_untouched(monkeypatch):
    # An unchanged field is never re-assigned, so a no-op save can't dirty the
    # session and get flushed by someone else's commit.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_employer="Acme Corp"
    )
    session = _SectionSession(current_employment=employment_row)
    payload = AlumniUpdateFull(career=CareerCreate(current_employer="Acme Corp"))
    asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert employment_row not in session.added
    assert session.committed == 0


def test_core_edit_alongside_an_unchanged_section_still_bumps(monkeypatch):
    # A no-op section must not SUPPRESS a real core change either.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3, alumni_id=1, current_employer="Acme Corp"
    )
    session = _SectionSession(current_employment=employment_row)
    payload = AlumniUpdateFull(
        first_name="Janet", career=CareerCreate(current_employer="Acme Corp")
    )
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert obj.first_name == "Janet"
    assert obj.manually_edited_at is not None
    assert obj.profile_updated_by_user_id == 9


def test_clearing_a_populated_field_counts_as_a_change(monkeypatch):
    # Blank-vs-value IS a change: an intentional clear must persist and bump.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    employment_row = CurrentEmployment(
        current_employment_id=3,
        alumni_id=1,
        current_employer="Acme Corp",
        current_title="Analyst",
    )
    session = _SectionSession(current_employment=employment_row)
    payload = AlumniUpdateFull(
        career=CareerCreate(current_employer="Acme Corp", current_title=None)
    )
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert employment_row.current_title is None
    assert obj.manually_edited_at is not None


# --- the comparison itself ---------------------------------------------------


def test_blank_variants_compare_equal():
    # A legacy row holding "" against a cleaned None means the same thing; calling
    # it a change would bump the date on every save of that record, forever.
    assert service._unchanged(None, None)
    assert service._unchanged("", None)
    assert service._unchanged(None, "")
    assert service._unchanged("   ", None)
    assert not service._unchanged(None, "Acme")
    assert not service._unchanged("Acme", None)


def test_date_and_datetime_compare_as_instants():
    # date vs datetime, and naive vs aware, must not read as a permanent change.
    assert service._unchanged(
        datetime.date(2020, 1, 1), datetime.datetime(2020, 1, 1)
    )
    assert service._unchanged(
        datetime.datetime(2020, 1, 1),
        datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC),
    )
    assert not service._unchanged(
        datetime.date(2020, 1, 1), datetime.date(2020, 1, 2)
    )


def test_booleans_and_numbers_compare_by_value():
    assert service._unchanged(False, False)
    assert service._unchanged(True, True)
    assert not service._unchanged(True, False)
    assert service._unchanged(2020, 2020)
    assert not service._unchanged(2020, 2021)


def test_update_with_no_sections_is_still_a_noop(monkeypatch):
    # Guards the plain-core path against the section changes above.
    existing = _alumnus()
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(service.update_alumni(session, 1, AlumniUpdate(), actor_user_id=9))
    assert obj.manually_edited_at is None
    assert session.committed == 0

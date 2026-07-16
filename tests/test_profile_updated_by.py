"""Last-updated provenance (#285) — offline, no database.

Covers the two halves of the fix:
  * the service stamps ``profile_updated_by_user_id`` from the ACTING user on
    every write that actually changes something, so the profile can render
    "Last updated <updated_at> by <name>" instead of a hand-typed date;
  * the route boundary drops the hand-typed ``profile_updated_date`` from client
    writes WITHOUT clearing the stored intake-sheet value.

Mirrors tests/test_alumni_service.py: a fake session captures add/commit/refresh
and the repository's ``get`` is monkeypatched, so the rules are exercised without
touching Postgres.
"""

import asyncio
import datetime

from app.api.routes import alumni as routes
from app.models.alumni import Alumni
from app.schemas.alumni import (
    AlumniCreate,
    AlumniCreateFull,
    AlumniUpdate,
    AlumniUpdateFull,
    CareerCreate,
)
from app.services import alumni as service


class _EmptyScalars:
    def all(self):
        return []


class _EmptyResult:
    def scalars(self):
        return _EmptyScalars()


class FakeSession:
    """Returns nothing for every lookup, so no fixture ever looks duplicated."""

    def __init__(self) -> None:
        self.added: list[object] = []
        self.committed = 0

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def scalar(self, stmt: object) -> None:
        return None

    async def execute(self, stmt: object) -> _EmptyResult:
        return _EmptyResult()

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed += 1

    async def refresh(self, obj: object) -> None:
        pass


def _patch_get(monkeypatch, value):
    async def fake_get(session, alumni_id):
        return value

    monkeypatch.setattr(service.repo, "get", fake_get)


# --- Service: the actor is stamped on write ---------------------------------


def test_create_stamps_actor_as_updater():
    session = FakeSession()
    payload = AlumniCreate(first_name="Jane", last_name="Doe")
    obj = asyncio.run(service.create_alumni(session, payload, actor_user_id=7))
    assert obj.profile_updated_by_user_id == 7


def test_create_without_actor_leaves_updater_unset():
    # Direct service callers / tests pass no actor; we must not invent one.
    session = FakeSession()
    payload = AlumniCreate(first_name="Jane", last_name="Doe")
    obj = asyncio.run(service.create_alumni(session, payload))
    assert obj.profile_updated_by_user_id is None


def test_update_stamps_actor_as_updater(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    existing.profile_updated_by_user_id = 3
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdate(first_name="Janet")
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert obj.first_name == "Janet"
    # Re-attributed to whoever made THIS edit.
    assert obj.profile_updated_by_user_id == 9
    assert obj.manually_edited_at is not None


def test_update_with_no_effective_change_does_not_reattribute(monkeypatch):
    # A save that changes nothing must not steal credit from the previous editor
    # (and must not bump updated_at) — gated on the same condition as
    # manually_edited_at.
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    existing.profile_updated_by_user_id = 3
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdate(first_name="Jane")
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert obj.profile_updated_by_user_id == 3
    assert obj.manually_edited_at is None
    assert session.committed == 0


def test_update_without_actor_preserves_previous_updater(monkeypatch):
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    existing.profile_updated_by_user_id = 3
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    obj = asyncio.run(service.update_alumni(session, 1, AlumniUpdate(first_name="Janet")))
    # No actor -> leave the resolved user in place rather than writing NULL.
    assert obj.profile_updated_by_user_id == 3


def test_section_only_edit_stamps_actor_on_the_alumni_row(monkeypatch):
    # The card's motivating case: Tanya edits EMPLOYMENT only. The alumni row must
    # still be touched so updated_at bumps and the actor is recorded.
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdateFull(career=CareerCreate(current_employer="Goldman Sachs"))
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=11))
    assert obj.profile_updated_by_user_id == 11
    assert obj.manually_edited_at is not None


# --- Route boundary: the hand-typed date is not accepted --------------------


def test_route_drops_manual_updated_date_from_create():
    payload = AlumniCreateFull(
        first_name="Jane", last_name="Doe", profile_updated_date="2020-01-01"
    )
    assert "profile_updated_date" in payload.__pydantic_fields_set__
    cleaned = routes._drop_manual_updated_date(payload)
    assert "profile_updated_date" not in cleaned.__pydantic_fields_set__


def test_dropped_date_never_reaches_the_write_path(monkeypatch):
    # The important half: unset (not None), so exclude_unset drops it entirely and
    # the stored intake-sheet date survives the save rather than being nulled.
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdateFull(first_name="Janet", profile_updated_date="2020-01-01")
    obj = asyncio.run(
        service.update_alumni(
            session, 1, routes._drop_manual_updated_date(payload), actor_user_id=9
        )
    )
    assert obj.first_name == "Janet"
    assert obj.profile_updated_date is None


def test_dropped_date_does_not_clear_a_stored_sheet_date(monkeypatch):
    stored = datetime.date(2019, 5, 4)
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    existing.profile_updated_date = stored
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdateFull(first_name="Janet", profile_updated_date="2020-01-01")
    obj = asyncio.run(
        service.update_alumni(
            session, 1, routes._drop_manual_updated_date(payload), actor_user_id=9
        )
    )
    # The spreadsheet's claim is provenance — preserved untouched.
    assert obj.profile_updated_date == stored


def test_importer_can_still_write_the_sheet_date(monkeypatch):
    # The CSV importer calls the SERVICE directly (not the route), so the intake
    # sheet's "Profile Updated Date" column still lands. Guards against the strip
    # being pushed down into the service.
    existing = Alumni(alumni_id=1, first_name="Jane", last_name="Doe")
    _patch_get(monkeypatch, existing)
    session = FakeSession()
    payload = AlumniUpdateFull(profile_updated_date="2020-01-01")
    obj = asyncio.run(service.update_alumni(session, 1, payload, actor_user_id=9))
    assert obj.profile_updated_date == datetime.date(2020, 1, 1)


# --- The write schema stays LEGAL but is advertised read-only ----------------
#
# These two guard opposite failure modes of the same decision (#285). The schema
# must keep ACCEPTING the field (the CSV importer builds these very models from
# intake-sheet cells, and ``extra='forbid'`` would turn a deletion into an import
# blocker), while OpenAPI must ADVERTISE it as read-only (over HTTP the route
# strips it, so a client that sends it gets a silent no-op).


def test_write_schemas_still_accept_the_sheet_date():
    # The exact construction services/import_csv.py performs. If someone "cleans
    # up" the field off AlumniBase, this raises ValidationError (extra='forbid')
    # -- which is precisely how the intake sheet would break in production.
    created = AlumniCreateFull(
        first_name="Jane", last_name="Doe", profile_updated_date="2020-01-01"
    )
    assert created.profile_updated_date == datetime.date(2020, 1, 1)
    updated = AlumniUpdateFull(profile_updated_date="2020-01-01")
    assert updated.profile_updated_date == datetime.date(2020, 1, 1)


def test_openapi_marks_the_sheet_date_read_only_on_write_schemas():
    # Asserted against the REAL generated document, not the annotation on the
    # class: the field is declared on AlumniBase and must survive the Base ->
    # Create/Update -> *Full subclass chain into the served schema.
    from app.main import app

    schemas = app.openapi()["components"]["schemas"]
    for name in ("AlumniCreateFull", "AlumniUpdateFull"):
        prop = schemas[name]["properties"]["profile_updated_date"]
        assert prop.get("readOnly") is True, (
            f"{name}.profile_updated_date must be advertised readOnly -- the route "
            "silently drops it from client writes (#285)."
        )
    # The read schema is a separate class and must NOT pick up the annotation.
    read_prop = schemas["AlumniRead"]["properties"]["profile_updated_date"]
    assert "readOnly" not in read_prop

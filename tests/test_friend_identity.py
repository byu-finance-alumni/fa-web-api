"""Friend-of-the-program identity (#538).

Jake's decision (2026-09-16): "email + visible friend id".

  * two friend rows are the same person when their EMAIL matches, case- and
    whitespace-insensitively, across ALL events; a row with no email falls
    back to the name + employer key, also across all events;
  * a friend row is never linked to an alumnus -- an email that belongs to a
    real alumnus refuses the row instead of creating a friend;
  * every friend record carries a visible ``FRIEND-00042`` id, derived from
    the primary key, on the list, the profile and the CSV export, and the
    search accepts it.

No DATABASE_URL: the route tests drive a hand-rolled session that answers the
three batched lookups by inspecting the statement, and the rest are pure.
"""

from __future__ import annotations

import csv
import datetime
import io
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.friend_id import friend_id_for, parse_friend_id
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.event import EventAttendance
from app.repositories.alumni import build_alumni_query
from app.schemas.alumni import (
    AlumniListItem,
    AlumniRead,
    AlumniWriteResult,
    minimize_alumni_read,
)
from app.schemas.auth import UserContext
from app.services import alumni as alumni_service
from app.services import alumni_export, attendee_match, friend_identity

# --- The identifier -----------------------------------------------------------


def test_friend_id_is_derived_from_the_primary_key_and_zero_padded():
    assert friend_id_for(42, False) == "FRIEND-00042"
    assert friend_id_for(7, False) == "FRIEND-00007"
    # Past five digits it simply grows; nothing is truncated.
    assert friend_id_for(123456, False) == "FRIEND-123456"


def test_friend_id_is_null_for_every_alumnus():
    assert friend_id_for(42, True) is None
    assert friend_id_for(42, None) is None
    assert friend_id_for(None, False) is None


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("FRIEND-00042", 42),
        ("friend-42", 42),
        ("  Friend_00042 ", 42),
        ("friend00042", 42),  # what the free-text normaliser hands over
        ("friend 42", 42),
        ("FRIEND-0", None),
        ("friend", None),
        ("friendly", None),
        ("42", None),
        ("jdoe1", None),
        (None, None),
        (42, None),
    ],
)
def test_parse_friend_id_accepts_the_visible_form_and_nothing_else(typed, expected):
    assert parse_friend_id(typed) == expected


# --- The same id everywhere: list, profile, write result, export -------------


def _orm(alumni_id: int, *, is_alumni: bool) -> Alumni:
    now = datetime.datetime(2026, 9, 16, tzinfo=datetime.UTC)
    return Alumni(
        alumni_id=alumni_id,
        first_name="Jane",
        last_name="Doe",
        is_alumni=is_alumni,
        deceased=False,
        archived=False,
        created_at=now,
        updated_at=now,
    )


def test_list_profile_and_write_reads_all_carry_the_same_friend_id():
    friend = _orm(42, is_alumni=False)
    assert AlumniRead.model_validate(friend).friend_id == "FRIEND-00042"
    assert AlumniListItem.model_validate(friend).friend_id == "FRIEND-00042"
    assert AlumniWriteResult.model_validate(friend).friend_id == "FRIEND-00042"
    # The export reads the ORM attribute -- same helper, same value.
    assert friend.friend_id == "FRIEND-00042"
    # And it is in the serialized body, not only on the Python object.
    assert AlumniRead.model_validate(friend).model_dump()["friend_id"] == "FRIEND-00042"


def test_an_alumnus_has_no_friend_id_anywhere():
    alum = _orm(42, is_alumni=True)
    assert AlumniRead.model_validate(alum).friend_id is None
    assert AlumniListItem.model_validate(alum).friend_id is None
    assert alum.friend_id is None
    assert "friend_id" in AlumniRead.model_validate(alum).model_dump()


def test_friend_id_survives_view_only_minimisation():
    """It is not PII -- a view_only caller still sees which friend is which."""
    read = AlumniRead.model_validate(_orm(42, is_alumni=False))
    assert minimize_alumni_read(read, can_edit=False).friend_id == "FRIEND-00042"


def test_friend_id_cannot_be_supplied_by_a_client():
    """Derived, never accepted: a create payload carrying one is refused
    (extra='forbid'), so nobody can mint or spoof an id."""
    from pydantic import ValidationError

    from app.schemas.alumni import AlumniCreateFull

    with pytest.raises(ValidationError):
        AlumniCreateFull(first_name="Jane", last_name="Doe", friend_id="FRIEND-00001")


class _ExportResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _ExportSession:
    def __init__(self, alumni):
        self._alumni = alumni
        self.added: list = []

    async def scalar(self, _stmt):
        return len(self._alumni)

    async def execute(self, _stmt):
        rows, self._alumni = self._alumni, []
        return _ExportResult(rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        return None


@pytest.mark.anyio
async def test_export_carries_the_friend_id_column():
    from app.schemas.alumni_export import AlumniExportFilters

    session = _ExportSession([_orm(42, is_alumni=False), _orm(43, is_alumni=True)])
    text = await alumni_export.export_csv(
        session,
        columns=alumni_export.validate_columns(["friend_id", "first_name"]),
        filters=AlumniExportFilters(),
        actor_user_id=1,
    )
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == ["Friend ID", "First name"]
    assert rows[1] == ["FRIEND-00042", "Jane"]
    assert rows[2] == ["", "Jane"]


def test_export_catalog_offers_friend_id_under_identity():
    col = {c.key: c for c in alumni_export.CATALOG}["friend_id"]
    assert col.label == "Friend ID"
    assert col.group == "Identity"


# --- Search ------------------------------------------------------------------


def _compiled(stmt):
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), list(compiled.params.values())


def test_a_typed_friend_id_in_the_search_box_finds_the_record():
    sql, params = _compiled(build_alumni_query(q="FRIEND-00042", is_alumni=None))
    assert "alumni.is_alumni IS false" in sql
    assert 42 in params


def test_a_typed_friend_id_in_the_net_id_box_finds_the_record():
    sql, params = _compiled(build_alumni_query(net_id="FRIEND-00042", is_alumni=None))
    assert "alumni.is_alumni IS false" in sql
    assert 42 in params
    # It replaces the Net ID LIKE, it does not AND with it (friends have none).
    assert "%FRIEND-00042%" not in params


def test_a_word_that_merely_starts_with_friend_is_an_ordinary_search():
    sql, params = _compiled(build_alumni_query(q="friendly", is_alumni=None))
    assert "alumni.is_alumni IS false" not in sql
    sql, params = _compiled(build_alumni_query(net_id="jdoe1", is_alumni=None))
    assert "alumni.is_alumni IS false" not in sql
    assert "%jdoe1%" in params


def test_a_friend_id_never_finds_an_alumnus_with_that_primary_key():
    """The is_alumni guard is what makes it a FRIEND id."""
    sql, _params = _compiled(build_alumni_query(q="FRIEND-00017", is_alumni=None))
    assert "alumni.alumni_id = " in sql
    assert "alumni.is_alumni IS false" in sql


# --- The dedupe rule (pure) --------------------------------------------------


def _row(**kwargs) -> dict:
    base = {
        "row": 2,
        "display_name": "Jane Doe",
        "first_name": "Jane",
        "preferred_first_name": None,
        "last_name": "Doe",
        "maiden_name": None,
        "emails": [],
        "company": "Byrne Capital",
        "graduation_year": None,
        "note": None,
        "payload": {"first_name": "Jane", "last_name": "Doe", "is_alumni": False},
        "cell_warnings": [],
    }
    base.update(kwargs)
    return base


def test_email_identifies_a_friend_across_events():
    index = friend_identity.FriendIndex(friends_by_email={"jane@x.com": [42]})
    decision = index.decide(_row(emails=["jane@x.com"]))
    assert decision.kind == "reuse"
    assert decision.alumni_id == 42
    assert decision.matched_on == "email"


def test_email_wins_over_a_different_employer():
    """Job changes do not duplicate when the row carries an email."""
    index = friend_identity.FriendIndex(
        friends_by_email={"jane@x.com": [42]},
        friends_by_key={attendee_match.friend_identity_key("Jane", "Doe", "Old Co"): 9},
    )
    decision = index.decide(_row(emails=["jane@x.com"], company="New Co"))
    assert decision.kind == "reuse"
    assert decision.alumni_id == 42


def test_a_row_with_an_email_does_not_fall_back_to_the_name_key():
    """Email present and unknown -> a NEW friend, even if a friend elsewhere
    shares the name + employer (a genuinely different person at the same
    firm). The fallback is for rows with no email only."""
    index = friend_identity.FriendIndex(
        friends_by_key={attendee_match.friend_identity_key("Jane", "Doe", "Byrne Capital"): 9}
    )
    assert index.decide(_row(emails=["other@x.com"])).kind == "create"


def test_no_email_falls_back_to_name_plus_employer_across_events():
    index = friend_identity.FriendIndex(
        friends_by_key={attendee_match.friend_identity_key("Jane", "Doe", "Byrne Capital"): 9}
    )
    decision = index.decide(_row(emails=[], company="Byrne Capital LLC"))
    assert decision.kind == "reuse"
    assert decision.alumni_id == 9
    assert decision.matched_on == "name"


def test_no_email_and_a_new_employer_creates_a_twin_the_accepted_trade_off():
    index = friend_identity.FriendIndex(
        friends_by_key={attendee_match.friend_identity_key("Jane", "Doe", "Old Co"): 9}
    )
    assert index.decide(_row(emails=[], company="New Co")).kind == "create"


def test_an_alumnus_email_refuses_the_row_even_when_a_friend_also_has_it():
    index = friend_identity.FriendIndex(
        alumni_by_email={"jane@x.com": [5]},
        friends_by_email={"jane@x.com": [42]},
    )
    decision = index.decide(_row(emails=["jane@x.com"]))
    assert decision.kind == "existing_alumnus"
    assert decision.alumni_id == 5


def test_the_oldest_friend_wins_when_prod_already_holds_twins():
    index = friend_identity.FriendIndex(friends_by_email={"jane@x.com": [42, 77]})
    assert index.decide(_row(emails=["jane@x.com"])).alumni_id == 42


def test_remember_makes_a_second_row_in_the_same_file_resolve_to_the_first():
    index = friend_identity.FriendIndex()
    first = _row(emails=["jane@x.com"])
    assert index.decide(first).kind == "create"
    index.remember(first, 42)
    # Same email, different spelling of the name: still her.
    again = index.decide(_row(first_name="Janet", emails=["jane@x.com"]))
    assert again.kind == "reuse"
    assert again.alumni_id == 42
    assert 42 in index.attending_ids


# --- The batched lookups -----------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _FriendsSession:
    """Answers the friends route's three batched lookups by inspecting the
    statement: the roster (joins event_attendance), the email owners (reads
    personal_email) and the name-key friends (everything else). Records the
    rows added so a test can prove what was, and was not, written."""

    def __init__(self, *, roster=(), email_hits=(), name_hits=(), event=None):
        self._event = event if event is not None else SimpleNamespace(event_id=7)
        self._roster = list(roster)
        self._email_hits = list(email_hits)
        self._name_hits = list(name_hits)
        self.queries: list[str] = []
        self.added: list = []
        self.committed = 0
        self.savepoints = 0

    async def get(self, _model, _pk):
        return self._event

    async def execute(self, stmt):
        sql = str(stmt)
        self.queries.append(sql)
        if "event_attendance" in sql:
            return _Result(self._roster)
        if "personal_email" in sql:
            return _Result(self._email_hits)
        return _Result(self._name_hits)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def flush(self):
        return None

    def begin_nested(self):
        self.savepoints += 1
        session = self

        class _Savepoint:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def rollback(self):
                session.rolled_back = True

        return _Savepoint()

    @property
    def attendance(self):
        return [o for o in self.added if isinstance(o, EventAttendance)]

    @property
    def audits(self):
        return [o for o in self.added if isinstance(o, AuditLog)]


def _csv(text: str) -> bytes:
    return text.strip().encode("utf-8")


@pytest.mark.anyio
async def test_the_index_is_built_in_at_most_three_queries_never_one_per_row():
    body = (
        "First name,Last name,Email\n"
        + "".join(f"Person{i},Surname{i},p{i}@x.com\n" for i in range(50))
        + "".join(f"NoMail{i},Surname{i},\n" for i in range(50))
    )
    rows, _errors, _ignored = attendee_match.parse_and_map(_csv(body))
    session = _FriendsSession()
    await friend_identity.build_friend_index(session, 7, rows)
    assert len(session.queries) == 3


@pytest.mark.anyio
async def test_the_email_lookup_is_skipped_when_no_row_has_an_email():
    rows, _errors, _ignored = attendee_match.parse_and_map(_csv("First name,Last name\nJane,Doe\n"))
    session = _FriendsSession()
    await friend_identity.build_friend_index(session, 7, rows)
    assert len(session.queries) == 2
    assert not any("personal_email" in q for q in session.queries)


@pytest.mark.anyio
async def test_the_email_lookup_reads_both_email_columns_and_skips_archived_rows():
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name,Email\nJane,Doe,jane@x.com\n")
    )
    session = _FriendsSession()
    await friend_identity.build_friend_index(session, 7, rows)
    email_sql = next(q for q in session.queries if "personal_email" in q)
    assert "work_email" in email_sql
    assert "archived IS false" in email_sql or "archived IS 0" in email_sql
    # NOT restricted to friends: alumni must come back too, so they can be
    # refused rather than silently ignored.
    assert "is_alumni IS false" not in email_sql


@pytest.mark.anyio
async def test_the_name_lookup_is_restricted_to_live_friends():
    rows, _errors, _ignored = attendee_match.parse_and_map(_csv("First name,Last name\nJane,Doe\n"))
    session = _FriendsSession()
    await friend_identity.build_friend_index(session, 7, rows)
    name_sql = session.queries[-1]
    assert "is_alumni IS false" in name_sql
    assert "archived IS false" in name_sql


@pytest.mark.anyio
async def test_email_matching_is_case_and_whitespace_insensitive_on_both_sides():
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name,Email\nJane,Doe,  Jane@X.COM \n")
    )
    assert rows[0]["emails"] == ["jane@x.com"]
    session = _FriendsSession(
        # (alumni_id, is_alumni, personal_email, work_email)
        email_hits=[(42, False, " JANE@x.com", None)]
    )
    index = await friend_identity.build_friend_index(session, 7, rows)
    assert index.friends_by_email == {"jane@x.com": [42]}
    assert index.decide(rows[0]).alumni_id == 42


@pytest.mark.anyio
async def test_email_hits_are_partitioned_into_alumni_and_friends():
    rows, _errors, _ignored = attendee_match.parse_and_map(
        _csv("First name,Last name,Email\nJane,Doe,jane@x.com\n")
    )
    session = _FriendsSession(
        email_hits=[(5, True, None, "jane@x.com"), (42, False, "jane@x.com", None)]
    )
    index = await friend_identity.build_friend_index(session, 7, rows)
    assert index.alumni_by_email == {"jane@x.com": [5]}
    assert index.friends_by_email == {"jane@x.com": [42]}


# --- The route ---------------------------------------------------------------


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles) or ["full_access"],
    )


@pytest.fixture
def friends_client():
    def _make(session):
        async def _dep():
            yield session

        app.dependency_overrides[get_session] = _dep
        app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def _post(client, body: str, rows: str = "2"):
    return client.post(
        "/events/7/attendees/match/friends",
        files={"file": ("a.csv", _csv(body))},
        data={"rows": rows},
    )


def test_a_friend_from_another_event_is_linked_not_created(friends_client):
    """Second event, same email: the existing friend is attached to THIS event
    and reported as reused with her visible id. No new row, no savepoint."""
    session = _FriendsSession(email_hits=[(42, False, "jane@x.com", None)])
    with friends_client(session) as client:
        response = _post(client, "First name,Last name,Company,Email\nJane,Doe,New Co,JANE@x.com")
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 0
    assert body["reused"] == 1
    assert body["attached"] == 1
    assert body["existing_alumni"] == 0
    item = body["items"][0]
    assert item["status"] == "reused"
    assert item["alumni_id"] == 42
    assert item["friend_id"] == "FRIEND-00042"
    assert item["is_existing_alumnus"] is False
    assert "FRIEND-00042" in item["message"]
    assert session.savepoints == 0
    assert [a.alumni_id for a in session.attendance] == [42]
    assert session.attendance[0].event_id == 7
    assert any("FRIEND-00042" in a.new_value for a in session.audits)
    assert session.committed == 1


def test_a_row_with_no_email_reuses_the_name_and_employer_twin(friends_client):
    session = _FriendsSession(
        # (alumni_id, first_name, preferred_first_name, last_name, employer)
        name_hits=[(9, "Jane", None, "Doe", "Byrne Capital")]
    )
    with friends_client(session) as client:
        response = _post(client, "First name,Last name,Company\nJane,Doe,Byrne Capital LLC")
    body = response.json()
    assert body["reused"] == 1
    assert body["items"][0]["friend_id"] == "FRIEND-00009"
    assert "name" in body["items"][0]["message"]
    assert [a.alumni_id for a in session.attendance] == [9]


def test_an_alumnus_email_is_refused_not_created_and_nothing_is_written(friends_client):
    session = _FriendsSession(email_hits=[(5, True, "jane@x.com", None)])
    with friends_client(session) as client:
        response = _post(client, "First name,Last name,Email\nJane,Doe,jane@x.com")
    body = response.json()
    assert body["created"] == 0
    assert body["reused"] == 0
    assert body["attached"] == 0
    assert body["existing_alumni"] == 1
    item = body["items"][0]
    assert item["status"] == "existing_alumnus"
    assert item["is_existing_alumnus"] is True
    assert item["alumni_id"] == 5
    assert item["friend_id"] is None
    assert "match" in item["message"].lower()
    # Nothing is created or attached -- but telling the reviewer whose record
    # that email belongs to is a disclosure, and it is recorded as one: an
    # audit row naming the alumnus (never the email), committed on its own.
    assert [type(o).__name__ for o in session.added] == ["AuditLog"]
    audit = session.added[0]
    assert audit.action_type == "attendee_friend_existing_alumnus"
    assert audit.entity_type == "event"
    assert "alumni 5" in audit.new_value
    assert "jane@x.com" not in audit.new_value
    assert session.savepoints == 0
    assert session.committed == 1


def test_reposting_the_same_file_is_idempotent(friends_client):
    """Friend 42 was created from this file last time and is on the roster;
    the re-post finds her by email, sees she already attends, and skips."""
    session = _FriendsSession(
        roster=[(42, "Jane", None, "Doe", "Byrne Capital")],
        email_hits=[(42, False, "jane@x.com", None)],
    )
    with friends_client(session) as client:
        response = _post(
            client, "First name,Last name,Company,Email\nJane,Doe,Byrne Capital,jane@x.com"
        )
    body = response.json()
    assert body["created"] == 0
    assert body["reused"] == 0
    assert body["skipped"] == 1
    assert body["items"][0]["status"] == "skipped"
    assert session.added == []
    assert session.committed == 0


def test_reposting_with_a_changed_name_still_does_not_double_attach(friends_client):
    """The name key misses (the file was tidied), the email hits, and the
    friend is already attending -> skipped with her id, not a second row."""
    session = _FriendsSession(
        roster=[(42, "Jane", None, "Doe", "Byrne Capital")],
        email_hits=[(42, False, "jane@x.com", None)],
    )
    with friends_client(session) as client:
        response = _post(
            client, "First name,Last name,Company,Email\nJanet,Doe,Byrne Capital,jane@x.com"
        )
    body = response.json()
    assert body["skipped"] == 1
    assert body["items"][0]["friend_id"] == "FRIEND-00042"
    assert "FRIEND-00042" in body["items"][0]["message"]
    assert session.attendance == []


def test_the_roster_name_guard_still_holds_for_rows_without_an_email(friends_client):
    """The pre-#538 behaviour, kept: someone with this name + employer already
    on the event's roster is skipped, whoever they are."""
    session = _FriendsSession(roster=[(3, "Jane", None, "Doe", "Byrne Capital")])
    with friends_client(session) as client:
        response = _post(client, "First name,Last name,Company\nJane,Doe,Byrne Capital LLC")
    body = response.json()
    assert body["skipped"] == 1
    assert body["items"][0]["status"] == "skipped"
    assert body["items"][0]["friend_id"] is None
    assert session.savepoints == 0


def test_a_created_friend_reports_its_visible_id(friends_client, monkeypatch):
    created: list[dict] = []

    async def _fake_create(session, model, *, actor_user_id):
        created.append(model.model_dump(exclude_none=True))
        return SimpleNamespace(alumni_id=77)

    monkeypatch.setattr(alumni_service, "create_alumni", _fake_create)
    session = _FriendsSession()
    with friends_client(session) as client:
        response = _post(client, "First name,Last name,Email\nJane,Doe,jane@x.com")
    body = response.json()
    assert body["created"] == 1
    assert body["attached"] == 1
    item = body["items"][0]
    assert item["status"] == "created"
    assert item["alumni_id"] == 77
    assert item["friend_id"] == "FRIEND-00077"
    assert created[0]["is_alumni"] is False
    assert [a.alumni_id for a in session.attendance] == [77]


def test_two_rows_for_the_same_email_in_one_file_create_one_friend(friends_client, monkeypatch):
    calls = 0

    async def _fake_create(session, model, *, actor_user_id):
        nonlocal calls
        calls += 1
        return SimpleNamespace(alumni_id=77)

    monkeypatch.setattr(alumni_service, "create_alumni", _fake_create)
    session = _FriendsSession()
    with friends_client(session) as client:
        response = _post(
            client,
            "First name,Last name,Email\nJane,Doe,jane@x.com\nJanet,Doe,JANE@X.COM",
            rows="2,3",
        )
    body = response.json()
    assert calls == 1
    assert body["created"] == 1
    assert body["skipped"] == 1
    assert body["items"][1]["friend_id"] == "FRIEND-00077"
    assert len(session.attendance) == 1


def test_mixed_outcomes_are_counted_separately(friends_client, monkeypatch):
    async def _fake_create(session, model, *, actor_user_id):
        return SimpleNamespace(alumni_id=77)

    monkeypatch.setattr(alumni_service, "create_alumni", _fake_create)
    session = _FriendsSession(
        email_hits=[(5, True, "alum@x.com", None), (42, False, None, "old@x.com")]
    )
    with friends_client(session) as client:
        response = _post(
            client,
            "First name,Last name,Email\n"
            "Al,Umnus,alum@x.com\n"
            "Old,Friend,old@x.com\n"
            "New,Friend,new@x.com",
            rows="2,3,4",
        )
    body = response.json()
    assert (body["existing_alumni"], body["reused"], body["created"]) == (1, 1, 1)
    assert body["attached"] == 2
    statuses = [i["status"] for i in body["items"]]
    assert statuses == ["existing_alumnus", "reused", "created"]
    assert sorted(a.alumni_id for a in session.attendance) == [42, 77]

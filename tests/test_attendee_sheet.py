"""Event attendee spreadsheet import (api#693).

Covers the issue's "done when" list: a missing Net ID column, extra columns,
the name + year match, Fin vs non-Fin, and no writes before confirm, plus the
confirm (commit) leg and its guards. Offline: the session is a stub.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.event import EventAttendance
from app.schemas.auth import UserContext
from app.services import alumni as alumni_service
from app.services import attendee_sheet

HEADER = "Fname,Lname,Company,Role,Email,Focus,UG,UG YR,LinkedIn,Grad,GradYR,Net ID"


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _csv(*lines: str) -> bytes:
    return ("\n".join(lines) + "\n").encode()


def _record(alumni_id: int, **kw):
    base = dict(
        alumni_id=alumni_id,
        first_name="Jane",
        preferred_first_name=None,
        last_name="Doe",
        graduation_year=None,
        graduate_graduation_year=None,
        is_alumni=True,
        current_employer=None,
        archived=False,
        net_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


# --- parsing -----------------------------------------------------------------


def test_meeting_headers_parse_every_field():
    rows, errors, ignored = attendee_sheet.parse_sheet(
        _csv(
            HEADER,
            "Jane,Doe,Goldman,Analyst,Jane@X.org,Fin,BYU,2019,"
            "linkedin.com/in/jd,MBA,2024, JDoe19 ",
        )
    )
    assert errors == [] and ignored == []
    r = rows[0]
    assert r["row"] == 2
    assert (r["first_name"], r["last_name"]) == ("Jane", "Doe")
    assert (r["company"], r["role"]) == ("Goldman", "Analyst")
    assert r["email"] == "jane@x.org"
    assert (r["ug_year"], r["grad_year"]) == (2019, 2024)
    assert r["net_id"] == "jdoe19"
    # A scheme-less LinkedIn gets https:// so the alumni schema accepts it.
    assert r["linkedin"] == "https://linkedin.com/in/jd"


def test_missing_net_id_column_is_fine():
    rows, errors, _ = attendee_sheet.parse_sheet(
        _csv("Fname,Lname,UG YR", "Jane,Doe,2019")
    )
    assert errors == []
    assert rows[0]["net_id"] is None
    assert rows[0]["ug_year"] == 2019


def test_blank_net_id_cell_is_fine():
    rows, errors, _ = attendee_sheet.parse_sheet(
        _csv(HEADER, "Jane,Doe,,,,,,2019,,,,")
    )
    assert errors == []
    assert rows[0]["net_id"] is None
    assert rows[0]["warnings"] == []


def test_extra_columns_are_ignored_not_an_error():
    rows, errors, ignored = attendee_sheet.parse_sheet(
        _csv("Fname,Shirt size,Lname,Dietary needs", "Jane,M,Doe,None")
    )
    assert errors == []
    assert ignored == ["Shirt size", "Dietary needs"]
    assert (rows[0]["first_name"], rows[0]["last_name"]) == ("Jane", "Doe")


def test_name_columns_are_required():
    _, errors, _ = attendee_sheet.parse_sheet(_csv("Fname,Company", "Jane,Acme"))
    assert errors == ["Missing required column(s): Lname."]


def test_duplicate_column_is_rejected():
    _, errors, _ = attendee_sheet.parse_sheet(_csv("Fname,Lname,Email,E-mail", "a,b,c,d"))
    assert errors and "Duplicate column" in errors[0]


def test_a_bad_year_warns_and_is_dropped():
    rows, errors, _ = attendee_sheet.parse_sheet(
        _csv("Fname,Lname,UG YR,GradYR", "Jane,Doe,'19,2019.0")
    )
    assert errors == []
    assert rows[0]["ug_year"] is None
    assert rows[0]["grad_year"] == 2019  # Excel's 2019.0 is still 2019
    assert any("UG YR" in w for w in rows[0]["warnings"])


def test_row_limit_is_enforced(monkeypatch):
    _, errors, _ = attendee_sheet.parse_sheet(
        _csv("Fname,Lname", "a,b", "c,d", "e,f"), max_rows=2
    )
    assert errors and "row limit" in errors[0]


@pytest.mark.parametrize(
    ("focus", "grad", "expected"),
    [
        ("Fin", None, True),
        ("FIN", None, True),
        ("Finance", None, True),
        (None, "MBA - Finance", True),
        (None, "Fin", True),
        ("Marketing", "MBA", False),
        ("Financial Planning", None, False),
        (None, None, False),
    ],
)
def test_fin_vs_non_fin(focus, grad, expected):
    assert attendee_sheet.is_finance(focus, grad) is expected
    kind = attendee_sheet.default_kind({"focus": focus, "grad": grad})
    assert kind == ("new_alumni" if expected else "new_friend")


# --- preview (service) -------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def scalars(self):
        return self


class _PreviewSession:
    """Answers the candidate query and the roster query; records any add."""

    def __init__(self, candidates=(), roster=(), event=None):
        self._candidates = list(candidates)
        self._roster = list(roster)
        self._event = event or SimpleNamespace(event_id=7, event_name="Conf")
        self.added: list = []
        self.commits = 0
        self.queries = 0

    async def get(self, _model, _pk):
        return self._event

    async def execute(self, stmt):
        self.queries += 1
        sql = str(stmt.compile())
        if sql.lstrip().startswith("SELECT event_attendance.alumni_id"):
            return _Result(self._roster)
        return _Result(self._candidates)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _rows(*lines):
    rows, errors, _ = attendee_sheet.parse_sheet(_csv(HEADER, *lines))
    assert errors == []
    return rows


@pytest.mark.anyio
async def test_name_plus_ug_year_matches():
    session = _PreviewSession([_record(42, graduation_year=2019)])
    report = await attendee_sheet.preview(
        session, 7, _rows("jane,DOE,,,,,,2019,,,,")
    )
    row = report["rows"][0]
    assert row["status"] == "matched"
    assert row["action"] == "match"
    assert row["alumni_id"] == 42
    assert report["summary"]["matched"] == 1


@pytest.mark.anyio
async def test_grad_year_matches_the_graduate_year_too():
    session = _PreviewSession([_record(42, graduate_graduation_year=2024)])
    report = await attendee_sheet.preview(
        session, 7, _rows("Jane,Doe,,,,,,,,MBA,2024,")
    )
    assert report["rows"][0]["alumni_id"] == 42


@pytest.mark.anyio
async def test_preferred_first_name_matches():
    session = _PreviewSession(
        [_record(42, first_name="Margaret", preferred_first_name="Peggy", graduation_year=2010)]
    )
    report = await attendee_sheet.preview(session, 7, _rows("Peggy,Doe,,,,,,2010,,,,"))
    assert report["rows"][0]["status"] == "matched"


@pytest.mark.anyio
async def test_same_name_wrong_year_is_not_a_match():
    session = _PreviewSession([_record(42, graduation_year=2015)])
    report = await attendee_sheet.preview(
        session, 7, _rows("Jane,Doe,,,,Fin,,2019,,,,")
    )
    row = report["rows"][0]
    assert row["status"] == "new_alumni"
    assert row["alumni_id"] is None and row["candidates"] == []


@pytest.mark.anyio
async def test_unmatched_non_fin_row_becomes_a_friend():
    session = _PreviewSession([])
    report = await attendee_sheet.preview(
        session, 7, _rows("Sam,Lee,Acme,Recruiter,,Marketing,,,,,,")
    )
    assert report["rows"][0]["status"] == "new_friend"
    assert report["summary"]["new_friend"] == 1


@pytest.mark.anyio
async def test_two_records_are_ambiguous_never_a_silent_pick():
    session = _PreviewSession(
        [_record(42, graduation_year=2019), _record(43, graduate_graduation_year=2019)]
    )
    report = await attendee_sheet.preview(session, 7, _rows("Jane,Doe,,,,,,2019,,,,"))
    row = report["rows"][0]
    assert row["status"] == "ambiguous"
    assert row["alumni_id"] is None
    assert {c["alumni_id"] for c in row["candidates"]} == {42, 43}


@pytest.mark.anyio
async def test_an_existing_yearless_friend_is_reused():
    session = _PreviewSession([_record(60, first_name="Sam", last_name="Lee", is_alumni=False)])
    report = await attendee_sheet.preview(session, 7, _rows("Sam,Lee,,,,,,,,,,"))
    row = report["rows"][0]
    assert row["status"] == "matched"
    assert row["candidates"][0]["friend_id"] == "FRIEND-00060"


@pytest.mark.anyio
async def test_a_yearless_ALUM_needs_the_year():
    session = _PreviewSession([_record(42)])
    report = await attendee_sheet.preview(session, 7, _rows("Jane,Doe,,,,,,2019,,,,"))
    assert report["rows"][0]["status"] == "new_friend"


@pytest.mark.anyio
async def test_already_on_the_roster_defaults_to_skip():
    session = _PreviewSession([_record(42, graduation_year=2019)], roster=[42])
    report = await attendee_sheet.preview(session, 7, _rows("Jane,Doe,,,,,,2019,,,,"))
    row = report["rows"][0]
    assert (row["status"], row["action"]) == ("already_attending", "skip")


@pytest.mark.anyio
async def test_preview_writes_nothing_and_is_batched():
    session = _PreviewSession([_record(42, graduation_year=2019)])
    await attendee_sheet.preview(
        session,
        7,
        _rows(*[f"Person{i},Doe,,,,Fin,,2019,,,," for i in range(50)]),
    )
    assert session.added == [] and session.commits == 0
    assert session.queries == 2  # candidates + roster, never one per row


# --- routes ------------------------------------------------------------------


@pytest.fixture
def make_client():
    def _make(session, role="full_access"):
        async def _dep():
            yield session

        app.dependency_overrides[get_session] = _dep
        app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
        return TestClient(app)

    yield _make
    app.dependency_overrides.clear()


def test_preview_route_writes_no_alumni_or_attendance(make_client):
    session = _PreviewSession([_record(42, graduation_year=2019)])
    client = make_client(session)
    response = client.post(
        "/events/7/attendees/sheet/preview",
        files={
            "file": (
                "a.csv",
                _csv(HEADER + ",T-shirt", "Jane,Doe,,,,,,2019,,,,,L", "Sam,Lee,,,,Fin,,,,,,,M"),
                "text/csv",
            )
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ignored_columns"] == ["T-shirt"]
    assert [r["status"] for r in body["rows"]] == ["matched", "new_alumni"]
    # The ONLY row written is the disclosure audit -- no alumni, no attendance.
    assert all(isinstance(o, AuditLog) for o in session.added)
    assert [o.action_type for o in session.added] == ["attendee_sheet_preview"]
    assert "alumni_ids=42" in session.added[0].new_value
    assert "Jane" not in session.added[0].new_value  # counts + ids, no names


def test_preview_route_reports_header_errors(make_client):
    client = make_client(_PreviewSession())
    response = client.post(
        "/events/7/attendees/sheet/preview",
        files={"file": ("a.csv", _csv("Company", "Acme"), "text/csv")},
    )
    assert response.status_code == 200
    assert response.json()["columns_ok"] is False


def test_preview_route_413s_an_oversized_file(make_client, monkeypatch):
    monkeypatch.setattr("app.services.attendee_match.MAX_UPLOAD_BYTES", 10)
    client = make_client(_PreviewSession())
    response = client.post(
        "/events/7/attendees/sheet/preview",
        files={"file": ("a.csv", b"Fname,Lname\nJane,Doe\n", "text/csv")},
    )
    assert response.status_code == 413


@pytest.mark.parametrize("role", ["view_only", "student"])
@pytest.mark.parametrize(
    ("path", "kwargs"),
    [
        ("/events/7/attendees/sheet/preview", {"files": {"file": ("a.csv", b"Fname,Lname\n")}}),
        ("/events/7/attendees/sheet/commit", {"json": {"rows": []}}),
    ],
)
def test_every_leg_is_refused_below_full_access(make_client, role, path, kwargs):
    client = make_client(_PreviewSession(), role=role)
    assert client.post(path, **kwargs).status_code == 403


def test_template_downloads(make_client):
    client = make_client(_PreviewSession())
    response = client.get("/events/attendees/sheet/template")
    assert response.status_code == 200
    assert response.text.splitlines()[0] == HEADER


class _SavePoint:
    def __init__(self):
        self.rolled_back = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def rollback(self):
        self.rolled_back = True


class _CommitSession(_PreviewSession):
    def __init__(self, records=(), roster=()):
        super().__init__(roster=roster)
        self._records = list(records)

    async def execute(self, stmt):
        sql = str(stmt.compile())
        if sql.lstrip().startswith("SELECT event_attendance.alumni_id"):
            return _Result(self._roster)
        return _Result(self._records)

    def begin_nested(self):
        return _SavePoint()

    async def flush(self):
        pass

    async def refresh(self, _obj):
        pass


@pytest.fixture
def fake_create(monkeypatch):
    created: list = []

    async def _create(session, model, actor_user_id=None):
        created.append(model)
        return SimpleNamespace(alumni_id=900 + len(created))

    monkeypatch.setattr(alumni_service, "create_alumni", _create)
    return created


def _attendance(session):
    return [o for o in session.added if isinstance(o, EventAttendance)]


def test_commit_records_match_creates_alum_and_friend(make_client, fake_create):
    session = _CommitSession(records=[_record(42, graduation_year=2019)])
    client = make_client(session)
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={
            "rows": [
                {"row": 2, "action": "match", "alumni_id": 42},
                {
                    "row": 3,
                    "action": "new_alumni",
                    # Edited on the confirm screen: the edit is what is saved.
                    "first_name": "Janet",
                    "last_name": "Roe",
                    "company": "Goldman",
                    "role": "Analyst",
                    "email": "janet@x.org",
                    "linkedin": "https://www.linkedin.com/in/janet",
                    "ug_year": 2019,
                    "grad_year": 2024,
                    "net_id": "JRoe19",
                },
                {"row": 4, "action": "new_friend", "first_name": "Sam", "last_name": "Lee"},
                {"row": 5, "action": "skip"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["added"], body["created_alumni"], body["created_friends"], body["skipped"]) == (
        1,
        1,
        1,
        1,
    )
    assert [i["status"] for i in body["items"]] == [
        "added",
        "created_alumni",
        "created_friend",
        "skipped",
    ]
    alum, friend = fake_create
    assert alum.is_alumni is True
    assert (alum.first_name, alum.last_name, alum.net_id) == ("Janet", "Roe", "jroe19")
    assert (alum.graduation_year, alum.graduate_graduation_year) == (2019, 2024)
    assert alum.linkedin_url == "https://www.linkedin.com/in/janet"
    assert alum.contact.personal_email == "janet@x.org"
    assert (alum.career.current_employer, alum.career.current_title) == ("Goldman", "Analyst")
    assert friend.is_alumni is False
    assert body["items"][2]["friend_id"] == "FRIEND-00902"
    assert {a.alumni_id for a in _attendance(session)} == {42, 901, 902}
    assert session.commits == 1


def test_commit_never_double_adds_or_trusts_a_stale_id(make_client, fake_create):
    session = _CommitSession(
        records=[_record(42), _record(43, archived=True)], roster=[42]
    )
    client = make_client(session)
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={
            "rows": [
                {"row": 2, "action": "match", "alumni_id": 42},
                {"row": 3, "action": "match", "alumni_id": 43},
                {"row": 4, "action": "match", "alumni_id": 44},
            ]
        },
    )
    body = response.json()
    assert [i["status"] for i in body["items"]] == [
        "already_attending",
        "not_found",
        "not_found",
    ]
    assert _attendance(session) == []
    assert session.commits == 0


def test_the_same_new_person_twice_is_created_once(make_client, fake_create):
    session = _CommitSession()
    client = make_client(session)
    row = {"action": "new_friend", "first_name": "Sam", "last_name": "Lee"}
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={"rows": [{"row": 2, **row}, {"row": 3, **row}]},
    )
    assert response.json()["created_friends"] == 1
    assert len(fake_create) == 1


def test_a_bad_linkedin_is_dropped_not_fatal(make_client, fake_create):
    session = _CommitSession()
    client = make_client(session)
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={
            "rows": [
                {
                    "row": 2,
                    "action": "new_friend",
                    "first_name": "Sam",
                    "last_name": "Lee",
                    "linkedin": "https://evil.example/in/sam",
                }
            ]
        },
    )
    item = response.json()["items"][0]
    assert item["status"] == "created_friend"
    assert "LinkedIn" in item["message"]
    assert fake_create[0].linkedin_url is None


def test_a_refused_create_is_rejected_and_the_rest_still_save(make_client, monkeypatch):
    from app.core.errors import ConflictError

    calls = []

    async def _create(session, model, actor_user_id=None):
        calls.append(model)
        if model.last_name == "Dup":
            raise ConflictError("An alumni record with this Net ID already exists.")
        return SimpleNamespace(alumni_id=950)

    monkeypatch.setattr(alumni_service, "create_alumni", _create)
    session = _CommitSession()
    client = make_client(session)
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={
            "rows": [
                {"row": 2, "action": "new_alumni", "first_name": "A", "last_name": "Dup"},
                {"row": 3, "action": "new_alumni", "first_name": "B", "last_name": "Ok"},
            ]
        },
    )
    body = response.json()
    assert [i["status"] for i in body["items"]] == ["rejected", "created_alumni"]
    assert "already exists" in body["items"][0]["message"]
    assert [a.alumni_id for a in _attendance(session)] == [950]


def test_an_unpicked_ambiguous_row_is_a_422(make_client):
    client = make_client(_CommitSession())
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={"rows": [{"row": 2, "action": "match"}]},
    )
    assert response.status_code == 422


def test_commit_rejects_unknown_keys(make_client):
    client = make_client(_CommitSession())
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={"rows": [{"row": 2, "action": "skip", "is_admin": True}]},
    )
    assert response.status_code == 422


def test_new_record_cap(make_client, monkeypatch):
    monkeypatch.setattr(attendee_sheet, "MAX_NEW_RECORDS_PER_COMMIT", 1)
    client = make_client(_CommitSession())
    row = {"action": "new_friend", "first_name": "S", "last_name": "L"}
    response = client.post(
        "/events/7/attendees/sheet/commit",
        json={"rows": [{"row": 2, **row}, {"row": 3, **row, "first_name": "T"}]},
    )
    assert response.status_code == 422


def test_commit_404s_for_an_unknown_event(make_client):
    session = _CommitSession()
    session._event = None
    client = make_client(session)
    response = client.post(
        "/events/7/attendees/sheet/commit", json={"rows": []}
    )
    assert response.status_code == 404


def test_alumni_model_has_is_alumni():
    # Guard the field the friend/alum split relies on.
    assert hasattr(Alumni, "is_alumni")

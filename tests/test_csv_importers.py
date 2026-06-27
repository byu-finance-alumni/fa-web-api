"""Tests for the events (#156) and donations (#161) bulk CSV importers.

The ``parse_and_map`` stage is pure (no DB), so header validation, grouping, and
per-cell coercion are tested directly. The ``evaluate`` stage (Net-ID matching,
unmatched-row rejection) is exercised with a sequenced fake session. Route-level
auth gating for the new endpoints is covered too.
"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import import_donations, import_events


def _bytes(text: str) -> bytes:
    return text.encode("utf-8")


# --- events: parse_and_map ----------------------------------------------------


def test_events_parse_groups_and_validates():
    csv_text = (
        "Event title,Event date (YYYY-MM-DD),Attendee Net ID,Attendee name\n"
        "Banquet,2026-04-15,jdoe,Jane Doe\n"
        "Banquet,2026-04-15,msmith,Mark Smith\n"
        "Trek,not-a-date,alee,Amy Lee\n"
    )
    rows, header_errors = import_events.parse_and_map(_bytes(csv_text))
    assert header_errors == []
    assert len(rows) == 3
    # First two rows share an event group identity.
    assert rows[0]["event_title"] == "Banquet"
    assert rows[0]["event_date"] == "2026-04-15"
    assert rows[0]["error"] is None
    # Bad date is flagged on the row.
    assert rows[2]["error"] is not None
    assert "date" in rows[2]["error"].lower()


def test_events_parse_rejects_bad_headers():
    rows, header_errors = import_events.parse_and_map(_bytes("a,b,c\n1,2,3\n"))
    assert rows == []
    assert any("Missing required column" in e for e in header_errors)


def test_events_parse_flags_missing_title():
    csv_text = (
        "Event title,Event date (YYYY-MM-DD),Attendee Net ID,Attendee name\n"
        ",2026-04-15,jdoe,Jane Doe\n"
    )
    rows, _ = import_events.parse_and_map(_bytes(csv_text))
    assert rows[0]["error"] is not None
    assert "title" in rows[0]["error"].lower()


def test_events_template_has_headers_and_examples():
    csv_text = import_events.build_template_csv()
    assert "Event title" in csv_text
    assert "Attendee Net ID" in csv_text
    # Two example rows share an event to demonstrate grouping.
    assert csv_text.count("Spring Finance Banquet") == 2


# --- events: evaluate (Net-ID matching, group rejection) ----------------------


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _SeqSession:
    """Returns queued ``execute().all()`` results in call order."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        return _Result(self._results.pop(0) if self._results else [])


def test_events_evaluate_rejects_group_with_unmatched_net_id():
    rows, _ = import_events.parse_and_map(
        _bytes(
            "Event title,Event date (YYYY-MM-DD),Attendee Net ID,Attendee name\n"
            "Banquet,2026-04-15,jdoe,Jane Doe\n"
            "Banquet,2026-04-15,ghost,Nobody\n"
        )
    )
    # match_net_ids -> only jdoe is active; _load_existing_events -> none.
    session = _SeqSession(results=[[(42, "jdoe")], []])
    report = asyncio.run(import_events.evaluate(session, rows))
    assert report["summary"]["events"] == 1
    assert report["summary"]["importable_events"] == 0
    event = report["events"][0]
    assert event["status"] == "rejected"
    assert any(b["code"] == "unmatched_net_id" for b in event["blockers"])


def test_events_evaluate_accepts_fully_matched_group():
    rows, _ = import_events.parse_and_map(
        _bytes(
            "Event title,Event date (YYYY-MM-DD),Attendee Net ID,Attendee name\n"
            "Banquet,2026-04-15,jdoe,Jane Doe\n"
        )
    )
    session = _SeqSession(results=[[(42, "jdoe")], []])
    report = asyncio.run(import_events.evaluate(session, rows))
    assert report["summary"]["importable_events"] == 1
    assert report["events"][0]["attendees"][0]["alumni_id"] == 42


# --- donations: parse_and_map -------------------------------------------------


def test_donations_parse_valid_and_invalid_rows():
    csv_text = (
        "Net ID,Name,Month,Year,Amount\n"
        'jdoe,Jane Doe,4,2026,"$1,250.00"\n'  # money with $ and comma (Excel quotes it)
        "msmith,Mark Smith,13,2025,100\n"      # bad month
        "alee,Amy Lee,,2024,abc\n"             # bad amount
        "bro,Bo Roe,5,,50\n"                   # missing year
    )
    rows, header_errors = import_donations.parse_and_map(_bytes(csv_text))
    assert header_errors == []
    assert rows[0]["error"] is None
    assert str(rows[0]["amount"]) == "1250.00"
    assert rows[0]["month"] == 4
    assert "Month" in rows[1]["error"]
    assert "Amount" in rows[2]["error"]
    assert "Year" in rows[3]["error"]


def test_donations_parse_rejects_bad_headers():
    rows, header_errors = import_donations.parse_and_map(_bytes("x,y\n1,2\n"))
    assert rows == []
    assert any("Missing required column" in e for e in header_errors)


def test_donations_evaluate_rejects_unmatched_net_id():
    rows, _ = import_donations.parse_and_map(
        _bytes("Net ID,Name,Month,Year,Amount\nghost,Nobody,4,2026,100\n")
    )
    session = _SeqSession(results=[[]])  # no active match for "ghost"
    report = asyncio.run(import_donations.evaluate(session, rows))
    assert report["summary"]["importable"] == 0
    assert report["rows"][0]["status"] == "rejected"
    assert any(b["code"] == "unmatched_net_id" for b in report["rows"][0]["blockers"])
    # Amount is echoed in the preview (super_admin-only endpoint).
    assert report["rows"][0]["amount"] == 100.0


# --- route auth gating for the new endpoints ----------------------------------


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def client():
    async def _no_db():
        yield None

    app.dependency_overrides[get_session] = _no_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_events_import_template_requires_full_access(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    assert client.get("/events/import/template").status_code == 403


def test_events_import_template_ok_for_full_access(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.get("/events/import/template")
    assert response.status_code == 200
    assert "Event title" in response.text


def test_delete_event_requires_auth(client):
    assert client.delete("/events/7").status_code == 401


def test_delete_event_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    assert client.delete("/events/7").status_code == 403


def test_delete_event_happy_path(client):
    from types import SimpleNamespace

    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")

    class _DelSession:
        def __init__(self):
            self.added = []
            self.committed = False
            self.deleted = []

        async def get(self, _model, _pk):
            return SimpleNamespace(event_id=7, event_name="Banquet")

        def add(self, obj):
            self.added.append(obj)

        async def delete(self, obj):
            self.deleted.append(obj)

        async def commit(self):
            self.committed = True

    session = _DelSession()

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    response = client.delete("/events/7")
    assert response.status_code == 200
    assert response.json() == {"event_id": 7, "deleted": True}
    assert session.committed is True
    audits = [o for o in session.added if hasattr(o, "action_type")]
    assert audits[0].action_type == "delete"
    assert audits[0].entity_type == "event"

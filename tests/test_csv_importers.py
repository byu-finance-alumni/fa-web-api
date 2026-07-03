"""Tests for the events (#156) and donations (#161) bulk CSV importers.

The ``parse_and_map`` stage is pure (no DB), so header validation and per-cell
coercion are tested directly. The ``evaluate`` stage (donor / Net-ID matching,
ambiguity, unmatched reporting) is exercised with fake sessions. Route-level auth
gating for the new endpoints is covered too.
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


_EV_HEADER = "Net ID,Name\n"


def test_events_parse_attendee_rows():
    csv_text = _EV_HEADER + "jdoe,Jane Doe\nmsmith,Mark Smith\n"
    rows, header_errors = import_events.parse_and_map(_bytes(csv_text))
    assert header_errors == []
    assert len(rows) == 2
    assert rows[0]["net_id"] == "jdoe"
    assert rows[0]["attendee_name"] == "Jane Doe"
    assert rows[0]["error"] is None


def test_events_parse_rejects_bad_headers():
    rows, header_errors = import_events.parse_and_map(_bytes("a,b,c\n1,2,3\n"))
    assert rows == []
    assert any("Missing required column" in e for e in header_errors)


def test_events_parse_flags_missing_net_id():
    rows, _ = import_events.parse_and_map(_bytes(_EV_HEADER + ",Jane Doe\n"))
    assert rows[0]["error"] is not None
    assert "net id" in rows[0]["error"].lower()


def test_events_template_has_headers_and_examples():
    csv_text = import_events.build_template_csv()
    assert "Net ID" in csv_text
    assert "jdoe" in csv_text


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


class _EventEvalSession:
    """Fake session for the one-event evaluate: ``scalar`` answers the
    "does this event already exist?" probe; ``execute`` returns the Net-ID match
    rows for ``match_net_ids``."""

    def __init__(self, matched_rows, existing=None):
        self._matched = matched_rows
        self._existing = existing

    async def scalar(self, _stmt):
        return self._existing

    async def execute(self, _stmt):
        return _Result(self._matched)


def test_events_evaluate_reports_unmatched_without_blocking():
    rows, _ = import_events.parse_and_map(
        _bytes(_EV_HEADER + "jdoe,Jane Doe\nghost,Nobody\n")
    )
    meta = import_events.normalize_event_meta("Banquet", "2026-04-15")
    session = _EventEvalSession(matched_rows=[(42, "jdoe")])
    report = asyncio.run(import_events.evaluate(session, rows, meta))
    # Event is importable; the unmatched attendee is reported, not blocking.
    assert report["importable"] is True
    assert report["summary"]["attendees_matched"] == 1
    assert report["summary"]["attendees_unmatched"] == 1
    ghost = next(a for a in report["attendees"] if a["net_id"] == "ghost")
    assert ghost["matched"] is False


def test_events_evaluate_matches_attendees():
    rows, _ = import_events.parse_and_map(_bytes(_EV_HEADER + "jdoe,Jane Doe\n"))
    meta = import_events.normalize_event_meta("Banquet", "2026-04-15")
    session = _EventEvalSession(matched_rows=[(42, "jdoe")])
    report = asyncio.run(import_events.evaluate(session, rows, meta))
    assert report["importable"] is True
    assert report["attendees"][0]["alumni_id"] == 42


def test_events_evaluate_rejects_bad_event_date():
    rows, _ = import_events.parse_and_map(_bytes(_EV_HEADER + "jdoe,Jane Doe\n"))
    meta = import_events.normalize_event_meta("Banquet", "not-a-date")
    session = _EventEvalSession(matched_rows=[(42, "jdoe")])
    report = asyncio.run(import_events.evaluate(session, rows, meta))
    assert report["importable"] is False
    assert any(e["code"] == "invalid_event" for e in report["event_errors"])


def test_events_evaluate_rejects_duplicate_event():
    rows, _ = import_events.parse_and_map(_bytes(_EV_HEADER + "jdoe,Jane Doe\n"))
    meta = import_events.normalize_event_meta("Banquet", "2026-04-15")
    # scalar returns an existing event id -> duplicate.
    session = _EventEvalSession(matched_rows=[(42, "jdoe")], existing=7)
    report = asyncio.run(import_events.evaluate(session, rows, meta))
    assert report["importable"] is False
    assert any(e["code"] == "duplicate_event" for e in report["event_errors"])


# --- donations: parse_and_map -------------------------------------------------


_DON_HEADER = "MSTID,First name,Last name,Month,Year,Amount\n"


def test_donations_parse_valid_and_invalid_rows():
    csv_text = (
        _DON_HEADER
        + '100200300,Jane,Doe,4,2026,"$1,250.00"\n'  # money with $ + comma (quoted)
        + "100200301,Mark,Smith,13,2025,100\n"        # bad month
        + "100200302,Amy,Lee,,2024,abc\n"             # bad amount
        + "100200303,Bo,Roe,5,,50\n"                  # missing year
        + ",,,,2024,50\n"                             # nothing to match on
    )
    rows, header_errors = import_donations.parse_and_map(_bytes(csv_text))
    assert header_errors == []
    assert rows[0]["error"] is None
    assert str(rows[0]["amount"]) == "1250.00"
    assert rows[0]["month"] == 4
    assert rows[0]["mstid"] == "100200300"
    assert "Month" in rows[1]["error"]
    assert "Amount" in rows[2]["error"]
    assert "Year" in rows[3]["error"]
    assert "MSTID" in rows[4]["error"]  # no MSTID and no name -> nothing to match


def test_donations_parse_rejects_bad_headers():
    rows, header_errors = import_donations.parse_and_map(_bytes("x,y\n1,2\n"))
    assert rows == []
    assert any("Missing required column" in e for e in header_errors)


def test_donations_parse_rejects_zero_amount():
    rows, _ = import_donations.parse_and_map(
        _bytes(_DON_HEADER + "100200300,Jane,Doe,4,2026,0\n")
    )
    assert rows[0]["error"] is not None
    assert "Amount" in rows[0]["error"]


def test_donations_parse_allows_name_only_row():
    # No MSTID is fine as long as both names are present (name-fallback match).
    rows, _ = import_donations.parse_and_map(
        _bytes(_DON_HEADER + ",Jane,Doe,4,2026,100\n")
    )
    assert rows[0]["error"] is None
    assert rows[0]["mstid"] == ""
    assert (rows[0]["first_name"], rows[0]["last_name"]) == ("Jane", "Doe")


def test_donations_parse_flags_duplicate_header():
    # Two "MSTID" columns map ambiguously — reject rather than last-wins.
    rows, header_errors = import_donations.parse_and_map(
        _bytes("MSTID,MSTID,First name,Last name,Month,Year,Amount\n1,2,J,D,4,2026,50\n")
    )
    assert rows == []
    assert any("Duplicate column" in e for e in header_errors)


def test_donations_evaluate_matches_by_mstid():
    rows, _ = import_donations.parse_and_map(
        _bytes(_DON_HEADER + "100200300,Jane,Doe,4,2026,100\n")
    )
    # match_mstids -> alumnus 42; match_names result is irrelevant (MSTID wins).
    session = _SeqSession(results=[[(42, "100200300")]])
    report = asyncio.run(import_donations.evaluate(session, rows))
    assert report["summary"]["importable"] == 1
    assert report["rows"][0]["alumni_id"] == 42
    assert report["rows"][0]["match_method"] == "mstid"


def test_donations_evaluate_falls_back_to_name():
    rows, _ = import_donations.parse_and_map(
        _bytes(_DON_HEADER + ",Jane,Doe,4,2026,100\n")
    )
    # No MSTID -> match_mstids doesn't query; match_names -> alumnus 42.
    session = _SeqSession(results=[[(42, "Doe", "Jane")]])
    report = asyncio.run(import_donations.evaluate(session, rows))
    assert report["summary"]["importable"] == 1
    assert report["rows"][0]["alumni_id"] == 42
    assert report["rows"][0]["match_method"] == "name"


def test_donations_evaluate_rejects_ambiguous_name():
    rows, _ = import_donations.parse_and_map(
        _bytes(_DON_HEADER + ",Jane,Doe,4,2026,100\n")
    )
    # Two active alumni named Jane Doe -> ambiguous, never auto-attributed.
    session = _SeqSession(results=[[(42, "Doe", "Jane"), (43, "Doe", "Jane")]])
    report = asyncio.run(import_donations.evaluate(session, rows))
    assert report["summary"]["importable"] == 0
    assert report["rows"][0]["status"] == "rejected"
    assert any(
        b["code"] == "ambiguous_name" for b in report["rows"][0]["blockers"]
    )


def test_donations_evaluate_warns_duplicate_in_file():
    rows, _ = import_donations.parse_and_map(
        _bytes(
            _DON_HEADER
            + "100200300,Jane,Doe,4,2026,100\n"
            + "100200300,Jane,Doe,4,2026,100\n"
        )
    )
    session = _SeqSession(results=[[(42, "100200300")]])
    report = asyncio.run(import_donations.evaluate(session, rows))
    # Both rows are importable; the second carries a duplicate warning.
    assert report["summary"]["importable"] == 2
    assert any(
        w["code"] == "possible_duplicate_in_file"
        for w in report["rows"][1]["warnings"]
    )
    # alumni_id is propagated for commit reuse (no second match query).
    assert report["rows"][0]["alumni_id"] == 42


def test_donations_evaluate_rejects_unmatched_donor():
    rows, _ = import_donations.parse_and_map(
        _bytes(_DON_HEADER + "999999999,Ghost,Nobody,4,2026,100\n")
    )
    # No MSTID match and no name match.
    session = _SeqSession(results=[[], []])
    report = asyncio.run(import_donations.evaluate(session, rows))
    assert report["summary"]["importable"] == 0
    assert report["rows"][0]["status"] == "rejected"
    assert any(
        b["code"] == "unmatched_donor" for b in report["rows"][0]["blockers"]
    )
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
    assert "Net ID" in response.text


def test_events_import_preview_forbidden_for_view_only(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("view_only")
    response = client.post(
        "/events/import/preview",
        data={"event_name": "Banquet"},
        files={"file": ("a.csv", b"Net ID,Name\njdoe,Jane Doe\n", "text/csv")},
    )
    assert response.status_code == 403


def test_events_import_preview_requires_event_name(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    response = client.post(
        "/events/import/preview",
        files={"file": ("a.csv", b"Net ID,Name\njdoe,Jane Doe\n", "text/csv")},
    )
    assert response.status_code == 422


def test_events_import_preview_happy_path(client):
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")

    async def _override():
        yield _EventEvalSession(matched_rows=[(42, "jdoe")])

    app.dependency_overrides[get_session] = _override
    response = client.post(
        "/events/import/preview",
        data={"event_name": "Banquet", "event_date": "2026-04-15"},
        files={"file": ("a.csv", b"Net ID,Name\njdoe,Jane Doe\n", "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["importable"] is True
    assert body["event"]["event_name"] == "Banquet"
    assert body["summary"]["attendees_matched"] == 1


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

"""Offline tests for the update-import "this overwrites a recent hand edit"
warning (#420), no database.

``alumni_service`` stamps ``manually_edited_at`` on every client edit so manual
edits win over a later import, but the bulk UPDATE import never read the column:
a cohort file built from a week-old export silently reverted a staffer's
correction, and the preview showed it as an ordinary field change. Jake's call
(2026-08-07) is WARN, DO NOT BLOCK — the commit still applies everything, the
preview just points at the rows worth double-checking.

These cover the flag itself (window boundary, no-change rows, never-edited
records), the editor attribution (linked app user > intake-sheet free text >
"we don't know"), the preview-level count, the ONE-query batching that keeps the
attribution off the N+1 path, and — the important one — that none of it changes
what a commit writes.

Session fakery reuses ``tests/test_alumni_update_import``; the subclass here only
adds the batched ``users`` lookup, which the base fake answers with nothing.
"""

import asyncio
import datetime

from sqlalchemy.dialects import postgresql

from app.models.alumni import Alumni
from app.services import import_csv
from tests.test_alumni_import import _csv_bytes, _row_values
from tests.test_alumni_update_import import FakeUpdateSession, _ExecResult

WINDOW = import_csv.MANUAL_EDIT_WARNING_WINDOW_DAYS


def _run(coro):
    return asyncio.run(coro)


def _ago(days: float) -> datetime.datetime:
    """A tz-aware timestamp *days* in the past (what the timestamptz column holds)."""
    return datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days)


class _UserAwareSession(FakeUpdateSession):
    """``FakeUpdateSession`` that can also answer the batched editor-name query.

    ``users`` rows are ``(user_id, first_name, last_name, email)`` — the exact
    columns ``_resolve_editor_names`` selects. ``user_queries`` counts how many
    times that query ran, which is what the N+1 guard asserts on.
    """

    def __init__(self, *args, users=(), **kwargs):
        super().__init__(*args, **kwargs)
        self._users = list(users)
        self.user_queries = 0

    async def execute(self, stmt):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if "users.user_id" in sql:
            self.user_queries += 1
            return _ExecResult(self._users)
        return await super().execute(stmt)


def _session(alumnus: Alumni, users=(), extra=()):
    """A session seeded with *alumnus* (id 1) plus any *extra* ``Alumni``."""
    people = [alumnus, *extra]
    index = [
        (a.alumni_id, a.byu_id, a.net_id, a.first_name, a.last_name, 2018, False)
        for a in people
    ]
    return _UserAwareSession(
        index_rows=index,
        alumni={a.alumni_id: a for a in people},
        users=users,
    )


def _alumnus(alumni_id=1, byu_id="123456789", **overrides) -> Alumni:
    fields = {
        "alumni_id": alumni_id,
        "byu_id": byu_id,
        "net_id": None,
        "first_name": "Jane",
        "last_name": "Doe",
        "graduation_year": 2018,
        "archived": False,
    }
    fields.update(overrides)
    return Alumni(**fields)


def _preview(session, *rows_values):
    csv = _csv_bytes(*rows_values)
    rows, errors = import_csv.parse_and_map(csv)
    assert errors == []
    return _run(import_csv.evaluate_update(session, rows))


# --- The flag ----------------------------------------------------------------


def test_changed_row_on_recently_edited_record_is_flagged_with_date_and_editor():
    edited_at = _ago(3)
    alumnus = _alumnus(
        manually_edited_at=edited_at,
        profile_updated_by_user_id=7,
    )
    session = _session(alumnus, users=[(7, "Amy", "Adams", "amy@byu.edu")])

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    row = preview["rows"][0]
    assert row["status"] == "update"
    # The exact shape the frontend renders: when, who, and how "who" was derived.
    # The private carrier key must never survive into the report.
    assert row["overwrites_manual_edit"] == {
        "manually_edited_at": edited_at.isoformat(),
        "edited_by": "Amy Adams",
        "edited_by_source": "user",
    }
    assert preview["summary"]["overwrites_manual_edit"] == 1


def test_edit_older_than_the_window_is_not_flagged():
    # One day past the window: the spreadsheet is no longer plausibly older than
    # the hand edit, so this is an ordinary field change.
    alumnus = _alumnus(manually_edited_at=_ago(WINDOW + 1), profile_updated_by_user_id=7)
    session = _session(alumnus, users=[(7, "Amy", "Adams", "amy@byu.edu")])

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    assert preview["rows"][0]["status"] == "update"
    assert preview["rows"][0]["overwrites_manual_edit"] is None
    assert preview["summary"]["overwrites_manual_edit"] == 0
    # Nothing was flagged, so the editor query never ran at all.
    assert session.user_queries == 0


def test_row_inside_the_window_is_still_flagged_at_the_boundary():
    edited_at = _ago(WINDOW - 0.5)
    alumnus = _alumnus(manually_edited_at=edited_at)
    session = _session(alumnus)

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    assert preview["rows"][0]["overwrites_manual_edit"] is not None


def test_row_that_changes_nothing_is_not_flagged_even_if_just_edited():
    # Re-uploading the stored value overwrites nothing, so however fresh the hand
    # edit is, there is nothing to warn about.
    alumnus = _alumnus(manually_edited_at=_ago(0.1), profile_updated_by_user_id=7)
    session = _session(alumnus, users=[(7, "Amy", "Adams", "amy@byu.edu")])

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Doe"))

    row = preview["rows"][0]
    assert row["status"] == "no_changes"
    assert row["overwrites_manual_edit"] is None
    assert preview["summary"]["overwrites_manual_edit"] == 0


def test_record_never_manually_edited_is_not_flagged():
    alumnus = _alumnus(manually_edited_at=None)
    session = _session(alumnus)

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    assert preview["rows"][0]["status"] == "update"
    assert preview["rows"][0]["overwrites_manual_edit"] is None
    assert preview["summary"]["overwrites_manual_edit"] == 0


def test_unmatched_row_is_never_flagged():
    alumnus = _alumnus(manually_edited_at=_ago(1))
    session = _session(alumnus)

    preview = _preview(session, _row_values(byu_id="999999999", last_name="Ghost"))

    assert preview["rows"][0]["status"] == "unmatched"
    assert preview["rows"][0]["overwrites_manual_edit"] is None


def test_naive_manually_edited_at_is_read_as_utc_not_dropped():
    # A tz-naive value (legacy row / hand-built object) must not lose the warning
    # to a naive-vs-aware comparison error.
    edited_at = _ago(1).replace(tzinfo=None)
    alumnus = _alumnus(manually_edited_at=edited_at)
    session = _session(alumnus)

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    warning = preview["rows"][0]["overwrites_manual_edit"]
    assert warning is not None
    assert warning["manually_edited_at"] == edited_at.replace(
        tzinfo=datetime.UTC
    ).isoformat()


# --- Who made the edit -------------------------------------------------------


def test_editor_falls_back_to_the_intake_sheet_name_when_no_user_is_linked():
    alumnus = _alumnus(
        manually_edited_at=_ago(2),
        profile_updated_by_user_id=None,
        profile_updated_by="Tanya (sheet)",
    )
    session = _session(alumnus)

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    warning = preview["rows"][0]["overwrites_manual_edit"]
    assert warning["edited_by"] == "Tanya (sheet)"
    assert warning["edited_by_source"] == "sheet"
    # No linked user id, so there was nothing to resolve.
    assert session.user_queries == 0


def test_unknown_editor_says_so_rather_than_guessing():
    alumnus = _alumnus(
        manually_edited_at=_ago(2),
        profile_updated_by_user_id=None,
        profile_updated_by=None,
    )
    session = _session(alumnus)

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    warning = preview["rows"][0]["overwrites_manual_edit"]
    assert warning["edited_by"] is None
    assert warning["edited_by_source"] == "unknown"


def test_linked_user_wins_over_the_sheet_name():
    alumnus = _alumnus(
        manually_edited_at=_ago(2),
        profile_updated_by_user_id=7,
        profile_updated_by="Stale Sheet Name",
    )
    session = _session(alumnus, users=[(7, "Amy", "Adams", "amy@byu.edu")])

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    warning = preview["rows"][0]["overwrites_manual_edit"]
    assert warning["edited_by"] == "Amy Adams"
    assert warning["edited_by_source"] == "user"


def test_editor_with_no_name_falls_back_to_their_email():
    # Same rule as the profile's "Profile updated by ..." hover.
    alumnus = _alumnus(manually_edited_at=_ago(2), profile_updated_by_user_id=7)
    session = _session(alumnus, users=[(7, None, None, "amy@byu.edu")])

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    warning = preview["rows"][0]["overwrites_manual_edit"]
    assert warning["edited_by"] == "amy@byu.edu"
    assert warning["edited_by_source"] == "user"


def test_editor_id_that_resolves_to_nobody_reports_unknown():
    alumnus = _alumnus(manually_edited_at=_ago(2), profile_updated_by_user_id=404)
    session = _session(alumnus, users=[])

    preview = _preview(session, _row_values(byu_id="123456789", last_name="Smith"))

    warning = preview["rows"][0]["overwrites_manual_edit"]
    assert warning["edited_by"] is None
    assert warning["edited_by_source"] == "unknown"


# --- Count + the N+1 guard ---------------------------------------------------


def test_count_matches_the_flagged_rows_and_editors_cost_one_query():
    recent_a = _alumnus(alumni_id=1, byu_id="111111111", manually_edited_at=_ago(1),
                        profile_updated_by_user_id=7)
    recent_b = _alumnus(alumni_id=2, byu_id="222222222", manually_edited_at=_ago(5),
                        profile_updated_by_user_id=8)
    stale = _alumnus(alumni_id=3, byu_id="333333333",
                     manually_edited_at=_ago(WINDOW + 10), profile_updated_by_user_id=7)
    never = _alumnus(alumni_id=4, byu_id="444444444", manually_edited_at=None)
    session = _session(
        recent_a,
        users=[(7, "Amy", "Adams", "amy@byu.edu"), (8, "Nate", "Barnes", "nate@byu.edu")],
        extra=[recent_b, stale, never],
    )

    preview = _preview(
        session,
        _row_values(byu_id="111111111", last_name="Smith"),
        _row_values(byu_id="222222222", last_name="Smith"),
        _row_values(byu_id="333333333", last_name="Smith"),
        _row_values(byu_id="444444444", last_name="Smith"),
    )

    flagged = [r for r in preview["rows"] if r["overwrites_manual_edit"] is not None]
    assert len(flagged) == 2
    assert preview["summary"]["overwrites_manual_edit"] == len(flagged)
    assert preview["summary"]["with_changes"] == 4
    assert [f["overwrites_manual_edit"]["edited_by"] for f in flagged] == [
        "Amy Adams",
        "Nate Barnes",
    ]
    # The whole point: two distinct editors across the file cost ONE query, not
    # one per row. This is the assertion that fails if someone reintroduces a
    # per-row session.get(User, ...).
    assert session.user_queries == 1


# --- The commit is untouched -------------------------------------------------


def test_commit_result_is_unchanged_by_the_warning():
    # A recently hand-edited record is still updated, in full, with the same
    # result shape as before #420 — the warning is information, not a gate.
    alumnus = _alumnus(manually_edited_at=_ago(1), profile_updated_by_user_id=7)
    session = _session(alumnus, users=[(7, "Amy", "Adams", "amy@byu.edu")])
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Smith"))
    rows, _errors = import_csv.parse_and_map(csv)

    result = _run(import_csv.commit_update(session, rows))

    assert result == {
        "updated": 1,
        "unchanged": 0,
        "unmatched": 0,
        "errors": 0,
        "updated_ids": [1],
        "results": [
            {
                "row": 2,
                # The row's own name cells, not the stored record's.
                "name": "Smith",
                "alumni_id": 1,
                "status": "updated",
                "message": "Updated.",
            }
        ],
    }
    # The edit landed (no skip, no per-row override) and the commit result never
    # mentions the warning.
    assert alumnus.last_name == "Smith"
    assert "overwrites_manual_edit" not in str(result)


def test_commit_applies_the_same_rows_whether_or_not_they_are_flagged():
    recent = _alumnus(alumni_id=1, byu_id="111111111", manually_edited_at=_ago(1))
    stale = _alumnus(alumni_id=2, byu_id="222222222",
                     manually_edited_at=_ago(WINDOW + 10))
    session = _session(recent, extra=[stale])
    csv = _csv_bytes(
        _row_values(byu_id="111111111", last_name="Smith"),
        _row_values(byu_id="222222222", last_name="Jones"),
    )
    rows, _errors = import_csv.parse_and_map(csv)

    result = _run(import_csv.commit_update(session, rows))

    assert result["updated"] == 2
    assert result["updated_ids"] == [1, 2]
    assert recent.last_name == "Smith"
    assert stale.last_name == "Jones"


# --- Route level: the fields survive the response model ----------------------


def test_route_preview_exposes_the_warning_and_the_count():
    import uuid

    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.core.database import get_session
    from app.main import app
    from app.schemas.auth import UserContext

    edited_at = _ago(2)
    alumnus = _alumnus(manually_edited_at=edited_at, profile_updated_by_user_id=7)
    session = _session(alumnus, users=[(7, "Amy", "Adams", "amy@byu.edu")])

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["full_access"],
    )
    csv = _csv_bytes(_row_values(byu_id="123456789", last_name="Smith"))
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/alumni/import/update/preview",
            files={"file": ("cohort.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    body = resp.json()
    # The response_model must carry both through, or the UI sees nothing.
    assert body["summary"]["overwrites_manual_edit"] == 1
    assert body["rows"][0]["overwrites_manual_edit"] == {
        "manually_edited_at": edited_at.isoformat(),
        "edited_by": "Amy Adams",
        "edited_by_source": "user",
    }


def test_route_preview_header_errors_still_return_a_zero_count():
    # The route builds this summary by hand and doesn't know about the new key;
    # the schema default is what keeps the shape consistent for the UI.
    import uuid

    from fastapi.testclient import TestClient

    from app.api.dependencies.auth import get_current_db_user
    from app.core.database import get_session
    from app.main import app
    from app.schemas.auth import UserContext

    session = _UserAwareSession()

    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=["full_access"],
    )
    csv = _csv_bytes(["Jane"], headers=["Only column"])
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.post(
            "/alumni/import/update/preview",
            files={"file": ("bad.csv", csv, "text/csv")},
        )
    app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert resp.json()["summary"]["overwrites_manual_edit"] == 0

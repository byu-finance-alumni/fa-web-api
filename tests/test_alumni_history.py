"""Per-record version history (#45): GET /alumni/{id}/history.

The grouping / keyset pagination is SQL, so these drive it against a real
(in-memory SQLite) ``audit_logs`` table through a tiny async shim — the suite has
no Postgres and no aiosqlite. The alumnus lookup is canned on the shim.
"""

from __future__ import annotations

import datetime
import pathlib
import re
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core.capabilities import DEFAULT_GRANTS, Capability
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.schemas.alumni import VIEW_ONLY_HIDDEN_FIELDS
from app.schemas.auth import UserContext
from app.services import alumni_history

T0 = datetime.datetime(2026, 9, 1, 12, 0, 0)


class _AsyncShim:
    """Just enough of AsyncSession over a sync SQLite Session."""

    def __init__(self, sync: Session, alumnus):
        self._s = sync
        self._alumnus = alumnus
        self.added: list[object] = []
        self.commits = 0

    async def get(self, model, pk):
        if model is Alumni and self._alumnus is not None and pk == self._alumnus.alumni_id:
            return self._alumnus
        return None

    async def execute(self, stmt):
        return self._s.execute(stmt)

    def add(self, obj):
        # Captured, not written: the read's own view_history row is asserted on.
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


@pytest.fixture
def db():
    # TestClient runs the app in another thread; share one connection.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AuditLog.__table__.create(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


_next_id = iter(range(1, 10_000))


def _row(
    db,
    *,
    action="update",
    field=None,
    old=None,
    new=None,
    cs=None,
    at=T0,
    alumni_id=1,
    source="manual",
    actor="Sam Editor",
    email="sam@byu.edu",
    entity_type="alumni",
):
    r = AuditLog(
        audit_log_id=next(_next_id),
        user_id=5,
        action_type=action,
        entity_type=entity_type,
        entity_id=alumni_id,
        field_name=field,
        old_value=old,
        new_value=new,
        change_set_id=cs,
        source=source,
        actor_name=actor,
        actor_email=email,
        created_at=at,
    )
    db.add(r)
    db.flush()
    return r


def _alum(archived=False):
    return SimpleNamespace(alumni_id=1, archived=archived)


def _ctx(*roles):
    return UserContext(
        user_id=7,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def client_for(db):
    shims: list[_AsyncShim] = []

    def make(role, alumnus=None, config=None):
        shim = _AsyncShim(db, alumnus if alumnus is not None else _alum())
        shims.append(shim)
        app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
        app.dependency_overrides[get_session] = lambda: shim
        if config is not None:
            app.dependency_overrides[get_permission_config] = lambda: config
        return TestClient(app), shim

    yield make
    app.dependency_overrides.pop(get_current_db_user, None)
    app.dependency_overrides.pop(get_session, None)


# --- permission ---------------------------------------------------------------


def test_view_only_is_forbidden(client_for, db):
    _row(db, field="first_name", old="A", new="B", cs="c1")
    c, _ = client_for("view_only")
    assert c.get("/alumni/1/history").status_code == 403


@pytest.mark.parametrize("role", ["student", "full_access", "super_admin", "engineer"])
def test_editor_roles_can_read(client_for, db, role):
    _row(db, field="first_name", old="A", new="B", cs="c1")
    c, _ = client_for(role)
    res = c.get("/alumni/1/history")
    assert res.status_code == 200
    assert res.json()["items"][0]["changes"][0]["new"] == "B"


# --- filtering ----------------------------------------------------------------


def test_read_rows_are_excluded(client_for, db):
    _row(db, action="view_profile", at=T0 + datetime.timedelta(minutes=5), source=None)
    _row(db, action="search", at=T0 + datetime.timedelta(minutes=6), source=None)
    _row(db, action="export_profile", at=T0 + datetime.timedelta(minutes=7), source=None)
    _row(db, action="view_history", at=T0 + datetime.timedelta(minutes=8), source=None)
    _row(db, field="last_name", old="X", new="Y", cs="c1")
    c, _ = client_for("student")
    items = c.get("/alumni/1/history").json()["items"]
    assert len(items) == 1
    assert [ch["action"] for ch in items[0]["changes"]] == ["update"]


def test_other_records_and_entities_excluded(client_for, db):
    _row(db, field="first_name", old="A", new="B", cs="mine")
    _row(db, field="first_name", old="C", new="D", cs="theirs", alumni_id=2)
    _row(db, action="update", entity_type="event", field="name", new="E", cs="ev")
    c, _ = client_for("student")
    items = c.get("/alumni/1/history").json()["items"]
    assert [i["group_id"] for i in items] == ["mine"]


# --- grouping -----------------------------------------------------------------


def test_groups_by_change_set_newest_first(client_for, db):
    _row(db, field="first_name", old="A", new="B", cs="old", at=T0)
    _row(
        db,
        field="contact.email",
        old="a@x",
        new="b@x",
        cs="new",
        at=T0 + datetime.timedelta(days=1),
    )
    _row(
        db,
        field="career.current_employer",
        old="Acme",
        new="Beta",
        cs="new",
        at=T0 + datetime.timedelta(days=1),
        source="import",
    )
    # A legacy row with no change set is its own group.
    _row(db, action="archive", cs=None, at=T0 - datetime.timedelta(days=1))
    c, shim = client_for("student")
    body = c.get("/alumni/1/history").json()
    items = body["items"]
    assert [i["change_set_id"] for i in items] == ["new", "old", None]
    assert items[2]["group_id"].startswith("row:")
    newest = items[0]
    assert [ch["field"] for ch in newest["changes"]] == [
        "contact.email",
        "career.current_employer",
    ]
    # Every change carries its audit row id (the restore handle).
    assert all(isinstance(ch["audit_id"], int) for ch in newest["changes"])
    assert newest["actor_name"] == "Sam Editor"
    assert newest["source"] == "manual"
    assert body["history_starts"] == "2026-08-18"
    assert body["next_before"] is None


def test_never_returns_email(client_for, db):
    _row(db, field="first_name", old="A", new="B", cs="c1", actor=None, email="secret@byu.edu")
    c, _ = client_for("student")
    res = c.get("/alumni/1/history")
    assert "secret@byu.edu" not in res.text
    assert res.json()["items"][0]["actor_name"] is None


def test_survey_summary_row_sets_source_but_is_not_a_change(client_for, db):
    _row(db, action="apply_survey_response", new="survey_response=4 fields=2", cs="s1", source=None)
    _row(db, field="first_name", old="A", new="B", cs="s1", source=None)
    c, _ = client_for("student")
    item = c.get("/alumni/1/history").json()["items"][0]
    assert item["source"] == "survey"
    assert [ch["field"] for ch in item["changes"]] == ["first_name"]


def test_labels_use_export_catalog_and_row_kinds():
    assert alumni_history.field_label("preferred_first_name") == "Preferred first name"
    assert alumni_history.field_label("employment[12].employment_title") == (
        "Past role: Employment title"
    )
    assert alumni_history.field_label("employment[12]") == "Past role"
    assert alumni_history.field_label(None) is None
    assert alumni_history.field_label("contact.some_new_col") == "Some new col"


# --- pagination ---------------------------------------------------------------


def test_keyset_pagination_walks_every_group_once(client_for, db):
    for i in range(5):
        at = T0 + datetime.timedelta(hours=i)
        _row(db, field="first_name", old=str(i), new=str(i + 1), cs=f"g{i}", at=at)
        _row(db, field="last_name", old=str(i), new=str(i + 1), cs=f"g{i}", at=at)
    # Two groups sharing one timestamp (a bulk import's single transaction).
    same = T0 + datetime.timedelta(hours=10)
    _row(db, field="first_name", new="x", cs="tieA", at=same)
    _row(db, field="first_name", new="y", cs="tieB", at=same)
    c, _ = client_for("student")
    seen, cursor, pages = [], None, 0
    while True:
        url = "/alumni/1/history?limit=2" + (f"&before={cursor}" if cursor else "")
        body = c.get(url).json()
        pages += 1
        seen += [i["group_id"] for i in body["items"]]
        # A group is never split across pages.
        for i in body["items"]:
            if i["group_id"].startswith("g"):
                assert len(i["changes"]) == 2
        cursor = body["next_before"]
        if not cursor:
            break
    assert pages == 4
    assert seen == ["tieB", "tieA", "g4", "g3", "g2", "g1", "g0"]


def test_bad_cursor_is_422(client_for, db):
    c, _ = client_for("student")
    assert c.get("/alumni/1/history?before=not-a-cursor!!").status_code == 422


def test_limit_bounds(client_for, db):
    c, _ = client_for("student")
    assert c.get("/alumni/1/history?limit=0").status_code == 422
    assert c.get("/alumni/1/history?limit=51").status_code == 422


# --- archived / missing -------------------------------------------------------


def test_archived_record_404s_like_the_profile(client_for, db):
    _row(db, field="first_name", old="A", new="B", cs="c1")
    c, _ = client_for("full_access", alumnus=_alum(archived=True))
    assert c.get("/alumni/1/history").status_code == 404


def test_missing_record_404s(client_for, db):
    c, shim = client_for("student")
    shim._alumnus = None
    assert c.get("/alumni/1/history").status_code == 404


# --- audit of the read itself ------------------------------------------------


def test_read_is_audit_logged(client_for, db):
    c, shim = client_for("student")
    c.get("/alumni/1/history")
    logged = [a for a in shim.added if isinstance(a, AuditLog)]
    assert [(a.action_type, a.entity_type, a.entity_id, a.user_id) for a in logged] == [
        ("view_history", "alumni", 1, 7)
    ]
    assert logged[0].old_value is None and logged[0].new_value is None


# --- redaction parity with the profile ---------------------------------------


def test_non_editor_granted_capability_gets_values_redacted(client_for, db):
    """If an engineer grants alumni.edit to view_only, the PROFILE still treats
    that caller as a non-editor (minimized, no audit trail). History must not
    show them more: shape only, every value nulled."""
    for field in sorted(VIEW_ONLY_HIDDEN_FIELDS):
        _row(db, field=field, old=f"old-{field}", new=f"new-{field}", cs="c1")
    config = {k: set(v) for k, v in DEFAULT_GRANTS.items()}
    config["view_only"] = set(config["view_only"]) | {Capability.ALUMNI_EDIT}
    config = {k: frozenset(v) for k, v in config.items()}
    c, _ = client_for("view_only", config=config)
    res = c.get("/alumni/1/history")
    assert res.status_code == 200
    changes = res.json()["items"][0]["changes"]
    assert len(changes) == len(VIEW_ONLY_HIDDEN_FIELDS)
    assert all(ch["old"] is None and ch["new"] is None for ch in changes)
    assert all(ch["redacted"] for ch in changes)
    assert "old-" not in res.text and "new-" not in res.text


def test_editor_sees_profile_visible_values(client_for, db):
    """Editors see the whole profile (minimize_alumni_read is a no-op for
    them), so they see every value — the same fields, unredacted."""
    _row(db, field="gender", old="Male", new="Female", cs="c1")
    c, _ = client_for("student")
    ch = c.get("/alumni/1/history").json()["items"][0]["changes"][0]
    assert (ch["old"], ch["new"], ch["redacted"]) == ("Male", "Female", False)


# --- classification guard ------------------------------------------------------


_APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _alumni_audit_actions() -> set[str]:
    """Every action literal written against an ``alumni`` audit entity."""
    found: set[str] = set()
    helper_files = {
        "services/alumni.py",
        "services/profile.py",
        "services/notes.py",
        "services/opportunity_links.py",
        "api/routes/alumni.py",
    }
    for path in _APP.rglob("*.py"):
        text = path.read_text()
        rel = path.relative_to(_APP).as_posix()
        found |= set(re.findall(r'_audit_alumni\(\s*[^,]+,\s*[^,]+,\s*"(\w+)"', text))
        if rel in helper_files:
            found |= set(re.findall(r'\b_audit\(\s*[^,]+,\s*[^,]+,\s*"(\w+)"', text))
        for block in re.findall(r"AuditLog\((.*?)\n\s*\)", text, flags=re.S):
            if 'entity_type="alumni"' in block:
                found |= set(re.findall(r'action_type="(\w+)"', block))
        for m in re.finditer(r'action="(\w+)",\s*entity_type="alumni"', text):
            found.add(m.group(1))
    return found


def test_every_alumni_audit_action_is_classified():
    actions = _alumni_audit_actions()
    # Sanity: the scanner actually sees the writers.
    assert {"update", "view_profile", "add_employment", "upload_headshot"} <= actions
    unclassified = actions - alumni_history.HISTORY_ACTIONS - alumni_history.NON_HISTORY_ACTIONS
    assert not unclassified, (
        f"Classify these alumni audit actions in app/services/alumni_history.py "
        f"(HISTORY_ACTIONS if they change the record, else NON_HISTORY_ACTIONS): "
        f"{sorted(unclassified)}"
    )
    assert not (alumni_history.HISTORY_ACTIONS & alumni_history.NON_HISTORY_ACTIONS)

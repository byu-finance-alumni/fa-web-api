"""#591 — archived records stay hidden below full_access, and sensitive reads
leave an audit row.

Archived: the event roster + its CSV, the dashboard activity feed and the donor
list/detail leave archived alumni out for roles below full_access, and keep them
for full_access and up (exactly the alumni list's rule). Notes are covered in
tests/test_notes_routes.py.

Audited: GET /alumni/{id}, the donor list + a donor's history, the survey
console lists, and headshot URL reads — the batch route writing ONE row per call
however many alumni it covers.

No database: a recording fake session captures every statement (so the archived
predicate can be read off the compiled SQL) and every added row.
"""

import datetime
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies import auth as auth_deps
from app.api.dependencies.auth import get_current_db_user
from app.api.routes import alumni as alumni_routes
from app.api.routes import survey as survey_routes
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.event import Event
from app.schemas.auth import UserContext

BELOW = ["view_only", "student"]
ABOVE = ["full_access", "super_admin", "engineer"]
_ARCHIVED_PREDICATE = "alumni.archived IS false"


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=9,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


class _Result:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _Session:
    """Records statements and added rows; answers everything with empties
    unless told otherwise."""

    def __init__(self, *, get=None, scalar=None, scalars=()):
        self._get = get or {}
        self._scalar = scalar
        self._scalars = list(scalars)
        self.statements: list = []
        self.added: list = []
        self.commits = 0

    async def get(self, model, pk):
        return self._get.get(model)

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return self._scalar

    async def scalars(self, stmt):
        self.statements.append(stmt)
        return _Result(self._scalars)

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    def sql(self) -> list[str]:
        return [
            str(s.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            for s in self.statements
        ]

    @property
    def audits(self) -> list[AuditLog]:
        return [a for a in self.added if isinstance(a, AuditLog)]


@pytest.fixture
def run():
    """``run(session, role, method, url)`` -> response, with overrides cleared."""

    def _run(session, role, method, url, **kw):
        async def _s():
            yield session

        app.dependency_overrides[get_session] = _s
        app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
        # The export-capability guard sits on a different resolver chain; pin it
        # to the same caller so the CSV route sees the role under test.
        app.dependency_overrides[auth_deps.require_alumni_export] = lambda: _ctx(role)
        try:
            with TestClient(app, raise_server_exceptions=False) as client:
                return client.request(method, url, **kw)
        finally:
            for dep in (get_session, get_current_db_user, auth_deps.require_alumni_export):
                app.dependency_overrides.pop(dep, None)

    return _run


def _event():
    return SimpleNamespace(event_id=4, event_name="Mixer")


# --- archived: event roster + CSV --------------------------------------------


@pytest.mark.parametrize("role", BELOW)
def test_event_roster_hides_archived_below_full_access(run, role):
    session = _Session(get={Event: _event()})
    resp = run(session, role, "GET", "/events/4/attendees")
    assert resp.status_code == 200, resp.text
    assert _ARCHIVED_PREDICATE in session.sql()[-1]


@pytest.mark.parametrize("role", ABOVE)
def test_event_roster_keeps_archived_for_full_access(run, role):
    session = _Session(get={Event: _event()})
    resp = run(session, role, "GET", "/events/4/attendees")
    assert resp.status_code == 200, resp.text
    assert _ARCHIVED_PREDICATE not in session.sql()[-1]


@pytest.mark.parametrize(("role", "hidden"), [("student", True), ("full_access", False)])
def test_event_roster_csv_follows_the_same_rule(run, role, hidden):
    session = _Session(get={Event: _event()})
    resp = run(session, role, "GET", "/events/4/attendees/export")
    assert resp.status_code == 200, resp.text
    assert (_ARCHIVED_PREDICATE in session.sql()[-1]) is hidden


# --- archived: dashboard activity feed ---------------------------------------


def _activity_config(role):
    """reports.advanced is full_access-and-up by default; grant it to the role
    under test so the lower tiers can reach the feed at all (the case #591 is
    about: an assignable capability handed to a lower role)."""
    from app.core.capabilities import DEFAULT_GRANTS, Capability

    config = dict(DEFAULT_GRANTS)
    config[role] = frozenset(config.get(role, frozenset())) | {Capability.REPORTS_ADVANCED}
    return config


@pytest.mark.parametrize(("role", "hidden"), [("student", True), ("view_only", True),
                                              ("full_access", False), ("engineer", False)])
def test_activity_feed_archived_rule(run, role, hidden):
    session = _Session(scalar=0)
    app.dependency_overrides[auth_deps.get_permission_config] = lambda: _activity_config(role)
    resp = run(session, role, "GET", "/dashboard/activity")
    assert resp.status_code == 200, resp.text
    count_sql, rows_sql = session.sql()[0], session.sql()[1]
    assert (_ARCHIVED_PREDICATE in count_sql) is hidden
    assert (_ARCHIVED_PREDICATE in rows_sql) is hidden


# --- archived + audited: donations -------------------------------------------


def _donations_config(role):
    from app.core.capabilities import DEFAULT_GRANTS, Capability

    config = dict(DEFAULT_GRANTS)
    config[role] = frozenset(config.get(role, frozenset())) | {Capability.DONATIONS_VIEW}
    return config


@pytest.mark.parametrize(("role", "hidden"), [("student", True), ("full_access", False)])
def test_donor_list_archived_rule_and_audit(run, role, hidden):
    session = _Session(scalar=0)
    app.dependency_overrides[auth_deps.get_permission_config] = lambda: _donations_config(role)
    resp = run(session, role, "GET", "/donations/donors")
    assert resp.status_code == 200, resp.text
    count_sql, page_sql = session.sql()[0], session.sql()[1]
    assert (_ARCHIVED_PREDICATE in count_sql) is hidden
    assert (_ARCHIVED_PREDICATE in page_sql) is hidden
    assert [(a.action_type, a.entity_type, a.user_id) for a in session.audits] == [
        ("view_donors", "donation", 9)
    ]


def _alumnus(archived: bool):
    return SimpleNamespace(
        alumni_id=42,
        first_name="Jane",
        preferred_first_name=None,
        last_name="Doe",
        archived=archived,
    )


def test_donor_history_of_archived_alumnus_404s_below_full_access(run):
    session = _Session(get={Alumni: _alumnus(archived=True)})
    app.dependency_overrides[auth_deps.get_permission_config] = lambda: _donations_config(
        "student"
    )
    resp = run(session, "student", "GET", "/donations/alumni/42")
    assert resp.status_code == 404
    assert session.audits == []


@pytest.mark.parametrize("archived", [True, False])
def test_donor_history_is_audited_for_full_access(run, archived):
    session = _Session(get={Alumni: _alumnus(archived=archived)})
    resp = run(session, "full_access", "GET", "/donations/alumni/42")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Jane Doe"
    assert [(a.action_type, a.entity_type, a.entity_id) for a in session.audits] == [
        ("view_donations", "alumni", 42)
    ]


# --- audited: GET /alumni/{id} -----------------------------------------------


def _core_record():
    now = datetime.datetime(2026, 6, 12, tzinfo=datetime.UTC)
    return SimpleNamespace(
        alumni_id=42,
        first_name="Jane",
        last_name="Doe",
        deceased=False,
        is_alumni=True,
        archived=False,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("role", ["view_only", "student", "full_access"])
def test_get_alumni_core_record_is_audited(run, role):
    session = _Session(get={Alumni: _core_record()})
    resp = run(session, role, "GET", "/alumni/42")
    assert resp.status_code == 200, resp.text
    assert resp.json()["alumni_id"] == 42
    assert [(a.action_type, a.entity_type, a.entity_id, a.user_id) for a in session.audits] == [
        ("view_alumni", "alumni", 42, 9)
    ]


# --- audited: headshots -------------------------------------------------------


@pytest.fixture
def signer(monkeypatch):
    async def _sign(_bucket, net_id):
        return f"https://storage.example/{net_id}?token=x"

    monkeypatch.setattr(alumni_routes.supabase_storage, "create_signed_url", _sign)


def test_headshot_url_read_is_audited(run, signer):
    alumnus = SimpleNamespace(alumni_id=42, net_id="jdoe12", archived=False)
    session = _Session(scalar=alumnus)
    resp = run(session, "full_access", "GET", "/alumni/42/headshot")
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"].startswith("https://storage.example/")
    assert [(a.action_type, a.entity_id) for a in session.audits] == [("view_headshot", 42)]


def test_headshot_url_read_with_no_photo_is_not_audited(run, signer):
    alumnus = SimpleNamespace(alumni_id=42, net_id=None, archived=False)
    session = _Session(scalar=alumnus)
    resp = run(session, "full_access", "GET", "/alumni/42/headshot")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": None}
    assert session.audits == []


def test_headshot_batch_writes_one_audit_row_per_call(run, signer):
    rows = [
        SimpleNamespace(alumni_id=i, net_id=f"user{i}", archived=False) for i in range(1, 26)
    ]
    session = _Session(scalars=rows)
    query = "&".join(f"alumni_ids={i}" for i in range(1, 26))
    resp = run(session, "view_only", "GET", f"/alumni/headshots/urls?{query}")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["urls"]) == 25
    assert len(session.audits) == 1
    audit = session.audits[0]
    assert audit.action_type == "view_headshots"
    assert audit.entity_id is None
    assert audit.new_value == "requested=25; issued=25"
    # Never the ids (or net IDs) themselves.
    assert "user" not in audit.new_value


# --- audited: survey console lists -------------------------------------------


@pytest.mark.parametrize(
    ("url", "service_attr", "fn", "result", "action"),
    [
        ("/survey/campaigns/2020/responses", "survey_responses", "list_pending", [],
         "read_survey_responses"),
        ("/survey/campaigns/2020/unreachable", "survey_email", "list_unreachable", [],
         "read_survey_unreachable"),
        ("/survey/schedules/2020/non-responders", "survey_schedule", "list_non_responders",
         [], "read_survey_non_responders"),
    ],
)
def test_survey_console_lists_are_audited(run, monkeypatch, url, service_attr, fn, result,
                                          action):
    async def _fake(_session, _year):
        return result

    monkeypatch.setattr(getattr(survey_routes, service_attr), fn, _fake)
    session = _Session()
    resp = run(session, "full_access", "GET", url)
    assert resp.status_code == 200, resp.text
    assert [(a.action_type, a.entity_type, a.entity_id) for a in session.audits] == [
        (action, "survey_campaign", 2020)
    ]


def test_survey_responders_and_recipients_are_audited(run, monkeypatch):
    from app.schemas.survey import SurveyRecipientBreakdown, SurveyResponders

    async def _responders(_session, _year):
        return SurveyResponders.model_validate(
            {k: [] for k in SurveyResponders.model_fields}
        )

    async def _breakdown(_session, year):
        return SurveyRecipientBreakdown.model_validate(
            {
                name: (year if name == "graduation_year" else 0)
                for name, field in SurveyRecipientBreakdown.model_fields.items()
                if field.is_required()
            }
        )

    monkeypatch.setattr(survey_routes.survey_schedule, "list_responders", _responders)
    monkeypatch.setattr(survey_routes.survey_email, "recipient_breakdown", _breakdown)

    session = _Session()
    resp = run(session, "full_access", "GET", "/survey/schedules/2020/responders")
    assert resp.status_code == 200, resp.text
    assert [a.action_type for a in session.audits] == ["read_survey_responders"]

    session = _Session()
    resp = run(session, "full_access", "GET", "/survey/campaigns/2020/recipients")
    assert resp.status_code == 200, resp.text
    assert [a.action_type for a in session.audits] == ["read_survey_recipients"]


def test_survey_non_responders_404_writes_no_audit(run, monkeypatch):
    async def _none(_session, _year):
        return None

    monkeypatch.setattr(survey_routes.survey_schedule, "list_non_responders", _none)
    session = _Session()
    resp = run(session, "full_access", "GET", "/survey/schedules/2020/non-responders")
    assert resp.status_code == 404
    assert session.audits == []

"""Opportunity links: URL validation, landing state, filtering, authz (#441).

Four layers, no real database and no network:

  * **The URL rule** — the crux of this feature. The stored value is rendered as
    a clickable href to a signed-in staff member and it arrives from a PUBLIC,
    token-gated write, so these tests pin scheme gating and, in particular, the
    RFC-3986-vs-WHATWG host-parsing differential. The differential itself is
    asserted against the real ``urlsplit``, so the test proves the trap is real
    rather than restating the fix.
  * **Landing state** — staff entry lands ``approved`` (typing it in IS the
    review), survey submission lands ``pending``.
  * **Filtering / paging** — including that an unfiltered read returns approved
    links only.
  * **Permission gating** — writes and moderation need ``surveys.manage``, and a
    caller without it cannot read unmoderated rows by any route.

Offline: auth, permission config and the session are overridden, so no
DATABASE_URL is required.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core import rate_limit
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.employment import CurrentEmployment
from app.models.opportunity_link import (
    COUNTRY_MAX,
    ROLE_TYPES,
    URL_MAX,
    OpportunityLink,
)
from app.models.user import User
from app.schemas.auth import UserContext
from app.schemas.opportunity_link import (
    MAX_LINKS_PER_SUBMIT,
    OpportunityLinkCreate,
    OpportunityLinkFilters,
    OpportunityLinkSubmitRequest,
    OpportunityLinkUpdate,
    OpportunitySurveyLinkSubmit,
    _today,
    validate_application_deadline,
    validate_opportunity_url,
)
from app.services import opportunity_links as service

GOOD_URL = "https://careers.acme-capital.example/jobs/analyst-2027"


def _run(coro):
    """Drive a coroutine to completion.

    This suite has no async plugin — the house convention is a plain
    ``asyncio.run`` per test (see tests/test_alumni_hygiene.py).
    """
    return asyncio.run(coro)


# --------------------------------------------------------------- fake session --


class _FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    """A no-DB session that dispatches ``get``/``execute`` by entity.

    ``execute`` reads the statement's first entity so the three lookups
    ``_project`` makes (alumni, employment, users) can be answered from canned
    rows without a database.
    """

    def __init__(
        self,
        *,
        alumni=None,
        link=None,
        alumni_rows=(),
        employment_rows=(),
        user_rows=(),
        link_rows=(),
        count=0,
    ):
        self._alumni = alumni
        self._link = link
        self._rows = {
            Alumni: list(alumni_rows),
            CurrentEmployment: list(employment_rows),
            User: list(user_rows),
            OpportunityLink: list(link_rows),
        }
        self._count = count
        self.added: list = []
        self.deleted: list = []
        self.committed = False
        self.statements: list = []
        self._next_id = 500

    async def get(self, model, pk):
        if model is Alumni:
            return self._alumni
        if model is OpportunityLink:
            return self._link
        if model is User:
            return next(
                (u for u in self._rows[User] if u.user_id == pk), None
            )
        return None

    async def execute(self, stmt):
        self.statements.append(stmt)
        entity = stmt.column_descriptions[0]["entity"]
        return _FakeResult(self._rows.get(entity, []))

    async def scalar(self, stmt):
        self.statements.append(stmt)
        return self._count

    def add(self, obj):
        if isinstance(obj, OpportunityLink) and obj.opportunity_link_id is None:
            obj.opportunity_link_id = self._next_id
            self._next_id += 1
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        if isinstance(obj, OpportunityLink):
            stamp = datetime.datetime(2026, 8, 17, 12, 0, tzinfo=datetime.UTC)
            obj.submitted_at = obj.submitted_at or stamp
            obj.updated_at = obj.updated_at or stamp

    @property
    def links(self) -> list[OpportunityLink]:
        return [o for o in self.added if isinstance(o, OpportunityLink)]

    @property
    def audits(self) -> list[AuditLog]:
        return [o for o in self.added if isinstance(o, AuditLog)]


def _alum(alumni_id: int = 1, **kw) -> Alumni:
    alum = Alumni(
        alumni_id=alumni_id,
        first_name=kw.get("first_name", "Dana"),
        last_name=kw.get("last_name", "Whitcomb"),
        preferred_first_name=kw.get("preferred_first_name"),
    )
    alum.archived = kw.get("archived", False)
    return alum


def _employment(alumni_id: int = 1, employer: str | None = "Acme Capital"):
    return CurrentEmployment(alumni_id=alumni_id, current_employer=employer)


def _link(**kw) -> OpportunityLink:
    base = dict(
        opportunity_link_id=42,
        alumni_id=1,
        is_own_company=False,
        company_name="Acme Capital",
        url=GOOD_URL,
        location_city="Provo",
        location_state="Utah",
        role_type="internship",
        application_deadline=datetime.date(2026, 11, 1),
        details="Summer analyst programme.",
        status="approved",
        source="staff",
        submitted_at=datetime.datetime(2026, 8, 17, 12, 0, tzinfo=datetime.UTC),
        updated_at=datetime.datetime(2026, 8, 17, 12, 0, tzinfo=datetime.UTC),
        created_by_user_id=1,
        reviewed_by_user_id=1,
        reviewed_at=datetime.datetime(2026, 8, 17, 12, 0, tzinfo=datetime.UTC),
    )
    base.update(kw)
    return OpportunityLink(**base)


def _filters(**kw) -> OpportunityLinkFilters:
    """The list's filter object, defaulted the way the route defaults it.

    ``status`` is REQUIRED on the model on purpose (see
    ``OpportunityLinkFilters``) — ``None`` on the wire is resolved to
    ``approved`` once, in the route, so neither the list nor the export can
    forget it. The helper mirrors that so a test reads like a request.
    """
    kw.setdefault("status", "approved")
    return OpportunityLinkFilters(**kw)


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _with_session(session):
    async def _override():
        yield session

    return _override


@pytest.fixture
def client():
    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def lenient_client():
    """A client that surfaces a handler blow-up as a 500 response instead of
    propagating it.

    For the "granted" half of the authorization probes only: there is no
    database behind the overridden session, so a request that CLEARS the guard
    goes on to fail in the handler. That is the distinguishable outcome we want
    (`!= 403`), not an error the test has to swallow. Mirrors
    tests/test_capability_split.py.
    """

    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _as(role: str) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)


# =============================================================================
# 0. The role-type list has one meaning
# =============================================================================


def test_the_role_type_literal_matches_the_db_check():
    """`RoleType` is what the API validates against, `ROLE_TYPES` is what the DB
    CHECK mirrors. Adding a role type in one place only would let the API accept
    a value the database refuses."""
    from typing import get_args

    from app.schemas.opportunity_link import RoleType

    assert set(get_args(RoleType)) == set(ROLE_TYPES)


# =============================================================================
# 1. The URL rule
# =============================================================================


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.acme-capital.example/jobs/analyst-2027",
        "http://jobs.example.com/posting?id=8812&src=alum",
        "https://www.example.co.uk/careers#openings",
        # A long-but-legal URL: query strings on job boards get big.
        "https://boards.example.com/apply?" + "a=1&" * 200,
    ],
)
def test_accepts_ordinary_http_s_links(url):
    assert validate_opportunity_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        # Scheme comparison must be case-insensitive. `urlsplit` lowercases it,
        # and this pins that we rely on that rather than on the caller's casing.
        "JaVaScRiPt:alert(1)",
        "data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "mailto:someone@example.com",
        # A scheme-relative URL has no scheme at all for `urlsplit`.
        "//evil.example/jobs",
        "/relative/path",
        "not a url at all",
    ],
)
def test_rejects_every_non_http_scheme(url):
    """Scheme gating is the ONE attack this field fully defeats: `javascript:`
    is a script a signed-in reviewer executes by clicking."""
    with pytest.raises(ValueError):
        validate_opportunity_url(url)


def test_the_backslash_host_differential_is_real_and_is_refused():
    """The trap, asserted from both ends.

    First half: prove the differential exists — Python's RFC-3986 `urlsplit`
    reads the host of this URL as the innocent one, while every browser's WHATWG
    parser treats `\\` as `/` and resolves at `evil.example`. A naive validator
    that only ran `urlsplit` would therefore be reasoning about a completely
    different host from the one the staff member lands on.

    Second half: our validator refuses it outright, before parsing.
    """
    from urllib.parse import urlsplit

    hostile = "https://evil.example\\@careers.acme.example/jobs"
    # What a naive check would have believed:
    assert urlsplit(hostile).hostname == "careers.acme.example"
    # What we actually do:
    with pytest.raises(ValueError):
        validate_opportunity_url(hostile)


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example\\@careers.acme.example/jobs",
        "https://evil.example%5C@careers.acme.example/jobs",
        # Same byte, other casing — the check is case-insensitive on purpose.
        "https://evil.example%5c@careers.acme.example/jobs",
        "https://careers.acme.example\\..\\evil.example/jobs",
    ],
)
def test_rejects_backslashes_encoded_or_not(url):
    with pytest.raises(ValueError):
        validate_opportunity_url(url)


@pytest.mark.parametrize(
    "url",
    [
        # WHATWG STRIPS raw tab/CR/LF before parsing; urlsplit does not, so the
        # two parsers disagree about the host of this string.
        "https://evil.example\t.careers.acme.example/jobs",
        "https://evil.example\n.careers.acme.example/jobs",
        "https://evil.example\r.careers.acme.example/jobs",
        "https://careers.acme.example/a b",
    ],
)
def test_rejects_whitespace_and_control_characters(url):
    with pytest.raises(ValueError):
        validate_opportunity_url(url)


def test_rejects_invisible_characters_in_the_host():
    """A zero-width character makes two different hosts render identically."""
    with pytest.raises(ValueError):
        validate_opportunity_url("https://careers.acme​.example/jobs")


def test_rejects_embedded_credentials():
    """`https://acme.example@evil.example` READS as acme.example to a human
    scanning the queue and resolves at evil.example. No job posting has a
    userinfo section."""
    with pytest.raises(ValueError):
        validate_opportunity_url("https://careers.acme.example@evil.example/jobs")


@pytest.mark.parametrize(
    "url", ["http://localhost/jobs", "https://intranet/jobs", "https://./jobs"]
)
def test_rejects_hostnames_that_are_not_real_sites(url):
    with pytest.raises(ValueError):
        validate_opportunity_url(url)


def test_rejects_urls_longer_than_the_column():
    too_long = "https://careers.example.com/" + "a" * URL_MAX
    assert len(too_long) > URL_MAX
    with pytest.raises(ValueError):
        validate_opportunity_url(too_long)
    # And the boundary itself is accepted, so the cap is the column's, not a
    # tighter accident.
    exact = "https://careers.example.com/" + "a" * (
        URL_MAX - len("https://careers.example.com/")
    )
    assert len(exact) == URL_MAX
    assert validate_opportunity_url(exact) == exact


def test_url_is_trimmed_not_rejected_for_surrounding_space():
    assert validate_opportunity_url(f"  {GOOD_URL}  ") == GOOD_URL


# =============================================================================
# 2. The same rule is enforced on the PUBLIC path, below Pydantic
# =============================================================================


def test_public_submit_revalidates_below_pydantic(monkeypatch):
    async def _body():
        """The finding this feature was built around, pinned.

        The survey's apply path writes with a raw ``setattr``, so no Pydantic
        validator fires — a rule that lives only on a schema is absent on the path
        the public actually writes through. Here the service re-runs the rule itself,
        so a caller that skipped model validation entirely (``model_construct``,
        i.e. any future non-HTTP caller) still cannot persist a `javascript:` URL.
        """
        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 1
        )
        session = _FakeSession(alumni=_alum())
        hostile = OpportunitySurveyLinkSubmit.model_construct(
            is_own_company=False,
            company_name="Acme",
            url="javascript:alert(1)",
            location_city=None,
            location_state=None,
            role_type="internship",
            application_deadline=None,
            details=None,
        )
        payload = OpportunityLinkSubmitRequest.model_construct(links=[hostile])
        with pytest.raises(ValueError):
            await service.submit_links(session, "tok", payload)
        assert session.links == []
        assert session.committed is False

    _run(_body())


def test_a_bad_link_rejects_the_whole_batch(monkeypatch):
    async def _body():
        """All-or-nothing: an alum never has to guess which of their entries landed."""
        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 1
        )
        session = _FakeSession(alumni=_alum())
        good = OpportunitySurveyLinkSubmit(
            company_name="Acme", url=GOOD_URL, role_type="both"
        )
        bad = OpportunitySurveyLinkSubmit.model_construct(
            is_own_company=False,
            company_name="Acme",
            url="javascript:alert(1)",
            location_city=None,
            location_state=None,
            role_type="both",
            application_deadline=None,
            details=None,
        )
        payload = OpportunityLinkSubmitRequest.model_construct(links=[good, bad])
        with pytest.raises(ValueError):
            await service.submit_links(session, "tok", payload)
        assert session.links == []

    _run(_body())


def test_schema_rejects_a_hostile_url_too():
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            company_name="Acme", url="javascript:alert(1)", role_type="internship"
        )


@pytest.mark.parametrize("bad", ["=cmd|'/c calc'!A1", "+1+1", "@SUM(A1)", "-2+3"])
def test_company_name_refuses_a_csv_formula_lead(bad):
    """Attacker-supplied text destined for a staff export."""
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            company_name=bad, url=GOOD_URL, role_type="internship"
        )


def test_details_are_capped_and_allow_ordinary_prose():
    ok = OpportunitySurveyLinkSubmit(
        company_name="Acme",
        url=GOOD_URL,
        role_type="full_time",
        details="Base salary >= $85k; hybrid <3 days/week>. Apply by Nov 1.",
    )
    assert ok.details.startswith("Base salary")
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            company_name="Acme",
            url=GOOD_URL,
            role_type="full_time",
            details="x" * 2001,
        )


def test_company_identity_is_exclusive():
    # Own company + a typed name is ambiguous, not redundant.
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            is_own_company=True,
            company_name="Acme",
            url=GOOD_URL,
            role_type="both",
        )
    # Neither is a row nobody can render.
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            is_own_company=False, url=GOOD_URL, role_type="both"
        )
    # Own company alone is fine — the name is resolved at read time.
    assert OpportunitySurveyLinkSubmit(
        is_own_company=True, url=GOOD_URL, role_type="both"
    ).company_name is None


def test_public_submit_batch_is_capped():
    one = {"company_name": "Acme", "url": GOOD_URL, "role_type": "both"}
    OpportunityLinkSubmitRequest(links=[one] * MAX_LINKS_PER_SUBMIT)
    with pytest.raises(ValueError):
        OpportunityLinkSubmitRequest(links=[one] * (MAX_LINKS_PER_SUBMIT + 1))


# =============================================================================
# 3. Landing state
# =============================================================================


def test_survey_submitted_links_land_pending(monkeypatch):
    async def _body():
        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 7
        )
        session = _FakeSession(alumni=_alum(alumni_id=7))
        payload = OpportunityLinkSubmitRequest(
            links=[
                {"company_name": "Acme", "url": GOOD_URL, "role_type": "internship"},
                {"is_own_company": True, "url": GOOD_URL, "role_type": "both"},
            ]
        )
        result = await service.submit_links(session, "tok", payload)

        assert result.staged is True
        assert result.link_count == 2
        assert [link.status for link in session.links] == ["pending", "pending"]
        assert [link.source for link in session.links] == ["survey", "survey"]
        # The alumnus comes from the SIGNED TOKEN, never the body.
        assert {link.alumni_id for link in session.links} == {7}
        # No actor on a public write, so no reviewer and no audit row.
        assert all(link.created_by_user_id is None for link in session.links)
        assert all(link.reviewed_by_user_id is None for link in session.links)
        assert session.audits == []
        # The own-company row stores no name; it is resolved at read time.
        own = [link for link in session.links if link.is_own_company][0]
        assert own.company_name is None

    _run(_body())


def test_a_dead_token_stages_nothing(monkeypatch):
    async def _body():
        from app.core.errors import NotFoundError

        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: None
        )
        session = _FakeSession(alumni=_alum())
        payload = OpportunityLinkSubmitRequest(
            links=[{"company_name": "Acme", "url": GOOD_URL, "role_type": "both"}]
        )
        with pytest.raises(NotFoundError):
            await service.submit_links(session, "tok", payload)
        assert session.links == []

    _run(_body())


def test_an_archived_alum_stages_nothing(monkeypatch):
    async def _body():
        from app.core.errors import NotFoundError

        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 1
        )
        session = _FakeSession(alumni=_alum(archived=True))
        payload = OpportunityLinkSubmitRequest(
            links=[{"company_name": "Acme", "url": GOOD_URL, "role_type": "both"}]
        )
        with pytest.raises(NotFoundError):
            await service.submit_links(session, "tok", payload)
        assert session.links == []

    _run(_body())


def test_staff_created_links_land_approved():
    async def _body():
        """A staff member typing it in IS the review, so there is nothing left to
        moderate — and the reviewer is stamped as them."""
        alum = _alum()
        session = _FakeSession(
            alumni=alum, alumni_rows=[alum], employment_rows=[_employment()]
        )
        payload = OpportunityLinkCreate(
            alumni_id=1,
            company_name="Acme Capital",
            url=GOOD_URL,
            role_type="full_time",
            location_city="Provo",
            location_state="Utah",
        )
        result = await service.create_link(session, payload, actor_user_id=9)

        assert result.status == "approved"
        assert result.source == "staff"
        created = session.links[0]
        assert created.reviewed_by_user_id == 9
        assert created.reviewed_at is not None
        assert created.created_by_user_id == 9
        # Every staff write is audited against the owning alumnus.
        assert len(session.audits) == 1
        audit = session.audits[0]
        assert audit.action_type == "add_opportunity_link"
        assert audit.entity_type == "alumni"
        assert audit.entity_id == 1
        assert GOOD_URL in audit.new_value

    _run(_body())


def test_staff_create_404s_for_an_unknown_alumnus():
    async def _body():
        from app.core.errors import NotFoundError

        session = _FakeSession(alumni=None)
        payload = OpportunityLinkCreate(
            alumni_id=999, company_name="Acme", url=GOOD_URL, role_type="both"
        )
        with pytest.raises(NotFoundError):
            await service.create_link(session, payload, actor_user_id=9)
        assert session.links == []

    _run(_body())


# =============================================================================
# 4. Moderation + read projection
# =============================================================================


def test_approving_stamps_the_reviewer_and_audits_the_transition():
    async def _body():
        pending = _link(status="pending", source="survey", reviewed_by_user_id=None)
        alum = _alum()
        session = _FakeSession(
            alumni=alum,
            link=pending,
            alumni_rows=[alum],
            employment_rows=[_employment()],
            user_rows=[User(user_id=9, first_name="Amy", last_name="Reeves")],
        )
        result = await service.moderate_link(
            session, 42, approve=True, actor_user_id=9
        )
        assert result.status == "approved"
        assert result.reviewed_by == "Amy Reeves"
        audit = session.audits[0]
        assert audit.action_type == "approve_opportunity_link"
        assert audit.old_value == "pending"
        assert audit.new_value.startswith("approved")

    _run(_body())


def test_rejecting_keeps_the_row():
    async def _body():
        pending = _link(status="pending", source="survey")
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=pending, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.moderate_link(
            session, 42, approve=False, actor_user_id=9
        )
        assert result.status == "rejected"
        assert session.deleted == []
        assert session.audits[0].action_type == "reject_opportunity_link"

    _run(_body())


def test_own_company_name_is_resolved_from_the_employment_record():
    async def _body():
        alum = _alum()
        link = _link(is_own_company=True, company_name=None)
        session = _FakeSession(
            alumni=alum,
            link=link,
            alumni_rows=[alum],
            employment_rows=[_employment(employer="Sorenson Capital")],
            link_rows=[link],
            count=1,
        )
        page = await service.list_links(session, _filters())
        assert page.items[0].company_name == "Sorenson Capital"
        assert page.items[0].submitted_by == "Dana Whitcomb"

    _run(_body())


def test_own_company_with_no_employment_row_shows_no_name():
    async def _body():
        """A dash in the list, not an invented name — the gap stays visible."""
        alum = _alum()
        link = _link(is_own_company=True, company_name=None)
        session = _FakeSession(
            alumni=alum,
            link=link,
            alumni_rows=[alum],
            employment_rows=[],
            link_rows=[link],
            count=1,
        )
        page = await service.list_links(session, _filters())
        assert page.items[0].company_name is None

    _run(_body())


def test_editing_does_not_change_the_moderation_status():
    async def _body():
        from app.schemas.opportunity_link import OpportunityLinkUpdate

        pending = _link(status="pending", source="survey")
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=pending, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(details="Corrected the deadline."),
            actor_user_id=9,
        )
        assert result.status == "pending"
        assert result.details == "Corrected the deadline."
        # Untouched fields survive the merge.
        assert result.url == GOOD_URL
        assert result.company_name == "Acme Capital"

    _run(_body())


def test_an_edit_cannot_leave_the_row_in_a_state_create_would_refuse():
    async def _body():
        """Flipping off `is_own_company` without supplying a name must fail, not
        persist a company-less row the DB CHECK would reject."""
        from app.schemas.opportunity_link import OpportunityLinkUpdate

        link = _link(is_own_company=True, company_name=None)
        alum = _alum()
        session = _FakeSession(alumni=alum, link=link, alumni_rows=[alum])
        with pytest.raises(ValueError):
            await service.update_link(
                session, 42, OpportunityLinkUpdate(is_own_company=False), actor_user_id=9
            )

    _run(_body())


def test_delete_snapshots_the_link_before_removing_it():
    async def _body():
        link = _link()
        session = _FakeSession(link=link)
        await service.delete_link(session, 42, actor_user_id=9)
        assert session.deleted == [link]
        audit = session.audits[0]
        assert audit.action_type == "delete_opportunity_link"
        assert GOOD_URL in audit.old_value

    _run(_body())


# =============================================================================
# 5. Filtering + the list envelope
# =============================================================================


def test_filters_reach_the_query():
    async def _body():
        """The filters are compiled into the SELECT rather than applied in Python —
        a list that filtered after paging would return short pages."""
        session = _FakeSession(count=0)
        await service.list_links(
            session,
            _filters(
                status="pending",
                role_type="internship",
                company="Acme",
                search="analyst",
            ),
            limit=25,
            offset=50,
        )
        sql = " ".join(str(s) for s in session.statements)
        assert "opportunity_links.status" in sql
        assert "opportunity_links.role_type" in sql
        # The company filter must also reach the joined employer column, or it would
        # silently miss every "my company" row — the rows the feature is named for.
        assert "current_employment.current_employer" in sql
        assert "LEFT OUTER JOIN current_employment" in sql

    _run(_body())


def test_search_wildcards_are_escaped():
    async def _body():
        """A bare `%` in the search box must not become match-everything."""
        session = _FakeSession(count=0)
        await service.list_links(session, _filters(search="100%"))
        params = [
            v
            for s in session.statements
            for v in s.compile().params.values()
            if isinstance(v, str)
        ]
        assert any(r"100\%" in p for p in params)

    _run(_body())


def test_the_page_envelope_reports_the_unpaged_total():
    async def _body():
        alum = _alum()
        link = _link()
        session = _FakeSession(
            alumni_rows=[alum], employment_rows=[], link_rows=[link], count=137
        )
        page = await service.list_links(session, _filters(), limit=10, offset=20)
        assert page.total == 137
        assert page.limit == 10
        assert page.offset == 20
        assert len(page.items) == 1

    _run(_body())


# =============================================================================
# 6. Permission gating (routes)
# =============================================================================


# DELETE is deliberately ABSENT from this table. It moved off `surveys.manage`
# onto its own `links.delete` capability (#441 follow-up), which full_access does
# NOT hold — so it would fail the "a holder of surveys.manage gets past the
# guard" probe below, correctly. Its gating lives in
# tests/test_links_delete_capability.py, which also pins that holding
# surveys.manage is not enough.
_WRITE_PROBES = [
    ("post", "/opportunity-links", {"json": {}}),
    ("patch", "/opportunity-links/1", {"json": {}}),
    ("post", "/opportunity-links/1/approve", {}),
    ("post", "/opportunity-links/1/reject", {}),
]
_WRITE_IDS = [f"{m}:{p}" for m, p, _ in _WRITE_PROBES]


@pytest.mark.parametrize("method,path,kwargs", _WRITE_PROBES, ids=_WRITE_IDS)
@pytest.mark.parametrize("role", ["view_only", "student"])
def test_writes_and_moderation_require_surveys_manage(client, method, path, kwargs, role):
    _as(role)
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize("method,path,kwargs", _WRITE_PROBES, ids=_WRITE_IDS)
def test_a_holder_of_surveys_manage_gets_past_the_guard(
    lenient_client, method, path, kwargs
):
    """`!= 403`, not `== 200`: there is no database behind the overridden
    session, so this is a test about the guard, not about the handler."""
    _as("full_access")
    response = getattr(lenient_client, method)(path, **kwargs)
    assert response.status_code != 403


def test_writes_require_a_token(client):
    app.dependency_overrides.pop(get_current_db_user, None)
    response = client.post("/opportunity-links", json={})
    assert response.status_code == 401


@pytest.mark.parametrize("bad_status", ["pending", "rejected"])
def test_unmoderated_links_are_403_for_a_reader_who_cannot_moderate(
    client, bad_status
):
    """A pending link is unmoderated, attacker-supplied text with a clickable
    URL in it. An explicit refusal, never a silently narrowed result."""
    _as("view_only")
    response = client.get(f"/opportunity-links?status={bad_status}")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_the_default_read_is_approved_only(monkeypatch, client):
    """The safe read is the default: a caller who does not ask for unmoderated
    rows never gets them, whatever their role."""
    seen = {}

    async def _list(session, filters, **kwargs):
        seen["filters"] = filters
        seen.update(kwargs)
        from app.schemas.opportunity_link import OpportunityLinkPage

        return OpportunityLinkPage(items=[], total=0, limit=50, offset=0)

    monkeypatch.setattr("app.services.opportunity_links.list_links", _list)
    _as("view_only")
    assert client.get("/opportunity-links").status_code == 200
    assert seen["filters"].status == "approved"


def test_a_moderator_may_read_the_pending_queue(monkeypatch, client):
    seen = {}

    async def _list(session, filters, **kwargs):
        seen["filters"] = filters
        seen.update(kwargs)
        from app.schemas.opportunity_link import OpportunityLinkPage

        return OpportunityLinkPage(items=[], total=0, limit=50, offset=0)

    monkeypatch.setattr("app.services.opportunity_links.list_links", _list)
    _as("full_access")
    assert client.get("/opportunity-links?status=pending").status_code == 200
    assert seen["filters"].status == "pending"


def test_a_direct_id_fetch_is_not_a_way_round_the_status_gate(monkeypatch, client):
    from app.schemas.opportunity_link import OpportunityLinkRead

    async def _get(session, link_id):
        return OpportunityLinkRead(
            opportunity_link_id=link_id,
            alumni_id=1,
            is_own_company=False,
            company_name="Acme",
            url=GOOD_URL,
            role_type="both",
            status="pending",
            source="survey",
            submitted_at=datetime.datetime(2026, 8, 17, tzinfo=datetime.UTC),
        )

    monkeypatch.setattr("app.services.opportunity_links.get_link", _get)
    _as("view_only")
    assert client.get("/opportunity-links/1").status_code == 403
    _as("full_access")
    assert client.get("/opportunity-links/1").status_code == 200


# =============================================================================
# 7. The public route: no login, but rate limited and validated
# =============================================================================


def _public_body(n: int = 1) -> dict:
    return {
        "links": [
            {"company_name": "Acme", "url": GOOD_URL, "role_type": "internship"}
        ]
        * n
    }


def test_public_submit_needs_no_login_and_is_rate_limited(client, monkeypatch):
    """Consistent with the other survey respond routes: the signed token is the
    whole credential, and the budget is per token AND per client IP."""
    from app.schemas.opportunity_link import OpportunityLinkSubmitResult

    async def _submit(session, token, body):
        return OpportunityLinkSubmitResult(staged=True, link_count=len(body.links))

    monkeypatch.setattr("app.services.opportunity_links.submit_links", _submit)
    app.dependency_overrides.pop(get_current_db_user, None)
    rate_limit.reset()

    codes = [
        client.post("/survey/respond/tok-abc/links", json=_public_body()).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [200] * 10, codes
    assert codes[10] == 429


def test_public_submit_422s_a_hostile_url(client):
    app.dependency_overrides.pop(get_current_db_user, None)
    rate_limit.reset()
    response = client.post(
        "/survey/respond/tok-xyz/links",
        json={
            "links": [
                {
                    "company_name": "Acme",
                    "url": "javascript:alert(1)",
                    "role_type": "internship",
                }
            ]
        },
    )
    assert response.status_code == 422


# =============================================================================
# 8. location_country — the "outside the United States" field
# =============================================================================
#
# The column exists because `location_city` + `location_state` cannot express a
# non-US opening. It is reachable from the PUBLIC survey submit, so it gets the
# SAME treatment as `location_city`: a length cap that mirrors the column, and
# the CSV-formula-lead defence. Every assertion below that says "on the public
# path" is the point of the section — a rule that is stricter on the staff path
# than on the public one is a rule that does not exist.


def test_the_country_cap_mirrors_the_column():
    """`COUNTRY_MAX` is what the schemas enforce; the column width is what the DB
    will actually hold. A drift means we accept a value the column truncates or
    rejects at apply time."""
    assert OpportunityLink.__table__.c.location_country.type.length == COUNTRY_MAX


def test_country_round_trips_through_staff_create():
    async def _body():
        alum = _alum()
        session = _FakeSession(
            alumni=alum, alumni_rows=[alum], employment_rows=[_employment()]
        )
        payload = OpportunityLinkCreate(
            alumni_id=1,
            company_name="Acme Capital",
            url=GOOD_URL,
            role_type="full_time",
            location_city="Toronto",
            location_state="Ontario",
            location_country="Canada",
        )
        result = await service.create_link(session, payload, actor_user_id=9)
        # Persisted...
        assert session.links[0].location_country == "Canada"
        # ...and projected back onto the read shape the staff list binds to.
        assert result.location_country == "Canada"

    _run(_body())


def test_country_round_trips_through_the_public_survey_submit(monkeypatch):
    async def _body():
        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 7
        )
        session = _FakeSession(alumni=_alum(alumni_id=7))
        payload = OpportunityLinkSubmitRequest(
            links=[
                {
                    "company_name": "Nomura",
                    "url": GOOD_URL,
                    "role_type": "internship",
                    "location_city": "Tokyo",
                    "location_country": "Japan",
                }
            ]
        )
        result = await service.submit_links(session, "tok", payload)
        assert result.link_count == 1
        staged = session.links[0]
        assert staged.location_country == "Japan"
        assert staged.location_city == "Tokyo"
        # A non-US opening does not have to invent a state.
        assert staged.location_state is None
        assert staged.status == "pending"

    _run(_body())


def test_country_round_trips_through_an_edit():
    async def _body():
        link = _link(location_country=None)
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=link, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(location_country="United Kingdom"),
            actor_user_id=9,
        )
        assert result.location_country == "United Kingdom"
        # And it can be cleared again — an opening that moves back onshore.
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(location_country=None),
            actor_user_id=9,
        )
        assert result.location_country is None

    _run(_body())


def test_country_reaches_the_read_projection_from_the_list():
    async def _body():
        alum = _alum()
        link = _link(location_country="Singapore")
        session = _FakeSession(
            alumni_rows=[alum], employment_rows=[], link_rows=[link], count=1
        )
        page = await service.list_links(session, _filters())
        assert page.items[0].location_country == "Singapore"

    _run(_body())


def test_country_is_searchable_like_the_other_location_columns():
    async def _body():
        """Country is part of "location" for the free-text box. Leaving it out
        would make a non-US posting unfindable by the one word that says where it
        is — the export/list parity trap this codebase keeps re-learning."""
        session = _FakeSession(count=0)
        await service.list_links(session, _filters(search="Japan"))
        sql = " ".join(str(s) for s in session.statements)
        assert "opportunity_links.location_country" in sql

    _run(_body())


def test_country_is_length_capped_on_the_public_path():
    too_long = "a" * (COUNTRY_MAX + 1)
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            company_name="Acme",
            url=GOOD_URL,
            role_type="internship",
            location_country=too_long,
        )
    # The boundary itself is accepted, so the cap is the column's and not a
    # tighter accident.
    exact = "a" * COUNTRY_MAX
    assert (
        OpportunitySurveyLinkSubmit(
            company_name="Acme",
            url=GOOD_URL,
            role_type="internship",
            location_country=exact,
        ).location_country
        == exact
    )


@pytest.mark.parametrize("bad", ["=cmd|'/c calc'!A1", "+1+1", "@SUM(A1)", "-2+3"])
def test_country_refuses_a_csv_formula_lead_on_the_public_path(bad):
    """This text is attacker-supplied and lands in a staff CSV export, exactly
    like `company_name` and `location_city`."""
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            company_name="Acme",
            url=GOOD_URL,
            role_type="internship",
            location_country=bad,
        )


def test_country_is_revalidated_below_pydantic_on_the_public_path(monkeypatch):
    async def _body():
        """The finding this feature was built around, re-pinned for the new
        column: a caller that skipped model validation entirely still cannot
        persist an over-long or formula-leading country."""
        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 1
        )
        for hostile_country in ("=1+1", "a" * (COUNTRY_MAX + 1)):
            session = _FakeSession(alumni=_alum())
            item = OpportunitySurveyLinkSubmit.model_construct(
                is_own_company=False,
                company_name="Acme",
                url=GOOD_URL,
                location_city=None,
                location_state=None,
                location_country=hostile_country,
                role_type="internship",
                application_deadline=None,
                details=None,
            )
            payload = OpportunityLinkSubmitRequest.model_construct(links=[item])
            with pytest.raises(ValueError):
                await service.submit_links(session, "tok", payload)
            assert session.links == []

    _run(_body())


def test_the_public_route_422s_an_over_long_country(client):
    app.dependency_overrides.pop(get_current_db_user, None)
    rate_limit.reset()
    response = client.post(
        "/survey/respond/tok-country/links",
        json={
            "links": [
                {
                    "company_name": "Acme",
                    "url": GOOD_URL,
                    "role_type": "internship",
                    "location_country": "a" * (COUNTRY_MAX + 1),
                }
            ]
        },
    )
    assert response.status_code == 422


def test_a_null_country_stays_valid_everywhere(monkeypatch):
    async def _body():
        """Nothing about this column is required, and the pre-existing rows that
        predate it are NULL by design — never backfilled to "United States"."""
        # Schema: omitted and explicit-null both land as None.
        assert (
            OpportunitySurveyLinkSubmit(
                company_name="Acme", url=GOOD_URL, role_type="both"
            ).location_country
            is None
        )
        assert (
            OpportunitySurveyLinkSubmit(
                company_name="Acme",
                url=GOOD_URL,
                role_type="both",
                location_country=None,
            ).location_country
            is None
        )
        # Blank / whitespace normalises to None rather than storing "   ".
        assert (
            OpportunitySurveyLinkSubmit(
                company_name="Acme",
                url=GOOD_URL,
                role_type="both",
                location_country="   ",
            ).location_country
            is None
        )
        # Service: a row with no country reads back cleanly.
        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 1
        )
        session = _FakeSession(alumni=_alum())
        await service.submit_links(
            session,
            "tok",
            OpportunityLinkSubmitRequest(
                links=[{"company_name": "Acme", "url": GOOD_URL, "role_type": "both"}]
            ),
        )
        assert session.links[0].location_country is None

    _run(_body())


# =============================================================================
# 9. application_deadline must not be in the past
# =============================================================================
#
# BOUNDARY, stated once here and asserted below: **today is ACCEPTED**; only a
# strictly earlier date is refused. A bare date with no time on it means
# "applications close at the end of that day", so a posting due today is still
# actionable — and it is the most urgent thing the form ever receives. Matches
# how `routes.events` already reads `event_date >= today` as UPCOMING.
#
# The dates below are computed RELATIVE to the server's current date rather than
# hard-coded, so this section does not quietly start testing something else the
# moment the calendar moves past a literal.


def _past(days: int = 1) -> datetime.date:
    return _today() - datetime.timedelta(days=days)


def _future(days: int = 30) -> datetime.date:
    return _today() + datetime.timedelta(days=days)


def test_today_is_an_acceptable_deadline():
    """The documented boundary: inclusive. A posting that closes at the end of
    today has not expired."""
    assert validate_application_deadline(_today()) == _today()
    assert (
        OpportunitySurveyLinkSubmit(
            company_name="Acme",
            url=GOOD_URL,
            role_type="internship",
            application_deadline=_today(),
        ).application_deadline
        == _today()
    )


def test_yesterday_is_not():
    with pytest.raises(ValueError):
        validate_application_deadline(_past())


def test_a_null_deadline_stays_valid():
    """Optional field: "no closing date stated" is a real answer, not an expired
    one."""
    assert validate_application_deadline(None) is None
    assert (
        OpportunitySurveyLinkSubmit(
            company_name="Acme", url=GOOD_URL, role_type="both"
        ).application_deadline
        is None
    )


def test_staff_create_refuses_a_past_deadline():
    """Both layers: the schema 422s it, and the service refuses it again below
    Pydantic for any caller that skipped model validation."""
    with pytest.raises(ValueError):
        OpportunityLinkCreate(
            alumni_id=1,
            company_name="Acme",
            url=GOOD_URL,
            role_type="internship",
            application_deadline=_past(),
        )

    async def _body():
        alum = _alum()
        session = _FakeSession(
            alumni=alum, alumni_rows=[alum], employment_rows=[_employment()]
        )
        payload = OpportunityLinkCreate.model_construct(
            alumni_id=1,
            is_own_company=False,
            company_name="Acme",
            url=GOOD_URL,
            location_city=None,
            location_state=None,
            location_country=None,
            role_type="internship",
            application_deadline=_past(),
            details=None,
        )
        with pytest.raises(ValueError):
            await service.create_link(session, payload, actor_user_id=9)
        assert session.links == []
        assert session.committed is False

    _run(_body())


def test_staff_create_accepts_a_future_deadline():
    async def _body():
        alum = _alum()
        session = _FakeSession(
            alumni=alum, alumni_rows=[alum], employment_rows=[_employment()]
        )
        result = await service.create_link(
            session,
            OpportunityLinkCreate(
                alumni_id=1,
                company_name="Acme",
                url=GOOD_URL,
                role_type="internship",
                application_deadline=_future(),
            ),
            actor_user_id=9,
        )
        assert result.application_deadline == _future()

    _run(_body())


def test_the_public_survey_submit_refuses_a_past_deadline(monkeypatch):
    with pytest.raises(ValueError):
        OpportunitySurveyLinkSubmit(
            company_name="Acme",
            url=GOOD_URL,
            role_type="internship",
            application_deadline=_past(),
        )

    async def _body():
        """And below Pydantic, on the path the public actually writes through —
        the whole batch is refused, nothing is staged."""
        monkeypatch.setattr(
            "app.services.opportunity_links.verify_survey_token", lambda token: 1
        )
        session = _FakeSession(alumni=_alum())
        item = OpportunitySurveyLinkSubmit.model_construct(
            is_own_company=False,
            company_name="Acme",
            url=GOOD_URL,
            location_city=None,
            location_state=None,
            location_country=None,
            role_type="internship",
            application_deadline=_past(),
            details=None,
        )
        payload = OpportunityLinkSubmitRequest.model_construct(links=[item])
        with pytest.raises(ValueError):
            await service.submit_links(session, "tok", payload)
        assert session.links == []
        assert session.committed is False

    _run(_body())


def test_the_public_route_422s_a_past_deadline(client):
    app.dependency_overrides.pop(get_current_db_user, None)
    rate_limit.reset()
    response = client.post(
        "/survey/respond/tok-deadline/links",
        json={
            "links": [
                {
                    "company_name": "Acme",
                    "url": GOOD_URL,
                    "role_type": "internship",
                    "application_deadline": _past().isoformat(),
                }
            ]
        },
    )
    assert response.status_code == 422


# --- and the subtle half: an EXISTING expired row stays editable --------------


def test_an_expired_row_is_still_editable_when_the_deadline_is_untouched():
    async def _body():
        """THE case this rule is written around. The dev data deliberately carries
        a row with a passed deadline. A reviewer must still be able to fix a typo
        in it — an expired posting with a wrong URL is worse than an expired
        posting — so the rule must not freeze the row by the mere passage of time.
        """
        expired = _link(application_deadline=_past(days=45))
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=expired, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(details="Corrected: the role is Provo-based."),
            actor_user_id=9,
        )
        assert result.details == "Corrected: the role is Provo-based."
        # The stale deadline survives the edit untouched — it is not silently
        # cleared, bumped, or refused.
        assert result.application_deadline == _past(days=45)

    _run(_body())


def test_an_expired_row_can_be_read():
    async def _body():
        expired = _link(application_deadline=_past(days=45))
        alum = _alum()
        session = _FakeSession(
            alumni_rows=[alum], employment_rows=[], link_rows=[expired], count=1
        )
        page = await service.list_links(session, _filters())
        assert page.items[0].application_deadline == _past(days=45)

    _run(_body())


def test_resending_the_same_expired_deadline_is_not_a_change():
    async def _body():
        """A client that PATCHes the whole object back — deadline included,
        unchanged — must not be refused. "Changed" means "differs from what is
        stored", not "was present in the body"."""
        expired = _link(application_deadline=_past(days=45))
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=expired, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(
                details="Same posting, tidier wording.",
                application_deadline=_past(days=45),
            ),
            actor_user_id=9,
        )
        assert result.application_deadline == _past(days=45)
        assert result.details == "Same posting, tidier wording."

    _run(_body())


def test_an_edit_that_sets_a_past_deadline_is_refused():
    async def _body():
        """Moving the deadline INTO the past is a change, and changes are checked."""
        link = _link(application_deadline=_future())
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=link, alumni_rows=[alum], employment_rows=[]
        )
        with pytest.raises(ValueError):
            await service.update_link(
                session,
                42,
                OpportunityLinkUpdate(application_deadline=_past()),
                actor_user_id=9,
            )
        # Nothing was written and nothing was committed.
        assert link.application_deadline == _future()
        assert session.committed is False

    _run(_body())


def test_an_edit_that_moves_an_expired_deadline_further_into_the_past_is_refused():
    async def _body():
        """The lenience is for LEAVING a stale deadline alone, not for editing one
        stale value into another."""
        expired = _link(application_deadline=_past(days=45))
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=expired, alumni_rows=[alum], employment_rows=[]
        )
        with pytest.raises(ValueError):
            await service.update_link(
                session,
                42,
                OpportunityLinkUpdate(application_deadline=_past(days=10)),
                actor_user_id=9,
            )

    _run(_body())


def test_an_expired_deadline_can_be_cleared_or_pushed_forward():
    async def _body():
        """The two repairs a reviewer actually needs on a stale row."""
        alum = _alum()

        cleared = _link(application_deadline=_past(days=45))
        session = _FakeSession(
            alumni=alum, link=cleared, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(application_deadline=None),
            actor_user_id=9,
        )
        assert result.application_deadline is None

        bumped = _link(application_deadline=_past(days=45))
        session = _FakeSession(
            alumni=alum, link=bumped, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(application_deadline=_future()),
            actor_user_id=9,
        )
        assert result.application_deadline == _future()

    _run(_body())


def test_setting_todays_date_on_an_edit_is_accepted():
    async def _body():
        """The inclusive boundary holds on the edit path too, not just on create."""
        link = _link(application_deadline=_future(days=60))
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=link, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.update_link(
            session,
            42,
            OpportunityLinkUpdate(application_deadline=_today()),
            actor_user_id=9,
        )
        assert result.application_deadline == _today()

    _run(_body())


def test_moderating_an_expired_link_still_works():
    async def _body():
        """Approve/reject never touch the deadline, so a stale row stays
        moderatable — rejecting a dead posting is the normal way it leaves the
        queue."""
        expired = _link(
            status="pending", source="survey", application_deadline=_past(days=45)
        )
        alum = _alum()
        session = _FakeSession(
            alumni=alum, link=expired, alumni_rows=[alum], employment_rows=[]
        )
        result = await service.moderate_link(
            session, 42, approve=False, actor_user_id=9
        )
        assert result.status == "rejected"
        assert result.application_deadline == _past(days=45)

    _run(_body())

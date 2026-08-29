"""The public survey's "email us directly" contact (#774).

`GET /survey/respond/{token}` is the app's one genuinely PUBLIC endpoint —
token-gated but unauthenticated — so these tests are as much about what is NOT
in the payload as about what is. Three things have to keep holding:

1. a row the engineer labelled for the survey reaches the respondent;
2. nothing else about that row does, and no OTHER contact does — the
   support-contacts privacy rule (`app/api/routes/support.py`) is relaxed for
   exactly one contact and exactly two fields;
3. a missing, unlabelled or broken contact serves `null` and the survey still
   loads. A stranger holding a valid token must always be able to reply.

No network and no DB: the session is a fake, `verify_survey_token` is stubbed,
and nothing here reads ambient credentials.
"""

import asyncio
import uuid

import pytest

from app.services import survey_email


class _FakeSettings:
    survey_token_secret = "unit-test-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"
    survey_usage_baseline_at = None
    survey_usage_baseline_today = 0
    survey_usage_baseline_month = 0


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _FakeSettings()
    monkeypatch.setattr(survey_email, "get_settings", lambda: settings)
    return settings


class _ContactSession:
    """Answers the single two-column support-contact select.

    `rows` are the `(name, email)` tuples the query WOULD return, already in the
    order the real `ORDER BY sort_order, support_contact_id` puts them; the
    resolver takes the first. `raises` makes `execute` blow up, standing in for
    an unreadable table.
    """

    def __init__(self, rows=(), *, raises=False):
        self._rows = list(rows)
        self.raises = raises
        self.rollbacks = 0

    async def execute(self, _stmt):
        if self.raises:
            raise RuntimeError("support_contacts is unreadable")
        rows = self._rows

        class _R:
            def first(self_inner):
                return rows[0] if rows else None

        return _R()

    async def rollback(self):
        self.rollbacks += 1


def _resolve(rows=(), **kw):
    return asyncio.run(survey_email.survey_support_contact(_ContactSession(rows, **kw)))


# ------------------------------------------------------ resolving the row ----


def test_configured_contact_is_returned():
    contact = _resolve([("Tanya Harmon", "tanya.harmon@byu.edu")])
    assert contact is not None
    assert contact.name == "Tanya Harmon"
    assert contact.email == "tanya.harmon@byu.edu"


def test_no_row_labelled_for_the_survey_is_none():
    # Nothing configured -> no contact. NOT a fallback address: a `mailto:` to
    # the wrong mailbox is worse than no button, because the respondent believes
    # they have reached a human and stops looking for another way.
    assert _resolve([]) is None


def test_only_the_two_exposed_fields_exist_on_the_model():
    # The whole point of selecting two columns instead of the ORM entity: there
    # is no id, no role_label and no sort_order to leak, even by accident.
    contact = _resolve([("Tanya Harmon", "tanya.harmon@byu.edu")])
    assert set(contact.model_dump()) == {"name", "email"}


def test_address_is_normalised_and_name_falls_back_to_it():
    # Whitespace and casing come off (rows seeded from `users.email` via SQL were
    # never through the Pydantic writer), and an empty name would otherwise render
    # as "Email " with nothing after it.
    contact = _resolve([("   ", "  Tanya.Harmon@BYU.edu  ")])
    assert contact.email == "tanya.harmon@byu.edu"
    assert contact.name == "tanya.harmon@byu.edu"


@pytest.mark.parametrize(
    "stored",
    [
        "",
        "   ",
        "not-an-address",
        "tanya@byu",  # no TLD
        "tanya@byu.edu, someone@byu.edu",  # a second recipient
        "tanya@byu.edu?subject=x",  # would append a mailto: parameter
        "tanya@byu.edu\nbcc: x@y.edu",  # header injection into the href
        "a" * 250 + "@byu.edu",  # over the length ceiling
    ],
)
def test_an_unusable_address_serves_none_not_a_broken_link(stored):
    # An address that cannot safely become a `mailto:` yields NO contact at all.
    # The cost of rejecting one is that the control does not render — the same
    # honest nothing as an unconfigured contact, never a link to nowhere.
    assert _resolve([("Tanya Harmon", stored)]) is None


def test_none_email_is_survived():
    assert _resolve([("Tanya Harmon", None)]) is None


def test_lookup_selects_only_name_and_email_ordered_by_sort_order():
    """The statement itself, not just its result.

    Reading the compiled SQL is what keeps the two guarantees honest: the select
    list can never widen into the rest of the row without this failing, and the
    row chosen can never depend on how Postgres happened to order them.
    """
    captured = {}

    class _Capture:
        async def execute(self, stmt):
            captured["sql"] = str(stmt)

            class _R:
                def first(self_inner):
                    return ("Tanya Harmon", "tanya.harmon@byu.edu")

            return _R()

    asyncio.run(survey_email.survey_support_contact(_Capture()))
    sql = captured["sql"].lower()
    select_list = sql.split("from")[0]
    assert "support_contacts.name" in select_list
    assert "support_contacts.email" in select_list
    for leaked in ("support_contact_id", "role_label", "sort_order", "created_at"):
        assert leaked not in select_list, f"{leaked} must not reach a public payload"
    # Chosen by the engineer's ordering, deterministically.
    assert "order by" in sql
    assert "support_contacts.sort_order" in sql.split("order by")[1]
    assert "limit" in sql
    # Matched loosely and case-insensitively on the label, so "Survey contact"
    # and "Survey (Tanya)" are the same row the engineer meant.
    assert "lower(support_contacts.role_label) like" in sql


def test_label_constant_is_the_control_surface():
    # Documented behaviour, asserted: the survey follows the "survey" label, so
    # relabelling a row in the engineer console is what re-points the button.
    assert survey_email.SURVEY_CONTACT_LABEL == "survey"
    assert survey_email._SURVEY_CONTACT_PATTERN == "%survey%"


# --------------------------------------------- inside the respond payload ----


def _alum():
    from types import SimpleNamespace

    return SimpleNamespace(
        alumni_id=7,
        archived=False,
        first_name="Jordan",
        middle_name=None,
        last_name="Avery",
        preferred_first_name=None,
        employment_status="Employed",
        linkedin_url=None,
        graduate_degree=None,
        graduate_school=None,
        graduate_graduation_year=None,
        spouse_first_name=None,
        spouse_last_name=None,
        other_designations=None,
        gender=None,
        marital_status=None,
        birth_date=None,
        citizenship=None,
        home_country=None,
    )


class _RespondSession:
    """alum, contact, job, engagement, then the support contact — in that order.

    `contact_raises` breaks only the LAST lookup, which is the scenario that
    matters: everything the alum came to confirm has already been read, and the
    question is whether the page still loads.
    """

    def __init__(self, rows, *, contact_raises=False):
        self._rows = list(rows)
        self._contact_raises = contact_raises
        self.rollbacks = 0

    async def execute(self, _stmt):
        last = not self._rows
        if last and self._contact_raises:
            raise RuntimeError("support_contacts is unreadable")
        row = self._rows.pop(0) if self._rows else None

        class _R:
            def scalar_one_or_none(self_inner):
                return row

            def first(self_inner):
                return row

        return _R()

    async def rollback(self):
        self.rollbacks += 1


def test_respond_info_carries_the_contact(fake_settings, monkeypatch):
    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    session = _RespondSession(
        [_alum(), None, None, None, ("Tanya Harmon", "tanya.harmon@byu.edu")]
    )
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info.support_contact.name == "Tanya Harmon"
    assert info.support_contact.email == "tanya.harmon@byu.edu"


def test_respond_info_is_none_when_nothing_is_configured(fake_settings, monkeypatch):
    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    session = _RespondSession([_alum(), None, None, None, None])
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info.support_contact is None
    # The survey itself is untouched — the alum can still confirm their info.
    assert info.first_name == "Jordan"
    assert info.fields["profile.employment_status"] == "Employed"


def test_survey_still_loads_when_the_contact_lookup_blows_up(
    fake_settings, monkeypatch
):
    """⚠️ The one that must never regress.

    A missing or misconfigured support-contacts table costs the survey a link.
    It must not cost a cohort their ability to reply."""
    monkeypatch.setattr(survey_email, "verify_survey_token", lambda _t: 7)
    session = _RespondSession([_alum(), None, None, None], contact_raises=True)
    info = asyncio.run(survey_email.get_respondent(session, "tok"))
    assert info is not None
    assert info.support_contact is None
    assert info.full_name == "Jordan Avery"
    assert info.fields  # the confirm page still has something to show
    # The poisoned transaction is rolled back so the session is returned clean.
    assert session.rollbacks == 1


# ---------------------------------------------------------------- route ------


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.core.database import get_session
    from app.main import app

    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _served(monkeypatch, contact):
    from app.schemas.survey import SurveyRespondInfo

    async def ok(session, token):
        return SurveyRespondInfo(
            first_name="Jane",
            full_name="Jane Doe",
            fields={"contact.city": "Provo"},
            support_contact=contact,
        )

    monkeypatch.setattr(survey_email, "get_respondent", ok)


def test_public_route_exposes_exactly_name_and_email(client, monkeypatch):
    from app.schemas.survey import SurveySupportContact

    _served(
        monkeypatch,
        SurveySupportContact(name="Tanya Harmon", email="tanya.harmon@byu.edu"),
    )
    body = client.get("/survey/respond/whatever").json()
    assert body["support_contact"] == {
        "name": "Tanya Harmon",
        "email": "tanya.harmon@byu.edu",
    }
    # Belt and braces on the serialized payload a stranger actually receives.
    serialized = str(body)
    for leaked in ("support_contact_id", "role_label", "sort_order"):
        assert leaked not in serialized


def test_public_route_serves_null_when_unconfigured(client, monkeypatch):
    _served(monkeypatch, None)
    resp = client.get("/survey/respond/whatever")
    assert resp.status_code == 200
    body = resp.json()
    # The key is PRESENT and null — the frontend renders nothing for it, and an
    # absent key would read as an older backend rather than as "none configured".
    assert "support_contact" in body
    assert body["support_contact"] is None


def test_route_stays_unauthenticated(client, monkeypatch):
    """No session, no Authorization header, still a 200.

    The contact rides on the survey's public payload; adding it must not have
    dragged an auth dependency onto the one route a stranger has to reach."""
    from app.schemas.survey import SurveySupportContact

    _served(
        monkeypatch,
        SurveySupportContact(name="Tanya Harmon", email="tanya.harmon@byu.edu"),
    )
    assert client.get("/survey/respond/whatever").status_code == 200


def test_support_contacts_list_route_still_requires_a_login(client):
    """The rule this change makes a narrow exception to is still in force.

    One contact on a token-gated survey page is the exception; the LIST stays
    behind a login, so staff names and emails are still not readable pre-auth.
    """
    assert uuid  # (imported for parity with the suite's other route modules)
    resp = client.get("/support-contacts")
    assert resp.status_code in (401, 403)

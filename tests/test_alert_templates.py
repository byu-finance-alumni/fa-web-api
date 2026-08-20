"""Owner-editable Slack alert wording, and the four things it must never do.

The owner wanted to write his own alert sentences instead of asking for a deploy
every time one read badly. That is a small feature with a large blast radius: the
text being edited is the text that tells him he is under attack, so a template
that cannot render, or that renders half-way, or that renders something it should
never have been able to say, is worse than the wording he did not like.

THE POINT OF THIS FILE IS THE NEGATIVE HALF, exactly like tests/test_login_auto_block.py.
The happy path is one test. The rest assert what an edit CANNOT do:

  1. IT CANNOT REACH AN ATTEMPTED EMAIL ADDRESS. No placeholder exists for them,
     none can be added by writing one in a template, and a field planted on the
     incident row cannot be pulled out through the wording.
     -> test_no_placeholder_name_can_reach_an_attempted_address
        test_a_template_cannot_name_a_fact_the_renderer_did_not_expose
        test_a_planted_address_cannot_be_pulled_out_through_a_template
        test_str_format_would_have_leaked_and_this_does_not

  2. IT CANNOT RAISE, AND IT CANNOT HALF-RENDER. Every hostile body falls back to
     the built-in default WHOLE; a brace never reaches a channel.
     -> test_no_template_can_make_rendering_raise
        test_a_broken_template_falls_back_to_todays_exact_wording
        test_a_partly_substituted_string_is_never_emitted
        test_the_last_resort_covers_the_case_the_default_cannot_render

  3. IT CANNOT COST THE MESSAGE. An unreadable table, a missing table, no
     database at all: the alert still goes out, saying what it said before this
     feature existed.
     -> test_an_unreadable_template_table_means_the_built_in_wording
        test_the_read_is_cached_so_a_sweep_does_not_read_once_per_report

  4. IT CANNOT BE DONE BY ANYONE BUT AN ENGINEER, and it cannot bypass the Slack
     escaping.
     -> test_only_an_engineer_can_read_or_change_the_wording
        test_a_templated_message_still_goes_through_the_slack_escaping

The fake store below is a real implementation of the four statements the service
issues, not a stub that returns a canned row: "save then read it back" has to
actually behave like one row per kind, or the customised/default distinction the
console shows would be untested.
"""

import asyncio
import datetime
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.core.errors import InvalidRequestError
from app.main import app
from app.models.audit import AuditLog
from app.schemas.auth import UserContext
from app.services import alert_templates, failure_alert, login_abuse

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "database"
    / "migrations"
    / "2026-08-20_alert_templates.sql"
)
SCHEMA = Path(__file__).resolve().parents[1] / "database" / "schema.sql"
RLS = Path(__file__).resolve().parents[1] / "database" / "rls_lockdown.sql"


# One real campaign from 2026-08-19, in the shape `evaluate` hands the renderers.
SEATTLE_INCIDENT = {
    "environment": "production",
    "ip_address": "159.26.103.94",
    "city": "Seattle",
    "region": "WA",
    "country": "United States",
    "attempt_count": 338,
    "distinct_email_count": 78,
    "pattern": "spraying: many addresses, a few passwords each",
    "started_at": datetime.datetime(2026, 8, 19, 14, 0, 0, tzinfo=datetime.UTC),
    "last_seen_at": datetime.datetime(2026, 8, 19, 14, 6, 0, tzinfo=datetime.UTC),
    "alert_sent_at": datetime.datetime(2026, 8, 19, 14, 0, 5, tzinfo=datetime.UTC),
    "abuse_incident_id": 7,
    "block_applied": True,
    "blocked_until": datetime.datetime(2026, 8, 19, 15, 0, 0, tzinfo=datetime.UTC),
}

OUTAGE_INCIDENT = {
    "incident_id": 3,
    "environment": "production",
    "started_at": datetime.datetime(2026, 8, 18, 9, 0, 0, tzinfo=datetime.UTC),
    "last_failure_at": datetime.datetime(2026, 8, 18, 9, 4, 0, tzinfo=datetime.UTC),
    "resolved_at": datetime.datetime(2026, 8, 18, 9, 6, 0, tzinfo=datetime.UTC),
    "failure_count": 17,
    "first_path": "/alumni/{alumni_id}",
    "last_path": "/dashboard/summary",
    "status_code": 500,
    "error_kind": "ProgrammingError",
}

# Today's wording, written out rather than imported, so a change to the defaults
# has to be made here too — deliberately, because these four strings are the
# product decision this whole feature is wrapped around.
TODAYS_OPENING = (
    "You are being attacked by 159.26.103.94 from Seattle, WA, United States. "
    "It is blocked and cannot sign in."
)
TODAYS_RESOLVED = (
    "The attack from 159.26.103.94 (Seattle, WA, United States) has stopped. "
    "338 attempts across 78 addresses over 6m 0s. Nothing got in."
)


# ============================================================== the fake store ==


class _Result:
    def __init__(self, rows):
        self._rows = [dict(r) for r in rows]

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeTemplateStore:
    """``alert_message_templates`` as a dict, driven by the real statements.

    ``fail`` makes every read raise, which is how the fail-safe tests reproduce
    "the migration has not been applied yet" and "the database blipped" without
    needing either.
    """

    def __init__(self, rows=None, fail=False):
        self.rows: dict[str, dict] = {}
        for key, body in (rows or {}).items():
            self.rows[key] = {
                "template_key": key,
                "body": body,
                "updated_at": datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC),
                "updated_by_user_id": 1,
            }
        self.fail = fail
        self.reads = 0
        self.sessions = 0


class FakeSession:
    """Answers the four statements in ``alert_templates`` and swallows the audit row."""

    def __init__(self, store: FakeTemplateStore):
        self.store = store
        self.added: list = []
        self.commits = 0

    async def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "DELETE FROM alert_message_templates" in sql:
            row = self.store.rows.pop(params["template_key"], None)
            return _Result([row] if row is not None else [])
        if "INSERT INTO alert_message_templates" in sql:
            row = {
                "template_key": params["template_key"],
                "body": params["body"],
                "updated_at": datetime.datetime(2026, 8, 20, tzinfo=datetime.UTC),
                "updated_by_user_id": params["actor_id"],
            }
            self.store.rows[params["template_key"]] = row
            return _Result([row])
        assert "FROM alert_message_templates" in sql, sql
        if self.store.fail:
            raise RuntimeError("relation alert_message_templates does not exist")
        self.store.reads += 1
        return _Result(sorted(self.store.rows.values(), key=lambda r: r["template_key"]))

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _fresh_template_cache():
    """The service caches per process, so a leaked cache would make one test's
    stored wording show up in the next one's alert."""
    alert_templates.reset_cache()
    yield
    alert_templates.reset_cache()


@pytest.fixture
def store(monkeypatch):
    """A fake ``alert_message_templates``, wired in as the app's session factory
    so ``alert_templates.load()`` — which deliberately opens its OWN session —
    reaches it."""
    data = FakeTemplateStore()

    def _factory():
        data.sessions += 1
        return FakeSession(data)

    monkeypatch.setattr(alert_templates.database, "SessionLocal", _factory)
    return data


def load(store_unused=None) -> dict:
    return asyncio.run(alert_templates.load())


# ================== 1. A TEMPLATE CANNOT REACH AN ATTEMPTED ADDRESS ============
#
# The rule the whole feature is subordinate to. The attempted addresses are
# unverified strings a stranger typed, some belong to real people, and a list of
# them in a Slack channel is both the attacker's scraped material republished and
# an enumeration oracle for everyone who can read the channel. Making the wording
# editable must not become a way to ask for them.


def test_no_placeholder_name_can_reach_an_attempted_address():
    """The tripwire, on the names themselves.

    ``{addresses}`` is allowed and is a COUNT — that is the number a reader acts
    on. Anything that even LOOKS like it might name the addresses goes red here
    with the reason attached, rather than shipping."""
    for kind in alert_templates.KINDS.values():
        for placeholder in kind.placeholders:
            for term in alert_templates._FORBIDDEN_PLACEHOLDER_TERMS:
                assert term not in placeholder.name, (
                    f"{kind.key}.{{{placeholder.name}}} looks like it could name "
                    "an attempted address. There is no placeholder for those and "
                    "there must never be one."
                )
    # ... and the one that sounds like it does is documented as a count.
    addresses = next(
        p
        for p in alert_templates.KINDS[
            alert_templates.SECURITY_ATTACK_OPENING
        ].placeholders
        if p.name == "addresses"
    )
    assert "count" in addresses.description.lower()
    assert addresses.example.isdigit()


def test_a_template_cannot_name_a_fact_the_renderer_did_not_expose():
    """Writing a placeholder does not create it. The editor refuses it up front,
    and the renderer refuses it again if a row arrives some other way."""
    for body in (
        "Attacked by {ip}, addresses tried: {emails}",
        "Attacked by {ip} ({attempted_emails})",
        "Attacked by {ip} {email}",
        "Attacked by {ip} {accounts}",
    ):
        with pytest.raises(InvalidRequestError):
            alert_templates.validate_body(
                alert_templates.SECURITY_ATTACK_OPENING, body
            )

    rendered = login_abuse.render_slack_summary(
        SEATTLE_INCIDENT,
        templates={
            alert_templates.SECURITY_ATTACK_OPENING: "Attacked by {ip}: {emails}"
        },
    )
    assert rendered == TODAYS_OPENING


def test_a_planted_address_cannot_be_pulled_out_through_a_template():
    """The structural half: the values dict is built by the renderer, and a key
    nobody declared is unreachable however the template is written.

    The incident row does not carry addresses today — this plants some anyway, so
    the test still holds if a future column, join, or `SELECT *` puts one there.
    """
    poisoned = SEATTLE_INCIDENT | {
        "email": "someone.real@byu.edu",
        "emails": ["a@byu.edu", "b@byu.edu"],
        "attempted_emails": "c@byu.edu",
    }
    values = login_abuse._template_values(poisoned)
    assert "byu.edu" not in repr(values)

    for body in (
        "{ip} tried {emails}",
        "{ip} tried {email}",
        "{ip} tried {attempted_emails}",
        "{ip} {location} {addresses}",
    ):
        out = login_abuse.render_slack_summary(
            poisoned,
            templates={alert_templates.SECURITY_ATTACK_OPENING: body},
        )
        assert "byu.edu" not in out
        assert "someone.real" not in out


def test_str_format_would_have_leaked_and_this_does_not():
    """Why substitution is a hand-written scan and not ``str.format``.

    ``str.format`` is a small expression language: attribute access reaches out
    of the value it was given and into the class, the module globals, and from
    there anywhere. A stored template is untrusted input, so it must not be
    handed to it — this asserts both that the attack is real and that it does not
    work here.
    """
    # The attack, proved against the thing we did NOT use.
    leaked = "{ip.__class__}".format(ip="159.26.103.94")
    assert "class" in leaked

    # The same string, through this module: refused at write time ...
    with pytest.raises(InvalidRequestError):
        alert_templates.validate_body(
            alert_templates.SECURITY_ATTACK_OPENING, "Attacked by {ip.__class__}"
        )
    # ... and discarded whole at render time.
    out = login_abuse.render_slack_summary(
        SEATTLE_INCIDENT,
        templates={
            alert_templates.SECURITY_ATTACK_OPENING: "Attacked by {ip.__class__}"
        },
    )
    assert out == TODAYS_OPENING
    assert "class" not in out


def test_the_renderers_expose_only_the_declared_placeholders():
    """The two value dicts and the declared placeholder lists agree.

    A value the renderer computes but no kind declares is dead weight; a
    placeholder a kind declares but no renderer supplies is a message that falls
    back to its default the first time it fires. Both are caught here rather than
    during an incident."""
    security = set(login_abuse._template_values(SEATTLE_INCIDENT))
    for key in (
        alert_templates.SECURITY_ATTACK_OPENING,
        alert_templates.SECURITY_ATTACK_RESOLVED,
    ):
        assert alert_templates.KINDS[key].placeholder_names <= security

    outage = set(failure_alert.outage_template_values(OUTAGE_INCIDENT))
    for key in (alert_templates.OUTAGE_OPENING, alert_templates.OUTAGE_RECOVERED):
        assert alert_templates.KINDS[key].placeholder_names <= outage


# ========================= 2. RENDERING IS TOTAL ==============================


HOSTILE = [
    "",
    "   ",
    "{",
    "}",
    "{ip",
    "ip}",
    "{}",
    "{0}",
    "{IP}",
    "{ip.__class__.__mro__}",
    "{ip!r}",
    "{ip:>200}",
    "{nope}",
    "x" * (alert_templates.MAX_BODY_CHARS + 1),
    "two\nlines",
    "nul\x00byte",
    "bidi‮override",
]


@pytest.mark.parametrize("body", HOSTILE)
def test_no_template_can_make_rendering_raise(body):
    """The contract the alerting path depends on.

    ``render`` runs on a request that is already failing, inside a module whose
    whole rule is that an alerter must never raise. Whatever is in the table, it
    returns a string."""
    unknown_location = {k: v for k, v in SEATTLE_INCIDENT.items()} | {
        "city": None,
        "region": None,
        "country": None,
    }
    for incident in (SEATTLE_INCIDENT, unknown_location, {}):
        out = login_abuse.render_slack_summary(
            incident, templates={alert_templates.SECURITY_ATTACK_OPENING: body}
        )
        assert isinstance(out, str)
        assert out.strip()


@pytest.mark.parametrize("body", HOSTILE)
def test_a_partly_substituted_string_is_never_emitted(body):
    """A brace in a Slack channel means the reader has to work out whether ``{ip}``
    is the attacker's address or a bug. Falling back whole is the only honest
    answer, so no rendered message may carry one."""
    out = login_abuse.render_slack_summary(
        SEATTLE_INCIDENT, templates={alert_templates.SECURITY_ATTACK_OPENING: body}
    )
    assert "{" not in out and "}" not in out


@pytest.mark.parametrize("body", HOSTILE)
def test_a_broken_template_falls_back_to_todays_exact_wording(body):
    """Not merely "something sensible" — the exact sentence the code shipped with,
    so a bad edit is invisible to the reader rather than being a second surprise
    on top of the incident."""
    out = login_abuse.render_slack_summary(
        SEATTLE_INCIDENT, templates={alert_templates.SECURITY_ATTACK_OPENING: body}
    )
    assert out == TODAYS_OPENING


def test_the_last_resort_covers_the_case_the_default_cannot_render():
    """The floor under the floor.

    Reachable only if the CALLER hands over a values dict missing something the
    built-in default names — a bug in a renderer, not in anyone's wording. The
    result is still a whole sentence, containing no placeholders at all."""
    out = alert_templates.render(alert_templates.SECURITY_ATTACK_OPENING, {})
    assert out == alert_templates.KINDS[
        alert_templates.SECURITY_ATTACK_OPENING
    ].last_resort
    assert "{" not in out

    for kind in alert_templates.KINDS.values():
        assert kind.last_resort.strip()
        assert "{" not in kind.last_resort and "}" not in kind.last_resort


def test_every_built_in_default_renders_from_its_real_caller():
    """The defaults and the renderers are checked against each other, so the
    last-resort path above stays unreachable in practice."""
    security = login_abuse._template_values(SEATTLE_INCIDENT)
    outage = failure_alert.outage_template_values(OUTAGE_INCIDENT)
    for key, values in (
        (alert_templates.SECURITY_ATTACK_OPENING, security),
        (alert_templates.SECURITY_ATTACK_RESOLVED, security),
        (alert_templates.OUTAGE_OPENING, outage),
        (alert_templates.OUTAGE_RECOVERED, outage),
    ):
        out = alert_templates.render(key, values)
        assert out != alert_templates.KINDS[key].last_resort
        assert "{" not in out and out.strip()


def test_todays_wording_is_unchanged_when_nothing_is_stored():
    """The regression that matters most: with an empty table, every message says
    exactly what it said before this feature existed."""
    assert login_abuse.render_slack_summary(SEATTLE_INCIDENT) == TODAYS_OPENING
    assert login_abuse.render_resolved(SEATTLE_INCIDENT)[2] == TODAYS_RESOLVED
    assert alert_templates.render(
        alert_templates.OUTAGE_OPENING,
        failure_alert.outage_template_values(OUTAGE_INCIDENT),
    ) == (
        "The API has been failing for long enough to be an incident. "
        "You will get one more email when it clears."
    )
    assert alert_templates.render(
        alert_templates.OUTAGE_RECOVERED,
        failure_alert.outage_template_values(OUTAGE_INCIDENT),
    ) == "The API is serving requests again. This incident is closed."


def test_an_unknown_geolocation_still_reads_as_a_sentence():
    """Why ``{location_phrase}`` carries its own leading space and can vanish."""
    nowhere = SEATTLE_INCIDENT | {"city": None, "region": None, "country": None}
    assert login_abuse.render_slack_summary(nowhere) == (
        "You are being attacked by 159.26.103.94. It is blocked and cannot sign in."
    )
    assert login_abuse.render_resolved(nowhere)[2].startswith(
        "The attack from 159.26.103.94 has stopped."
    )


def test_the_owners_own_wording_is_what_gets_sent():
    """The feature itself, once. Everything above is what it must not do."""
    out = login_abuse.render_slack_summary(
        SEATTLE_INCIDENT,
        templates={
            alert_templates.SECURITY_ATTACK_OPENING: (
                "WAKE UP: {ip}{location_phrase} is hammering the login "
                "({attempts} tries, {addresses} accounts). {action}"
            )
        },
    )
    assert out == (
        "WAKE UP: 159.26.103.94 from Seattle, WA, United States is hammering the "
        "login (338 tries, 78 accounts). It is blocked and cannot sign in."
    )


# ============================ 3. IT CANNOT COST THE MESSAGE ===================


def test_an_unreadable_template_table_means_the_built_in_wording(monkeypatch):
    """Migration not applied, table dropped, database blipped — all the same
    answer: no overrides, and the alert still goes out."""
    data = FakeTemplateStore(fail=True)
    monkeypatch.setattr(
        alert_templates.database, "SessionLocal", lambda: FakeSession(data)
    )
    assert asyncio.run(alert_templates.load()) == {}
    assert (
        login_abuse.render_slack_summary(SEATTLE_INCIDENT, templates=load())
        == TODAYS_OPENING
    )


def test_no_database_at_all_means_the_built_in_wording(monkeypatch):
    monkeypatch.setattr(alert_templates.database, "SessionLocal", None)
    assert asyncio.run(alert_templates.load()) == {}


def test_a_read_that_fails_after_a_good_one_keeps_the_last_good_value(monkeypatch):
    """Sticky rather than flapping: a blip mid-incident must not change the
    wording halfway through a campaign.

    The TTL is dropped to zero rather than the cache being cleared, because those
    are two different states — an EXPIRED cache still remembers the last good
    value, and remembering it is the whole point."""
    data = FakeTemplateStore({alert_templates.OUTAGE_OPENING: "It is broken."})
    monkeypatch.setattr(
        alert_templates.database, "SessionLocal", lambda: FakeSession(data)
    )
    monkeypatch.setattr(alert_templates, "_CACHE_TTL_SECONDS", 0.0)
    assert asyncio.run(alert_templates.load()) == {
        alert_templates.OUTAGE_OPENING: "It is broken."
    }
    data.fail = True
    assert asyncio.run(alert_templates.load()) == {
        alert_templates.OUTAGE_OPENING: "It is broken."
    }


def test_a_placeholder_may_be_repeated_or_left_out(store):
    """Nothing forces a template to use every placeholder, or to use one once."""
    out = login_abuse.render_slack_summary(
        SEATTLE_INCIDENT,
        templates={
            alert_templates.SECURITY_ATTACK_OPENING: "{ip} {ip} {ip}. Ignore it."
        },
    )
    assert out == "159.26.103.94 159.26.103.94 159.26.103.94. Ignore it."


def test_the_read_is_cached_so_a_sweep_does_not_read_once_per_report(store):
    """``sweep_quiet`` can close several campaigns in one pass. The cache is what
    keeps that one read rather than one per message — and the reason it is only
    an optimisation is that the read is on the alerting path in the first place."""
    for _ in range(5):
        load()
    assert store.reads == 1


def test_saving_publishes_the_new_wording_to_this_process_immediately(store):
    """An engineer who saves and then triggers a test alert must see the new
    words, not the ones cached thirty seconds ago."""
    session = FakeSession(store)
    load()
    asyncio.run(
        alert_templates.set_body(
            session,
            kind_key=alert_templates.SECURITY_ATTACK_OPENING,
            body="New words about {ip}.",
            actor_user_id=1,
        )
    )
    assert load()[alert_templates.SECURITY_ATTACK_OPENING] == "New words about {ip}."


# ============================== 4. VALIDATION =================================


def test_the_length_cap_is_enforced():
    """Slack answers an oversized payload with a 400 and the whole alert is lost,
    so a wordy template must be refused where someone can see the message."""
    key = alert_templates.SECURITY_ATTACK_OPENING
    assert alert_templates.validate_body(key, "x" * alert_templates.MAX_BODY_CHARS)
    with pytest.raises(InvalidRequestError, match="too long"):
        alert_templates.validate_body(key, "x" * (alert_templates.MAX_BODY_CHARS + 1))


@pytest.mark.parametrize(
    "body",
    [
        "two\nlines",
        "carriage\rreturn",
        "nul\x00byte",
        "esc\x1b[31m",
        "tab\tstop",
        "zero​width",
        "bidi‮override",
    ],
)
def test_control_and_invisible_characters_are_rejected(body):
    """These messages are one sentence. A newline, an escape sequence or a
    zero-width character in one is either a slip or an attempt to fake structure
    in the channel — and the same helper the alumni email/URL gates use decides,
    so the app has one definition of "invisible" rather than two."""
    with pytest.raises(InvalidRequestError):
        alert_templates.validate_body(
            alert_templates.SECURITY_ATTACK_OPENING, body + " {ip}"
        )


def test_an_empty_or_blank_body_is_rejected():
    for body in ("", "   ", " "):
        with pytest.raises(InvalidRequestError):
            alert_templates.validate_body(
                alert_templates.SECURITY_ATTACK_OPENING, body
            )


def test_a_body_that_renders_as_nothing_falls_back_rather_than_posting_nothing():
    """The one case a write-time check cannot catch, so the render-time one must.

    ``{location_phrase}`` legitimately expands to nothing when the edge gave us
    no geolocation, so a template made only of it VALIDATES (against the example
    values, where the location is known) and then renders empty on the day it
    fires. An empty Slack post is a rejected payload and a silent alert, so the
    built-in default takes over — the same fallback every other unusable template
    gets."""
    body = "{location_phrase}"
    assert alert_templates.validate_body(
        alert_templates.SECURITY_ATTACK_OPENING, body
    ) == body

    nowhere = SEATTLE_INCIDENT | {"city": None, "region": None, "country": None}
    assert login_abuse.render_slack_summary(
        nowhere, templates={alert_templates.SECURITY_ATTACK_OPENING: body}
    ) == "You are being attacked by 159.26.103.94. It is blocked and cannot sign in."


def test_a_placeholder_from_another_message_is_rejected():
    """Each kind declares its own list. ``{status_code}`` is real, but not on a
    security line, and a template naming it would silently lose it."""
    with pytest.raises(InvalidRequestError, match="status_code"):
        alert_templates.validate_body(
            alert_templates.SECURITY_ATTACK_OPENING, "{ip} returned {status_code}"
        )


def test_an_unknown_message_kind_is_rejected():
    with pytest.raises(InvalidRequestError):
        alert_templates.validate_body("no_such_message", "hello")


def test_the_error_says_which_placeholders_are_available():
    """The engineer typing this has no other way to find out."""
    with pytest.raises(InvalidRequestError) as exc:
        alert_templates.validate_body(
            alert_templates.SECURITY_ATTACK_OPENING, "{nope}"
        )
    assert "{ip}" in exc.value.message
    assert "{addresses}" in exc.value.message


# =========================== 5. THE ENGINEER GATE =============================


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def console(store):
    """The engineer console, wired to the fake template table."""
    session = FakeSession(store)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        yield client, session, store
    app.dependency_overrides.clear()


@pytest.mark.parametrize("role", ["super_admin", "admin", "staff", "view_only"])
def test_only_an_engineer_can_read_or_change_the_wording(console, role):
    """These sentences are the security channel's contents. Super_admin is
    excluded too — it is the user-administration role, and this is alerting."""
    client, _session, _store = console
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)

    assert client.get("/admin/alert-templates").status_code == 403
    assert (
        client.put(
            f"/admin/alert-templates/{alert_templates.SECURITY_ATTACK_OPENING}",
            json={"body": "hi {ip}"},
        ).status_code
        == 403
    )
    assert (
        client.delete(
            f"/admin/alert-templates/{alert_templates.SECURITY_ATTACK_OPENING}"
        ).status_code
        == 403
    )


def test_an_engineer_sees_every_message_with_its_default_and_a_preview(console):
    client, _session, _store = console

    body = client.get("/admin/alert-templates").json()

    assert [i["key"] for i in body["items"]] == list(alert_templates.KINDS)
    for item in body["items"]:
        assert item["customized"] is False
        assert item["body"] == item["default_body"]
        assert "{" not in item["preview"]
        assert item["placeholders"]
        assert item["max_chars"] == alert_templates.MAX_BODY_CHARS


def test_saving_then_resetting_walks_the_wording_there_and_back(console):
    client, session, _store = console
    key = alert_templates.SECURITY_ATTACK_OPENING

    saved = client.put(
        f"/admin/alert-templates/{key}", json={"body": "WAKE UP: {ip}. {action}"}
    )
    assert saved.status_code == 200
    assert saved.json()["customized"] is True
    assert saved.json()["preview"].startswith("WAKE UP: 159.26.103.94.")
    assert (
        login_abuse.render_slack_summary(SEATTLE_INCIDENT, templates=load())
        == "WAKE UP: 159.26.103.94. It is blocked and cannot sign in."
    )

    restored = client.delete(f"/admin/alert-templates/{key}")
    assert restored.status_code == 200
    assert restored.json()["customized"] is False
    assert login_abuse.render_slack_summary(SEATTLE_INCIDENT, templates=load()) == (
        TODAYS_OPENING
    )

    actions = [a.action_type for a in session.added if isinstance(a, AuditLog)]
    assert "update_alert_template" in actions
    assert "reset_alert_template" in actions


def test_the_audit_row_names_the_message_and_not_the_prose(console):
    """An audit row is not the place to keep a copy of the wording — the table is
    one SELECT away, and the log should stay a log."""
    client, session, _store = console
    key = alert_templates.OUTAGE_OPENING
    client.put(f"/admin/alert-templates/{key}", json={"body": "It is on fire."})

    row = next(
        a
        for a in session.added
        if isinstance(a, AuditLog) and a.action_type == "update_alert_template"
    )
    assert row.field_name == key
    assert "on fire" not in str(row.new_value)


def test_a_bad_body_is_a_422_with_something_to_act_on(console):
    client, _session, store = console
    key = alert_templates.SECURITY_ATTACK_OPENING

    for body in ("", "x" * 900, "{emails}", "two\nlines", "{ip"):
        response = client.put(f"/admin/alert-templates/{key}", json={"body": body})
        assert response.status_code == 422, body
    assert store.rows == {}


def test_resetting_something_already_default_is_a_clean_404(console):
    client, _session, _store = console
    response = client.delete(
        f"/admin/alert-templates/{alert_templates.SECURITY_ATTACK_OPENING}"
    )
    assert response.status_code == 404


def test_an_unknown_message_name_is_a_404_not_a_new_row(console):
    client, _session, store = console
    assert (
        client.put(
            "/admin/alert-templates/not_a_message", json={"body": "hello"}
        ).status_code
        == 404
    )
    assert store.rows == {}


def test_an_unexpected_field_is_refused_rather_than_silently_ignored(console):
    """``extra="forbid"``: a typo'd field must not look like a successful save."""
    client, _session, _store = console
    response = client.put(
        f"/admin/alert-templates/{alert_templates.SECURITY_ATTACK_OPENING}",
        json={"body": "hi {ip}", "kind": "security"},
    )
    assert response.status_code == 422


def test_the_save_is_rate_limited_and_the_reset_is_not(console):
    """The brake belongs on the direction that does the damage. Reset is the
    recovery path from wording that broke the message, and limiting the way back
    is the same lockout-shaped mistake as limiting maintenance-mode disable."""
    client, _session, _store = console
    key = alert_templates.OUTAGE_RECOVERED

    codes = [
        client.put(f"/admin/alert-templates/{key}", json={"body": "ok {environment}"})
        .status_code
        for _ in range(40)
    ]
    assert 429 in codes

    # The reset still works while the save budget is spent.
    assert client.delete(f"/admin/alert-templates/{key}").status_code == 200


# ================== 6. THE OUTPUT STILL GOES THROUGH SLACK ESCAPING ===========


def test_a_templated_message_still_goes_through_the_slack_escaping():
    """A single ``<`` starts a link or a mention and eats the rest of the line, so
    an alert that silently loses its own contents is worse than no alert. The
    escaping already existed; making the wording editable must not route around
    it."""
    summary = login_abuse.render_slack_summary(
        SEATTLE_INCIDENT,
        templates={
            alert_templates.SECURITY_ATTACK_OPENING: "<!channel> {ip} & friends"
        },
    )
    payload = failure_alert.render_slack(
        "subject", "intro", [], purpose=failure_alert.SECURITY, summary=summary
    )
    block = payload["blocks"][0]["text"]["text"]
    assert "&lt;!channel&gt;" in block
    assert "&amp;" in block
    assert "<!channel>" not in block


# ========================= 7. THE MIGRATION AND THE SCHEMA ====================


def _seeded() -> dict[str, str]:
    sql = MIGRATION.read_text(encoding="utf-8")
    block = sql[sql.index("INSERT INTO alert_message_templates") :]
    block = block[: block.index("ON CONFLICT")]
    return dict(re.findall(r"\('([a-z_]+)',\s*'([^']*)'\)", block))


def test_the_migration_seeds_exactly_the_built_in_defaults():
    """The seed exists so the table is self-describing, which is only true while
    it agrees with the code. A default edited in Python and not mirrored here
    would make a freshly migrated database say something different from an
    upgraded one — a wording difference nobody would ever think to look for."""
    seeded = _seeded()
    assert set(seeded) == set(alert_templates.KINDS)
    for key, kind in alert_templates.KINDS.items():
        assert seeded[key] == kind.default, key


def test_the_seeded_defaults_are_valid_by_the_apis_own_rules():
    """The migration writes rows the endpoint would have refused? Then one of the
    two is wrong."""
    for key, body in _seeded().items():
        assert alert_templates.validate_body(key, body) == body


def test_the_migration_is_safe_to_re_run_and_cannot_clobber_an_edit():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS alert_message_templates" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_templates_key" in sql
    assert "ON CONFLICT (template_key) DO NOTHING" in sql


def test_the_new_table_is_locked_down_and_declared_in_the_schema():
    """Supabase auto-exposes every public table through the Data API with the
    publishable key that ships in the frontend bundle. RLS with no policies is the
    only thing between that key and this table."""
    assert "CREATE TABLE alert_message_templates" in SCHEMA.read_text(encoding="utf-8")
    assert (
        "ALTER TABLE alert_message_templates ENABLE ROW LEVEL SECURITY"
        in RLS.read_text(encoding="utf-8")
    )
    assert (
        "ALTER TABLE alert_message_templates ENABLE ROW LEVEL SECURITY"
        in MIGRATION.read_text(encoding="utf-8")
    )


def test_the_database_carries_the_same_two_limits_the_api_does():
    """The API is not the only thing that can write a row — psql, a restored
    backup, a future endpoint. The constraints are the layer that holds then."""
    for text_ in (MIGRATION.read_text(encoding="utf-8"), SCHEMA.read_text(encoding="utf-8")):
        assert f"BETWEEN 1 AND {alert_templates.MAX_BODY_CHARS}" in text_
        assert "[^[:cntrl:]]" in text_

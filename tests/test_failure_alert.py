"""API failure alerting: one email per incident, not one per error (#444).

The requirement this file exists to prove is a NEGATIVE one — a flood of 500s
must not produce a flood of emails, and a single blip must not page anyone — so
almost every assertion here counts emails rather than checking one happened.

HOW THE DATABASE IS FAKED, AND WHAT THAT DOES AND DOES NOT PROVE.
``FakeIncidentTable`` below is a real implementation of the ``service_incidents``
semantics the service depends on: at most ONE open row per environment, and
"claim by setting a column that must still be NULL". The service's real
statements are dispatched to it by identity, so the tests exercise the actual
call sequence, the actual parameters and the actual decisions.

What that CANNOT prove is that Postgres provides those semantics — that comes
from the partial unique index ``uq_service_incidents_open`` and from
``UPDATE ... WHERE <col> IS NULL RETURNING`` under READ COMMITTED. The last test
in this file therefore asserts the migration still defines that index, because
without it the feature silently degrades to one email per serverless instance
and every test above would still pass.

Concurrency is modelled by ``asyncio.gather`` over separate sessions sharing one
table. The fake's operations contain no awaits, which is exactly the atomicity a
single SQL statement has.
"""

import asyncio
import datetime
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import failure_monitor
from app.services import failure_alert
from app.services.failure_alert import FailureSignal

# --------------------------------------------------------------- the fake DB --


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return dict(self._rows[0]) if self._rows else None


class FakeIncidentTable:
    """An in-memory ``service_incidents`` honouring the constraints that matter.

    Shared by every :class:`FakeSession`, the way one table is shared by every
    serverless instance.
    """

    def __init__(self):
        self.rows: list[dict] = []
        self.next_id = 1
        self.now = datetime.datetime(2026, 8, 18, 18, 0, 0, tzinfo=datetime.UTC)

    def advance(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)

    def _open_row(self, environment):
        for row in self.rows:
            if row["environment"] == environment and row["resolved_at"] is None:
                return row
        return None

    # -- the four statements ---------------------------------------------------

    def open(self, params):
        # The partial unique index: an INSERT cannot land while another row for
        # this environment is open.
        if self._open_row(params["environment"]) is not None:
            return []
        row = {
            "incident_id": self.next_id,
            "environment": params["environment"],
            "started_at": self.now,
            "last_failure_at": self.now,
            "failure_count": 1,
            "first_path": params["path"],
            "last_path": params["path"],
            "status_code": params["status_code"],
            "error_kind": params["error_kind"],
            "alert_sent_at": None,
            "recovery_sent_at": None,
            "resolved_at": None,
        }
        self.next_id += 1
        self.rows.append(row)
        return [{"incident_id": row["incident_id"]}]

    def bump(self, params):
        row = self._open_row(params["environment"])
        if row is None:
            return []
        row["failure_count"] += 1
        row["last_failure_at"] = self.now
        row["last_path"] = params["path"]
        row["status_code"] = params["status_code"]
        row["error_kind"] = params["error_kind"]
        return [{"incident_id": row["incident_id"]}]

    def claim_alert(self, params):
        for row in self.rows:
            if row["incident_id"] != params["incident_id"]:
                continue
            sustained = row["failure_count"] >= params["min_failures"] and (
                row["started_at"]
                <= self.now - datetime.timedelta(seconds=params["min_seconds"])
            )
            # The claim: only a row whose alert_sent_at is STILL NULL may be won.
            if row["alert_sent_at"] is None and row["resolved_at"] is None and sustained:
                row["alert_sent_at"] = self.now
                return [dict(row)]
        return []

    def close_if_quiet(self, params):
        row = self._open_row(params["environment"])
        if row is None:
            return []
        quiet_since = self.now - datetime.timedelta(seconds=params["quiet_seconds"])
        if row["last_failure_at"] >= quiet_since:
            return []
        row["resolved_at"] = self.now
        if row["alert_sent_at"] is not None:
            row["recovery_sent_at"] = self.now
        return [dict(row)]


class FakeSession:
    """One "serverless instance"'s session onto the shared fake table."""

    def __init__(self, table: FakeIncidentTable):
        self.table = table
        self.commits = 0

    async def execute(self, statement, params=None):
        params = params or {}
        if statement is failure_alert._SQL_OPEN:
            return _Result(self.table.open(params))
        if statement is failure_alert._SQL_BUMP:
            return _Result(self.table.bump(params))
        if statement is failure_alert._SQL_CLAIM_ALERT:
            return _Result(self.table.claim_alert(params))
        if statement is failure_alert._SQL_CLOSE_IF_QUIET:
            return _Result(self.table.close_if_quiet(params))
        raise AssertionError(f"unexpected statement: {statement}")

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ------------------------------------------------------------------ fixtures --


class _FakeSettings:
    environment = "production"
    resend_api_key = "re_test_key"
    alert_from_name = "BYU Finance Alumni API"
    alert_recipients = ["engineer@example.edu"]
    alert_sender = "alerts@example.edu"
    # Email-only, which is what every test in THIS file is about. Slack delivery
    # is the same alert through the other channels and is covered end to end in
    # tests/test_slack_alerts.py (including which channel each kind routes to);
    # leaving both unset here keeps these assertions counting Resend payloads and
    # nothing else.
    slack_webhook = None
    slack_security_webhook = None


@pytest.fixture
def sent(monkeypatch):
    """Capture every Resend payload the alerter posts, without a network call."""
    posted: list[dict] = []

    async def fake_post(url, *, api_key, payload, timeout):
        posted.append(payload)
        return SimpleNamespace(is_success=True, status_code=200)

    monkeypatch.setattr(failure_alert.mailer, "post_json", fake_post)
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _FakeSettings())
    failure_alert.reset_degraded_state()
    return posted


@pytest.fixture
def table(monkeypatch):
    """A shared fake ``service_incidents`` wired in as the app's session factory."""
    tbl = FakeIncidentTable()
    monkeypatch.setattr(
        failure_alert.database, "SessionLocal", lambda: FakeSession(tbl)
    )
    return tbl


def _signal(path="/alumni", status=500, kind="ProgrammingError"):
    return FailureSignal(path=path, status_code=status, error_kind=kind)


async def _fail(table, times=1, gap=15):
    """Report ``times`` failures, ``gap`` seconds apart, as the middleware would."""
    for _ in range(times):
        await failure_alert.note_failure(_signal(), process_sustained=False)
        table.advance(gap)


def _subjects(payloads):
    return [p["subject"] for p in payloads]


# ----------------------------------------------------- one alert per incident --


def test_a_single_blip_never_pages_anyone(sent, table):
    """One 500 opens an incident and emails nobody. This is half the feature: a
    pager that fires on every transient error is a pager people mute."""
    asyncio.run(failure_alert.note_failure(_signal(), process_sustained=False))

    assert sent == []
    assert len(table.rows) == 1
    assert table.rows[0]["alert_sent_at"] is None


def test_alerting_needs_both_a_count_and_a_duration(sent, table):
    """A burst of errors in one instant is one bad request fanned out, not an
    outage — so the count alone must not trip the alert."""
    asyncio.run(_fail(table, times=10, gap=0))
    assert sent == []

    # Same incident, now genuinely sustained past the time threshold.
    table.advance(failure_alert.ALERT_MIN_SECONDS + 1)
    asyncio.run(_fail(table, times=1, gap=0))
    assert len(sent) == 1


def test_a_flood_of_errors_produces_exactly_one_email(sent, table):
    """The requirement, stated as a test: several minutes of continuous failure
    reported over and over yields ONE opening email."""
    asyncio.run(_fail(table, times=40, gap=15))  # ten minutes of failing

    assert len(sent) == 1
    assert "API failing" in sent[0]["subject"]
    assert table.rows[0]["failure_count"] == 40


def test_concurrent_instances_send_exactly_one_email(sent, table):
    """The serverless case: twenty instances observe the same outage at the same
    moment, each with its own session and no shared memory. Exactly one of them
    may open the incident, and exactly one may claim the alert."""

    async def scenario():
        # Get the incident open and past both thresholds first.
        await failure_alert.note_failure(_signal(), process_sustained=False)
        table.advance(failure_alert.ALERT_MIN_SECONDS + 1)
        await asyncio.gather(
            *(
                failure_alert.note_failure(_signal(), process_sustained=False)
                for _ in range(20)
            )
        )

    asyncio.run(scenario())

    assert len(sent) == 1
    assert len(table.rows) == 1, "twenty instances must not open twenty incidents"


def test_no_second_email_however_long_the_outage_runs(sent, table):
    """Once alerted, the incident goes quiet no matter how much more breaks."""
    asyncio.run(_fail(table, times=6, gap=15))
    assert len(sent) == 1

    asyncio.run(_fail(table, times=200, gap=15))
    assert len(sent) == 1


# ------------------------------------------------------------------ recovery --


def _open_and_alert(table):
    asyncio.run(_fail(table, times=6, gap=15))


def test_recovery_sends_one_email_and_only_one(sent, table):
    """Today's incident should produce exactly two emails: one opening, one
    clearing — including when several instances notice the recovery at once."""
    _open_and_alert(table)
    assert len(sent) == 1

    table.advance(failure_alert._RECOVERY_QUIET_SECONDS + 1)

    async def everyone_probes():
        await asyncio.gather(*(failure_alert.note_success() for _ in range(5)))

    asyncio.run(everyone_probes())
    assert len(sent) == 2
    assert "API recovered" in sent[1]["subject"]

    # And it stays closed: later probes have nothing to report.
    table.advance(600)
    asyncio.run(failure_alert.note_success())
    assert len(sent) == 2


def test_a_blip_closes_silently(sent, table):
    """An incident that never paged anyone has nothing to clear, so recovery must
    not send a 'recovered' email for an alert nobody received."""
    asyncio.run(failure_alert.note_failure(_signal(), process_sustained=False))
    table.advance(failure_alert._RECOVERY_QUIET_SECONDS + 1)
    asyncio.run(failure_alert.note_success())

    assert sent == []
    assert table.rows[0]["resolved_at"] is not None


def test_recovery_waits_for_a_quiet_period(sent, table):
    """A flapping outage (fail, succeed, fail) must stay ONE incident rather than
    becoming an open/close/open/close email chain."""
    _open_and_alert(table)
    table.advance(10)  # well inside the quiet window

    asyncio.run(failure_alert.note_success())
    assert len(sent) == 1
    assert table.rows[0]["resolved_at"] is None


def test_the_next_outage_alerts_again(sent, table):
    """Dedup is per INCIDENT, not "one alert ever". A resolved incident must not
    silence the next one — that would be a monitoring system that stops working
    after its first success."""
    _open_and_alert(table)
    table.advance(failure_alert._RECOVERY_QUIET_SECONDS + 1)
    asyncio.run(failure_alert.note_success())
    assert len(sent) == 2

    asyncio.run(_fail(table, times=6, gap=15))
    assert len(sent) == 3
    assert "API failing" in sent[2]["subject"]
    assert len(table.rows) == 2


def test_a_stale_incident_is_reaped_so_the_next_outage_is_not_swallowed(sent, table):
    """An incident nobody closed (an outage at night, then no traffic at all) must
    not block tomorrow's alert. The failure path reaps it, and pays the recovery
    email it owed."""
    _open_and_alert(table)
    assert len(sent) == 1

    # Hours pass with no requests of any kind, then a fresh outage starts.
    table.advance(failure_alert._STALE_INCIDENT_SECONDS + 60)
    asyncio.run(_fail(table, times=6, gap=15))

    assert len(sent) == 3, _subjects(sent)
    assert "API failing" in sent[0]["subject"]
    assert "API recovered" in sent[1]["subject"]  # the overdue clear
    assert "API failing" in sent[2]["subject"]  # the new incident
    assert len(table.rows) == 2


# ------------------------------------------- the alerter must not amplify ----


def test_a_failing_send_neither_raises_nor_retries(monkeypatch, table):
    """The alert runs on a request that is already broken. If Resend is down too,
    that must cost one logged line — not an exception, and not a retry loop
    hammering a third party during an outage."""
    attempts = []

    async def exploding_post(url, *, api_key, payload, timeout):
        attempts.append(payload)
        raise RuntimeError("resend unreachable")

    monkeypatch.setattr(failure_alert.mailer, "post_json", exploding_post)
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _FakeSettings())

    asyncio.run(_fail(table, times=6, gap=15))  # must not raise

    assert len(attempts) == 1, "a failed alert must not be retried"
    # The claim stands, so continuing failure cannot turn into repeated attempts.
    asyncio.run(_fail(table, times=10, gap=15))
    assert len(attempts) == 1


def test_a_rejected_send_is_not_retried(monkeypatch, table):
    """Same rule for a Resend 4xx (e.g. the dev domain being unverified)."""
    attempts = []

    async def rejecting_post(url, *, api_key, payload, timeout):
        attempts.append(payload)
        return SimpleNamespace(is_success=False, status_code=403)

    monkeypatch.setattr(failure_alert.mailer, "post_json", rejecting_post)
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _FakeSettings())

    asyncio.run(_fail(table, times=20, gap=15))
    assert len(attempts) == 1


def test_alerting_is_off_unless_it_is_configured(monkeypatch, table):
    """No recipients configured = no alerting at all, and no database work. This
    is the state local dev, CI and every preview deployment run in."""
    monkeypatch.setattr(
        failure_alert,
        "get_settings",
        lambda: SimpleNamespace(
            environment="development",
            resend_api_key="re_test_key",
            alert_recipients=[],
            alert_sender=None,
            alert_from_name="x",
            slack_webhook=None,
            slack_security_webhook=None,
            slack_submission_webhook=None,
        ),
    )
    asyncio.run(_fail(table, times=20, gap=15))
    assert table.rows == []


# ------------------------------------------------- database itself unreachable --


def test_degraded_alerting_when_the_dedup_store_is_down(sent, monkeypatch):
    """If the DATABASE is the outage, the dedup store is unreachable — the exact
    failure that took the site down on 2026-08-18. Staying silent then would be
    the worst possible behaviour, so the alerter falls back to per-process dedup
    with a long cooldown: bounded, and labelled as degraded so the reader knows
    why a duplicate might arrive."""
    monkeypatch.setattr(failure_alert.database, "SessionLocal", None)

    asyncio.run(failure_alert.note_failure(_signal(), process_sustained=True))
    assert len(sent) == 1
    assert "degraded" in sent[0]["subject"]

    # Bounded: the flood behind it is still one email.
    for _ in range(50):
        asyncio.run(failure_alert.note_failure(_signal(), process_sustained=True))
    assert len(sent) == 1


def test_degraded_alerting_fires_on_a_freshly_booted_instance(sent, monkeypatch):
    """A COLD instance must still be able to send the first degraded alert.

    `time.monotonic()` counts from an arbitrary origin -- on Linux, machine boot
    -- so seconds after start it returns a small number. When the cooldown
    baseline was initialised to 0.0, `now - 0.0 < 1800` was TRUE on any instance
    younger than half an hour, and the alert was dropped as a duplicate of one
    that had never been sent.

    That is the worst possible place for the bug: on Vercel every cold start is a
    fresh instance, so the suppressed alert is the first one after a deploy, or
    the one where the database went down and took the durable dedup store with
    it. It passed on a developer machine with 28 hours of uptime and failed in
    CI, whose runners boot seconds before the tests.
    """
    monkeypatch.setattr(failure_alert.time, "monotonic", lambda: 3.0)

    asyncio.run(failure_alert.note_failure(_signal(), process_sustained=True))

    assert len(sent) == 1
    assert "degraded" in sent[0]["subject"]


def test_degraded_alerting_still_ignores_a_blip(sent, monkeypatch):
    """A single failure plus a flaky connection is not an incident."""
    monkeypatch.setattr(failure_alert.database, "SessionLocal", None)

    asyncio.run(failure_alert.note_failure(_signal(), process_sustained=False))
    assert sent == []


# ------------------------------------------------------------ no PII, no leak --


def test_route_templates_strip_ids_and_tokens():
    """Everything the monitor records is emailed off-platform, so an alumni id
    (a pointer to a person's record) and a survey token (a live credential) must
    never survive the trip out of ``route_template``."""

    def req(path, path_params=None):
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"q=Smith",
            "headers": [],
            "path_params": path_params or {},
            "scheme": "http",
            "server": ("test", 80),
        }
        from fastapi import Request

        return Request(scope)

    # Matched route: the param is named.
    assert (
        failure_monitor.route_template(req("/alumni/8421", {"alumni_id": 8421}))
        == "/alumni/{alumni_id}"
    )
    # Unmatched (an exception before routing finished): the scrubber still fires.
    assert failure_monitor.route_template(req("/alumni/8421")) == "/alumni/{id}"
    # A signed survey token is long and opaque — scrubbed either way.
    # Assembled rather than written as one literal: a realistic-looking
    # signed blob trips the secret scanner's generic-api-key entropy rule,
    # and a test fixture is not worth an allowlist entry that would also
    # blind the scanner to a real key landing in this file later.
    token = ".".join(["eyJhbGciOiJIUzI1NiJ9", "abcdefghijklmnopqrstuvwxyz0123456789"])
    assert failure_monitor.route_template(req(f"/survey/{token}")) == "/survey/{id}"
    assert token not in failure_monitor.route_template(req(f"/survey/{token}"))
    # A UUID (e.g. a Supabase user id) is not a route word.
    assert (
        failure_monitor.route_template(req("/admin/users/9f1c2b7e-1111-2222-3333-444455556666"))
        == "/admin/users/{id}"
    )
    # Ordinary route words survive, so the email is still useful.
    assert failure_monitor.route_template(req("/dashboard/stats")) == "/dashboard/stats"


def test_the_alert_body_carries_no_free_text_from_the_failure(sent, table):
    """The email may name a route template, a status code and an exception CLASS.
    Never an exception message — those quote row values."""
    asyncio.run(_fail(table, times=6, gap=15))
    body = sent[0]["text"]

    assert "/alumni" in body
    assert "ProgrammingError" in body
    # A query string is never captured at all, so it cannot appear.
    assert "Smith" not in body
    assert "@" not in body


# ------------------------------------------------------------- the middleware --


def _mini_app():
    """A miniature app carrying only the failure middleware, so the failure paths
    can be exercised without inventing broken routes on the real one."""
    app = FastAPI()
    app.middleware("http")(failure_monitor.failure_alert_middleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/missing")
    async def missing():
        from fastapi import HTTPException

        raise HTTPException(status_code=404)

    return app


@pytest.fixture
def observed(monkeypatch):
    """Record what the middleware decides, without any database or email."""
    calls = {"failures": [], "successes": 0}

    async def note_failure(signal, *, process_sustained):
        calls["failures"].append(signal)

    async def note_success():
        calls["successes"] += 1

    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: True)
    monkeypatch.setattr(failure_alert, "note_failure", note_failure)
    monkeypatch.setattr(failure_alert, "note_success", note_success)
    failure_monitor.reset()
    return calls


def test_middleware_records_a_5xx_and_leaves_the_response_alone(observed):
    client = TestClient(_mini_app(), raise_server_exceptions=False)
    response = client.get("/boom")

    assert response.status_code == 500
    assert len(observed["failures"]) == 1
    assert observed["failures"][0].error_kind == "RuntimeError"
    assert observed["failures"][0].path == "/boom"


def test_middleware_never_swallows_the_original_exception(observed):
    client = TestClient(_mini_app())
    with pytest.raises(RuntimeError, match="kaboom"):
        client.get("/boom")
    assert len(observed["failures"]) == 1


def test_a_client_error_is_not_a_failure(observed):
    """404/401/403/422/429 are the CLIENT being wrong. Paging on those would mean
    paging on every bot that probes the API."""
    client = TestClient(_mini_app())
    client.get("/missing")

    assert observed["failures"] == []


def test_a_healthy_request_costs_at_most_one_sampled_probe(observed, monkeypatch):
    """Success is sampled, not measured: N successful requests inside the probe
    interval must not become N database round trips."""
    clock = [1000.0]
    monkeypatch.setattr(failure_monitor, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = TestClient(_mini_app())

    for _ in range(50):
        client.get("/ok")
    assert observed["successes"] == 1

    clock[0] += failure_monitor._PROBE_INTERVAL_SECONDS + 1
    client.get("/ok")
    assert observed["successes"] == 2


def test_a_flood_of_500s_is_throttled_into_a_few_reports(observed, monkeypatch):
    """The in-process gate in front of the database. Thousands of errors a minute
    must not become thousands of writes to the store that may itself be sick."""
    clock = [1000.0]
    monkeypatch.setattr(failure_monitor, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = TestClient(_mini_app(), raise_server_exceptions=False)

    for _ in range(500):
        clock[0] += 0.05  # 500 errors over 25 seconds
        client.get("/boom")

    assert len(observed["failures"]) <= 3


def test_the_middleware_is_inert_when_alerting_is_unconfigured(monkeypatch):
    """Unconfigured means untouched: no state, no work, on the hot path of every
    request in local dev, CI and the whole test suite."""
    called = []

    async def note_failure(signal, *, process_sustained):
        called.append(signal)

    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: False)
    monkeypatch.setattr(failure_alert, "note_failure", note_failure)

    client = TestClient(_mini_app(), raise_server_exceptions=False)
    client.get("/boom")
    client.get("/ok")

    assert called == []


def test_maintenance_mode_is_not_an_outage():
    """The site-wide pause returns 503 on purpose. The engineer flipped that
    switch; alerting them about it is noise, and an incident opened by it would
    mask a real failure behind an already-open one."""
    from fastapi import Request

    from app.core.security import MaintenanceModeError
    from app.main import maintenance_mode_handler

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/alumni",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
    }
    request = Request(scope)
    asyncio.run(maintenance_mode_handler(request, MaintenanceModeError("paused")))

    assert request.state.alert_ignore is True
    # And the flag lives on the ASGI scope, so the outer middleware sees it.
    assert scope["state"]["alert_ignore"] is True


# ------------------------------------------------- the constraint under it all --


MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "database"
    / "migrations"
    / "2026-08-18_service_incidents.sql"
)


def test_the_partial_unique_index_still_exists():
    """Every test above would still pass if this index were dropped — and the
    feature would silently become one email per serverless instance. It is the
    only thing making "one open incident" true, so it gets its own guard."""
    sql = MIGRATION.read_text(encoding="utf-8")

    index = re.search(
        r"CREATE\s+UNIQUE\s+INDEX[^;]*?ON\s+service_incidents\s*\(\s*environment\s*\)"
        r"\s*WHERE\s+resolved_at\s+IS\s+NULL",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert index, "the one-open-incident-per-environment index is missing"
    assert "ENABLE ROW LEVEL SECURITY" in sql, "new tables must be locked down"

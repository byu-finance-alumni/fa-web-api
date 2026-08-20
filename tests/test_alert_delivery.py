"""The alert-delivery toggle: Slack only, or Slack AND e-mail (#458).

``tests/test_failure_alert.py`` proves WHEN an alert is produced and
``tests/test_slack_alerts.py`` proves WHERE it goes. This file proves the one
thing the new setting is allowed to change — whether the e-mail is a COPY or a
BACKSTOP — and, far more importantly, the one thing it is NOT allowed to change:

    ⚠️ NO SETTING MAY PRODUCE "NO CHANNEL AT ALL".

A toggle that could switch the e-mail off entirely would let one click turn a
monitoring feature into silence, and silence is the failure the whole alerting
module exists to prevent. So the central assertion here — section 2 — is
PARAMETRIZED OVER EVERY MODE IN ``alert_delivery.MODES`` rather than written for
the one that looks risky. A third mode added later cannot quietly opt out of the
backstop; it has to make this file pass first.

The other half is that READING the setting can never break the alerting path. It
runs on a request that is already failing, quite possibly failing BECAUSE the
database is down, which is precisely the incident worth hearing about. Section 3
kills the database in every way it can and asserts the read still answers.
"""

import asyncio
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.sql.elements import TextClause

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.engineer_action import EngineerActionLog
from app.schemas.alert_delivery import AlertDeliveryUpdate
from app.schemas.auth import UserContext
from app.services import alert_delivery, failure_alert

ROWS = [("Environment", "production"), ("Status code", "500")]
WEBHOOK = "https://hooks.slack.test/services/ERROR/CHANNEL/FAKE"


# --------------------------------------------------------------- fake world --


@pytest.fixture
def channels(monkeypatch):
    """Capture what each channel would send, with no network call anywhere."""
    captured = {"email": [], "slack": []}

    async def fake_email(url, *, api_key, payload, timeout):
        captured["email"].append(payload)
        return SimpleNamespace(is_success=True, status_code=200)

    async def fake_slack(url, *, payload, timeout):
        captured["slack"].append((url, payload))
        return SimpleNamespace(is_success=True, status_code=200)

    monkeypatch.setattr(failure_alert.mailer, "post_json", fake_email)
    monkeypatch.setattr(failure_alert.slack, "post_webhook", fake_slack)
    return captured


def _settings(monkeypatch, *, webhook=WEBHOOK, email=True):
    """Both channels configured unless a test says otherwise."""
    fake = SimpleNamespace(
        environment="production",
        resend_api_key="re_test_key" if email else None,
        alert_from_name="BYU Finance Alumni API",
        alert_recipients=["engineer@example.edu"] if email else [],
        alert_sender="alerts@example.edu" if email else None,
        slack_webhook=webhook,
        slack_security_webhook=webhook,
    )
    monkeypatch.setattr(failure_alert, "get_settings", lambda: fake)


def _mode(monkeypatch, mode):
    """Install the delivery mode the way ``deliver_alert`` sees it."""

    async def _read():
        return mode

    monkeypatch.setattr(alert_delivery, "read_mode", _read)


def _deliver():
    return asyncio.run(failure_alert.deliver_alert("subject", "intro", ROWS))


def _rejecting_webhook(channels):
    """A Slack endpoint that answers 404, the way a revoked webhook does."""

    async def reject(url, *, payload, timeout):
        channels["slack"].append((url, payload))
        return SimpleNamespace(is_success=False, status_code=404)

    return reject


def _exploding_webhook():
    """A Slack endpoint that is simply unreachable."""

    async def explode(url, *, payload, timeout):
        raise RuntimeError("slack unreachable")

    return explode


# ============================================== 1. THE TWO MODES DO THEIR JOB ==


def test_slack_only_leaves_the_mailbox_alone(monkeypatch, channels):
    """The default, and the behaviour the owner asked for: one message in one
    place. Both channels used to fire every time, which is why the first real
    security alert arrived twice."""
    _settings(monkeypatch)
    _mode(monkeypatch, alert_delivery.SLACK_ONLY)

    assert _deliver() is True
    assert len(channels["slack"]) == 1
    assert channels["email"] == [], "in slack_only the mail is a backstop, not a copy"


def test_slack_and_email_sends_both_every_time(monkeypatch, channels):
    """The other setting, and the behaviour from before 2026-08-19. Nothing has
    failed here — Slack accepted the post — and the e-mail goes anyway, which is
    the entire difference between the two modes."""
    _settings(monkeypatch)
    _mode(monkeypatch, alert_delivery.SLACK_AND_EMAIL)

    assert _deliver() is True
    assert len(channels["slack"]) == 1
    assert len(channels["email"]) == 1


def test_the_two_modes_differ_only_in_the_healthy_case(monkeypatch, channels):
    """Stated as one assertion because it is the whole feature: when Slack works,
    the setting decides whether the mailbox also hears about it. When Slack does
    not work, section 2 says both modes behave identically."""
    _settings(monkeypatch)

    _mode(monkeypatch, alert_delivery.SLACK_ONLY)
    _deliver()
    only = len(channels["email"])

    _mode(monkeypatch, alert_delivery.SLACK_AND_EMAIL)
    _deliver()
    both = len(channels["email"])

    assert (only, both) == (0, 1)


# ================================ 2. THE BACKSTOP SURVIVES *EVERY* MODE =======
#
# The rule this feature could most easily get wrong, so it is asserted for every
# value in MODES rather than for slack_only alone. If somebody adds a third mode,
# these are the tests that will refuse it until its failure path still reaches a
# person.


@pytest.mark.parametrize("mode", alert_delivery.MODES)
def test_a_rejected_slack_post_still_reaches_the_mailbox(mode, monkeypatch, channels):
    """A revoked webhook, a moved channel, a typo'd URL: Slack answers 404 and
    the alert must still land somewhere. This is why the second channel was not
    simply deleted when Slack became the primary one."""
    _settings(monkeypatch)
    _mode(monkeypatch, mode)
    monkeypatch.setattr(
        failure_alert.slack, "post_webhook", _rejecting_webhook(channels)
    )

    assert _deliver() is True
    assert len(channels["email"]) == 1, f"{mode} produced no alert at all"
    assert channels["email"][0]["subject"] == "subject"


@pytest.mark.parametrize("mode", alert_delivery.MODES)
def test_an_unreachable_slack_still_reaches_the_mailbox(mode, monkeypatch, channels):
    """Slack itself being down. The POST raises rather than answering, which is a
    different code path from a 404 and has to end in the same place."""
    _settings(monkeypatch)
    _mode(monkeypatch, mode)
    monkeypatch.setattr(failure_alert.slack, "post_webhook", _exploding_webhook())

    assert _deliver() is True
    assert len(channels["email"]) == 1, f"{mode} produced no alert at all"


@pytest.mark.parametrize("mode", alert_delivery.MODES)
def test_an_unconfigured_slack_channel_still_reaches_the_mailbox(
    mode, monkeypatch, channels
):
    """The quiet one. No webhook is set at all, so nothing is even attempted and
    nothing fails — which is exactly how "Slack only" would become "nothing" if
    the mail were conditional on a Slack ERROR rather than on a Slack SUCCESS."""
    _settings(monkeypatch, webhook=None)
    _mode(monkeypatch, mode)

    assert _deliver() is True
    assert channels["slack"] == []
    assert len(channels["email"]) == 1, f"{mode} produced no alert at all"


@pytest.mark.parametrize("mode", alert_delivery.MODES)
def test_the_mailbox_is_skipped_only_when_slack_actually_landed(
    mode, monkeypatch, channels
):
    """The invariant behind all of the above, stated directly.

    Delivery may skip the e-mail ONLY in the case where Slack accepted the
    message. Every other outcome — rejected, unreachable, unconfigured — must
    produce an e-mail, in every mode.
    """
    _settings(monkeypatch)
    _mode(monkeypatch, mode)

    outcomes = {
        "slack accepted": None,
        "slack rejected": _rejecting_webhook(channels),
        "slack unreachable": _exploding_webhook(),
    }
    for name, transport in outcomes.items():
        channels["email"].clear()
        channels["slack"].clear()
        if transport is not None:
            monkeypatch.setattr(failure_alert.slack, "post_webhook", transport)

        assert _deliver() is True
        slack_landed = name == "slack accepted"
        emailed = len(channels["email"]) == 1
        if not slack_landed:
            assert emailed, f"{mode}: {name} produced no alert at all"


@pytest.mark.parametrize("mode", alert_delivery.MODES)
def test_no_mode_is_silent_when_only_the_mailbox_is_configured(
    mode, monkeypatch, channels
):
    """A deployment with no webhook at all — which is what production ran before
    Slack existed. Neither mode may treat "Slack is the channel" as "and there is
    no other one"."""
    _settings(monkeypatch, webhook=None, email=True)
    _mode(monkeypatch, mode)

    assert _deliver() is True
    assert len(channels["email"]) == 1


def test_neither_channel_configured_is_still_silent_and_raises_nothing(
    monkeypatch, channels
):
    """The default everywhere except prod: local dev, CI, this suite, every
    preview deployment. The toggle must not turn "nothing configured" into an
    attempted send or an exception."""
    _settings(monkeypatch, webhook=None, email=False)
    for mode in alert_delivery.MODES:
        _mode(monkeypatch, mode)
        assert _deliver() is False
        assert channels["email"] == []
        assert channels["slack"] == []


def test_in_both_modes_a_broken_mailbox_does_not_suppress_slack(monkeypatch, channels):
    """The mirror of the backstop rule. In slack_and_email the two are dispatched
    concurrently with ``return_exceptions=True`` precisely so that one channel
    raising cannot take the other down with it."""
    _settings(monkeypatch)

    async def exploding(url, *, api_key, payload, timeout):
        raise RuntimeError("resend unreachable")

    monkeypatch.setattr(failure_alert.mailer, "post_json", exploding)

    for mode in alert_delivery.MODES:
        channels["slack"].clear()
        _mode(monkeypatch, mode)
        assert _deliver() is True
        assert len(channels["slack"]) == 1


# ====================== 3. READING THE SETTING CANNOT BREAK THE ALERT PATH =====


class _FakeRow:
    def __init__(self, mode):
        self.mode = mode
        self.updated_at = None
        self.updated_by_user_id = None


class _FakeSession:
    """Answers the one SELECT ``_load_mode`` makes."""

    def __init__(self, row=None, raises=None, delay=0.0):
        self.row, self.raises, self.delay = row, raises, delay
        self.scalars = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def scalar(self, _stmt):
        self.scalars += 1
        if self.raises is not None:
            raise self.raises
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.row


def _session_factory(session):
    return lambda: session


def test_the_default_is_todays_behaviour(monkeypatch):
    """Whatever goes wrong, the answer is the mode the API already runs in, so a
    failure to read this setting changes nothing about delivery."""
    assert alert_delivery.DEFAULT_MODE == alert_delivery.SLACK_ONLY


def test_no_database_configured_reads_as_the_default(monkeypatch):
    """Local runs, unit tests, and anything imported without a DATABASE_URL."""
    monkeypatch.setattr(alert_delivery.database, "SessionLocal", None)
    alert_delivery.reset_cache()

    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.DEFAULT_MODE


def test_a_database_error_reads_as_the_default_and_never_raises(monkeypatch):
    """The case that matters most: the alerting path runs on a request that is
    already failing, and very often failing BECAUSE the database is down. That is
    exactly the incident worth hearing about, so the read must answer rather than
    add a second exception to a broken request."""
    session = _FakeSession(raises=RuntimeError("relation does not exist"))
    monkeypatch.setattr(
        alert_delivery.database, "SessionLocal", _session_factory(session)
    )
    alert_delivery.reset_cache()

    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.DEFAULT_MODE


def test_a_slow_database_is_not_waited_on(monkeypatch):
    """Timing out is just another error path, and every error path is the
    default. A slow answer here is worth less than the default answer now."""
    session = _FakeSession(
        row=_FakeRow(alert_delivery.SLACK_AND_EMAIL),
        delay=alert_delivery._READ_TIMEOUT_SECONDS + 0.5,
    )
    monkeypatch.setattr(
        alert_delivery.database, "SessionLocal", _session_factory(session)
    )
    alert_delivery.reset_cache()

    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.DEFAULT_MODE


def test_a_missing_row_reads_as_the_default(monkeypatch):
    """The migration has been applied but the seed row is gone. Same answer."""
    session = _FakeSession(row=None)
    monkeypatch.setattr(
        alert_delivery.database, "SessionLocal", _session_factory(session)
    )
    alert_delivery.reset_cache()

    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.DEFAULT_MODE


def test_an_unrecognised_stored_value_reads_as_the_default(monkeypatch):
    """Third layer, after the API's Literal and the database's CHECK. An unknown
    value must mean "behave as we did yesterday", never an exception on the
    alerting path and never a channel silently switched off."""
    session = _FakeSession(row=_FakeRow("email_only"))
    monkeypatch.setattr(
        alert_delivery.database, "SessionLocal", _session_factory(session)
    )
    alert_delivery.reset_cache()

    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.DEFAULT_MODE
    assert alert_delivery.normalize(None) == alert_delivery.DEFAULT_MODE
    assert alert_delivery.normalize("") == alert_delivery.DEFAULT_MODE
    assert alert_delivery.normalize("SLACK_ONLY") == alert_delivery.DEFAULT_MODE


def test_a_stored_value_is_actually_used(monkeypatch):
    """The read is not a stub — a stored ``slack_and_email`` comes back."""
    session = _FakeSession(row=_FakeRow(alert_delivery.SLACK_AND_EMAIL))
    monkeypatch.setattr(
        alert_delivery.database, "SessionLocal", _session_factory(session)
    )
    alert_delivery.reset_cache()

    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.SLACK_AND_EMAIL


def test_the_read_is_cached_so_a_burst_of_alerts_is_not_a_query_each(monkeypatch):
    """A flood of 500s can deliver several alerts in a second, and the database
    may be the thing that is failing. One read serves them all."""
    session = _FakeSession(row=_FakeRow(alert_delivery.SLACK_AND_EMAIL))
    monkeypatch.setattr(
        alert_delivery.database, "SessionLocal", _session_factory(session)
    )
    alert_delivery.reset_cache()

    for _ in range(5):
        assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.SLACK_AND_EMAIL
    assert session.scalars == 1


def test_a_later_failure_keeps_the_last_known_value(monkeypatch):
    """Sticky rather than flapping: once a value has been read successfully, a
    database that goes away does not silently revert an engineer's choice."""
    good = _FakeSession(row=_FakeRow(alert_delivery.SLACK_AND_EMAIL))
    monkeypatch.setattr(alert_delivery.database, "SessionLocal", _session_factory(good))
    alert_delivery.reset_cache()
    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.SLACK_AND_EMAIL

    bad = _FakeSession(raises=RuntimeError("connection refused"))
    monkeypatch.setattr(alert_delivery.database, "SessionLocal", _session_factory(bad))
    # Expire the cache without waiting a minute.
    alert_delivery._cached = (
        alert_delivery._cached[0] - alert_delivery._CACHE_TTL_SECONDS - 1,
        alert_delivery._cached[1],
    )

    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.SLACK_AND_EMAIL


def test_the_cache_sentinel_is_none_and_not_a_timestamp(monkeypatch):
    """``time.monotonic()`` counts from an arbitrary origin — machine boot on
    Linux — so a 0.0 initial value would read as "we just refreshed it" on a cold
    serverless instance and pin the mode to whatever was in memory. Same trap
    that bit ``failure_alert._degraded_last_alert_at``, which CI caught and a
    long-running laptop did not."""
    alert_delivery.reset_cache()
    assert alert_delivery._cached is None


def test_the_delivery_path_never_raises_even_if_the_read_does(monkeypatch, channels):
    """Belt and braces on the contract above: if a future edit lets ``read_mode``
    raise, the alert must still go out rather than turning one broken request
    into two."""
    _settings(monkeypatch)
    monkeypatch.setattr(alert_delivery.database, "SessionLocal", None)
    alert_delivery.reset_cache()

    assert _deliver() is True
    assert len(channels["slack"]) == 1


# ============================================ 4. WRITING IT, AND THE AUDIT ====


class _WriteSession:
    """Enough of a session for ``set_mode``: one row, adds, and a commit."""

    def __init__(self, row=None, actor_email="engineer@byu.edu"):
        self.row = row
        self.actor_email = actor_email
        self.added: list = []
        self.commits = 0
        self._calls = 0

    async def scalar(self, stmt):
        self._calls += 1
        # First call is the config row; any later one is the actor's e-mail.
        if "alert_delivery_config" in str(stmt):
            return self.row
        return self.actor_email

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, alert_delivery.AlertDeliveryConfig):
            self.row = obj

    async def commit(self):
        self.commits += 1


def test_setting_the_mode_writes_it_and_says_who(monkeypatch):
    _settings(monkeypatch)
    session = _WriteSession(row=_FakeRow(alert_delivery.SLACK_ONLY))

    state = asyncio.run(
        alert_delivery.set_mode(
            session, mode=alert_delivery.SLACK_AND_EMAIL, actor_user_id=7
        )
    )

    assert session.row.mode == alert_delivery.SLACK_AND_EMAIL
    assert session.row.updated_by_user_id == 7
    assert session.commits == 1
    assert state.mode == alert_delivery.SLACK_AND_EMAIL
    assert state.updated_by_email == "engineer@byu.edu"


def test_setting_the_mode_is_audited_with_the_old_and_new_values(monkeypatch):
    """An engineer changing where alerts go is exactly the kind of act the
    oversight trail exists for, and "from what, to what" is the part that makes
    the row useful six weeks later."""
    _settings(monkeypatch)
    session = _WriteSession(row=_FakeRow(alert_delivery.SLACK_ONLY))

    asyncio.run(
        alert_delivery.set_mode(
            session, mode=alert_delivery.SLACK_AND_EMAIL, actor_user_id=7
        )
    )

    entries = [o for o in session.added if isinstance(o, AuditLog)]
    assert len(entries) == 1
    assert entries[0].action_type == "set_alert_delivery_mode"
    assert entries[0].entity_type == "alert_delivery_config"
    assert entries[0].old_value == alert_delivery.SLACK_ONLY
    assert entries[0].new_value == alert_delivery.SLACK_AND_EMAIL
    assert entries[0].user_id == 7


def test_the_service_never_writes_the_engineer_log_itself(monkeypatch):
    """``engineer_action_log`` is written by the ``before_flush`` guard in
    app/models/audit.py, which reroutes an engineer's AuditLog (#199). A service
    writing it directly would bypass the guard that makes the trail
    tamper-resistant."""
    _settings(monkeypatch)
    session = _WriteSession(row=_FakeRow(alert_delivery.SLACK_ONLY))

    asyncio.run(
        alert_delivery.set_mode(
            session, mode=alert_delivery.SLACK_AND_EMAIL, actor_user_id=7
        )
    )

    assert not any(isinstance(o, EngineerActionLog) for o in session.added)
    source = Path(alert_delivery.__file__).read_text(encoding="utf-8")
    assert "EngineerActionLog" not in source
    assert "engineer_action_log" in source, "but it should SAY why it does not"


def test_a_write_publishes_the_new_value_to_this_process_immediately(monkeypatch):
    """The engineer who just pressed the button must not see a stale answer for a
    minute. Other instances pick it up within the cache TTL."""
    _settings(monkeypatch)
    alert_delivery.reset_cache()
    session = _WriteSession(row=_FakeRow(alert_delivery.SLACK_ONLY))

    asyncio.run(
        alert_delivery.set_mode(
            session, mode=alert_delivery.SLACK_AND_EMAIL, actor_user_id=7
        )
    )

    monkeypatch.setattr(alert_delivery.database, "SessionLocal", None)
    assert asyncio.run(alert_delivery.read_mode()) == alert_delivery.SLACK_AND_EMAIL


def test_an_unknown_mode_cannot_be_written(monkeypatch):
    """Three layers refuse it. This is the service's."""
    _settings(monkeypatch)
    session = _WriteSession(row=_FakeRow(alert_delivery.SLACK_ONLY))

    state = asyncio.run(
        alert_delivery.set_mode(session, mode="email_only", actor_user_id=7)
    )

    assert state.mode == alert_delivery.DEFAULT_MODE
    assert session.row.mode == alert_delivery.DEFAULT_MODE


def test_the_api_schema_refuses_an_unknown_mode_before_any_query_runs():
    """And this is the edge's layer — a 422, not a stored surprise."""
    with pytest.raises(ValidationError):
        AlertDeliveryUpdate(mode="email_only")
    assert AlertDeliveryUpdate(mode=alert_delivery.SLACK_ONLY).mode == "slack_only"


def test_the_schema_and_the_service_agree_on_the_spellings():
    """Two lists of the same two strings, in two files. A parity test is what
    stops them drifting — the failure mode otherwise is a mode the console can
    select and the service silently normalises away."""
    from typing import get_args

    from app.schemas.alert_delivery import AlertDeliveryModeName

    assert set(get_args(AlertDeliveryModeName)) == set(alert_delivery.MODES)


def test_the_permitted_modes_are_the_ones_the_migration_allows():
    """The database's CHECK constraint is the fourth copy of this list. It is
    written by hand in SQL, so nothing but a test connects it to the Python."""
    sql = Path(alert_delivery.__file__).parents[2] / "database" / "migrations"
    sql = (sql / "2026-08-19_alert_delivery_config.sql").read_text(encoding="utf-8")
    check = sql[sql.index("ck_alert_delivery_config_mode") :]
    check = check[: check.index(")")]
    for mode in alert_delivery.MODES:
        assert f"'{mode}'" in check, f"the migration's CHECK forbids {mode}"


# ================================= 5. THE SQL TRAP THIS PROJECT KEEPS HITTING ==


#: A ``:placeholder``, ignoring ``::casts``. Same expression as the guard in
#: tests/test_login_auto_block.py, which exists because the first run of #457
#: against a real Postgres died on it.
_PLACEHOLDER = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)", re.IGNORECASE)


def test_no_raw_statement_here_hides_a_swallowed_parameter():
    """``text()`` does not bind ``:name::type``.

    SQLAlchemy's placeholder pattern refuses a name followed by a colon, so a
    parameter written with a Postgres-style cast stays in the statement as
    literal text and the statement is a syntax error against a REAL database
    while every faked test passes. It failed silently the last time, on a path
    whose caller swallows exceptions, so the feature would have shipped doing
    nothing.

    This module uses the ORM today and therefore has no ``text()`` statements at
    all — which is itself the safest answer. The guard is here so that the first
    person to add one is caught by the same net that caught #457.

    Write ``CAST(:name AS type)``. It is uglier and it works.
    """
    statements = {
        name: value
        for name, value in vars(alert_delivery).items()
        if isinstance(value, TextClause)
    }
    for name, stmt in statements.items():
        sql = "\n".join(line.split("--")[0] for line in str(stmt).splitlines())
        written = set(_PLACEHOLDER.findall(sql))
        assert written == set(stmt._bindparams), (
            f"alert_delivery.{name}: SQLAlchemy did not bind "
            f"{sorted(written - set(stmt._bindparams))} (a ':name::type' cast "
            f"swallows the parameter — write CAST(:name AS type))"
        )
    # And the source itself, so a statement built inline in a function — not
    # bound to a module attribute — is covered too.
    source = Path(alert_delivery.__file__).read_text(encoding="utf-8")
    assert not re.search(r":[a-z_][a-z0-9_]*::", source), (
        "a ':name::type' cast swallows the parameter — write CAST(:name AS type)"
    )


# ================================== 6. THE ENGINEER'S CONTROL, END TO END =====


def _ctx(*roles: str, user_id: int = 7) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def console(monkeypatch):
    """The engineer console, wired to a fake single-row config table."""
    _settings(monkeypatch)
    session = _WriteSession(row=_FakeRow(alert_delivery.SLACK_ONLY))

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        yield client, session
    app.dependency_overrides.clear()


def test_an_engineer_can_read_the_current_mode(console):
    client, _session = console

    body = client.get("/admin/alert-delivery").json()

    assert body["mode"] == alert_delivery.SLACK_ONLY
    # The two flags that let the console promise the backstop honestly.
    assert body["slack_configured"] is True
    assert body["email_configured"] is True


def test_the_console_is_told_when_the_backstop_has_nowhere_to_go(
    monkeypatch, console
):
    """"Slack only" reads as "and e-mail if Slack breaks" — which is a LIE when no
    mailbox is configured. The screen cannot say so without being told."""
    client, _session = console
    _settings(monkeypatch, email=False)

    body = client.get("/admin/alert-delivery").json()

    assert body["email_configured"] is False


def test_an_engineer_can_change_the_mode(console):
    client, session = console

    body = client.put(
        "/admin/alert-delivery", json={"mode": alert_delivery.SLACK_AND_EMAIL}
    ).json()

    assert body["mode"] == alert_delivery.SLACK_AND_EMAIL
    assert session.row.mode == alert_delivery.SLACK_AND_EMAIL
    assert [o.action_type for o in session.added if isinstance(o, AuditLog)] == [
        "set_alert_delivery_mode"
    ]


def test_an_unknown_mode_is_refused_at_the_edge(console):
    """422 before any query runs — the Literal on AlertDeliveryUpdate."""
    client, session = console

    assert client.put("/admin/alert-delivery", json={"mode": "email_only"}).status_code == 422
    assert session.row.mode == alert_delivery.SLACK_ONLY


def test_an_unknown_field_is_refused_rather_than_ignored(console):
    """``extra="forbid"``. A typo'd field on a control somebody just changed must
    not come back 200 having done nothing."""
    client, _session = console

    resp = client.put(
        "/admin/alert-delivery",
        json={"mode": alert_delivery.SLACK_ONLY, "enabled": True},
    )

    assert resp.status_code == 422


def test_only_an_engineer_can_read_or_change_where_alerts_go(console):
    """Same gate as the neighbouring alert controls. A super_admin is not an
    engineer here — this decides whether an outage reaches anybody."""
    client, _session = console
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")

    assert client.get("/admin/alert-delivery").status_code == 403
    assert (
        client.put(
            "/admin/alert-delivery", json={"mode": alert_delivery.SLACK_AND_EMAIL}
        ).status_code
        == 403
    )

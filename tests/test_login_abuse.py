"""Brute-force detection: one alert per source, and none for a human (#456).

Every test in this file is a replay of something that actually happened, or of
something that must never be treated as an attack. The three attack scenarios are
the real prod numbers from 2026-08-19:

    66.234.153.26  (Romania)     190 attempts   68 addresses   over 10 minutes
    159.26.103.94  (Seattle WA)  338 attempts   78 addresses   over  6 minutes
    134.82.68.139  (Miami FL)    222 attempts  202 addresses   over 16 SECONDS

and the counter-case is the one this detector must never fire on: a staff member
mistyping their password.

The requirement is a NEGATIVE one in both directions — 750 attempts must not
produce 750 messages, and a fumbling human must not produce one — so nearly every
assertion here counts messages rather than checking that one happened.

Since #457 the detector also BLOCKS the sources it reports, so a few tests here
cover the seam: that a block is applied without any alert channel configured,
that the kill switch turns the whole path off, and that the message says what was
done about the source. The block's own safety properties — above all "never block
an address with a recent successful sign-in" — live in
tests/test_login_auto_block.py, which drives the same fake database.

HOW THE DATABASE IS FAKED, AND WHAT THAT DOES AND DOES NOT PROVE.
:class:`FakeAbuseData` is a real implementation of the semantics the services
depend on: at most ONE open incident per (environment, source), "claim by setting
a column that must still be NULL", at most one un-lifted block per (environment,
source), and the three exemptions guarding block creation. The services' real
statements are dispatched to it by identity, so these tests exercise the actual
call sequence, the actual parameters and the actual decisions.

What it CANNOT prove is that Postgres provides those semantics — that comes from
the partial unique index ``uq_login_abuse_open`` and from
``UPDATE ... WHERE alert_sent_at IS NULL RETURNING`` under READ COMMITTED. Two
tests at the bottom cover the gap: one asserts the migration still declares the
index, and one deliberately makes the fake's claim NAIVE and proves the flood
tests above go red — because a suite that passes with the dedup removed is not
testing the dedup.
"""

import asyncio
import datetime
import math
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import failure_alert, login_abuse, login_block

UTC = datetime.UTC


# --------------------------------------------------------------- the fake DB --


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def first(self):
        return dict(self._rows[0]) if self._rows else None

    def all(self):
        return [dict(r) for r in self._rows]


class FakeAbuseData:
    """In-memory ``login_failures`` + ``login_abuse_incidents`` + ``login_events``
    + ``login_ip_blocks``.

    Shared by every :class:`FakeSession`, the way one database is shared by every
    serverless instance. Holds a single clock used for both the wall time the SQL
    would see (``now()``) and the monotonic clock the in-process gate reads, so a
    replay advances them together and cannot drift.

    THE BLOCK STORE IS A REAL IMPLEMENTATION OF THE THREE EXEMPTIONS (#457), not
    a stub: the ``NOT EXISTS`` clauses in ``login_block._SQL_BLOCK`` are the
    safety properties of the whole feature, so a fake that ignored them would let
    every safety test pass while proving nothing. It also honours "at most one
    un-lifted row per (environment, ip)" — the partial unique index — and expiry
    by comparison against the shared clock rather than by any sweep, which is how
    the real one behaves. See tests/test_login_auto_block.py, which drives this
    same store and includes the mutation checks that prove these clauses are what
    the safety assertions actually depend on.
    """

    def __init__(self):
        self.failures: list[dict] = []
        self.incidents: list[dict] = []
        self.successes: list[dict] = []
        self.blocks: list[dict] = []
        self.next_id = 1
        self.next_block_id = 1
        self.measurements = 0
        self.block_writes = 0
        self.now = datetime.datetime(2026, 8, 19, 8, 47, 12, tzinfo=UTC)
        self.monotonic = 100_000.0

    def advance(self, seconds: float) -> None:
        self.now += datetime.timedelta(seconds=seconds)
        self.monotonic += seconds

    def record_failure(self, *, ip: str, email: str) -> None:
        """What ``_record_login_failure`` commits before the detector is called."""
        self.failures.append({"ip": ip, "email": email, "at": self.now})

    def record_success(self, *, ip: str, engineer: bool = False) -> None:
        """A ``login_events`` row — what ``POST /auth/login`` writes.

        ⚠️ Only an AUTHENTICATED caller can produce one of these, which is the
        reason the exemption built on them is not forgeable by the same party
        that can forge ``ip_address``.
        """
        self.successes.append({"ip": ip, "at": self.now, "engineer": engineer})

    # -- login_ip_blocks -------------------------------------------------------

    def active_block(self, ip: str, environment: str = "production") -> dict | None:
        """The un-lifted, unexpired block for a source, if any."""
        row = self._unlifted(environment, ip)
        if row is None or row["blocked_until"] <= self.now:
            return None
        return row

    def _unlifted(self, environment, ip):
        # The partial unique index: at most one un-lifted row per (env, ip).
        for row in self.blocks:
            if (
                row["environment"] == environment
                and row["ip_address"] == ip
                and row["lifted_at"] is None
            ):
                return row
        return None

    def is_blocked(self, params):
        row = self._unlifted(params["environment"], params["ip"])
        # ⚠️ `blocked_until > now()` IS THE EXPIRY — in the read, not in a sweep.
        if row is None or row["blocked_until"] <= self.now:
            return []
        left = math.ceil((row["blocked_until"] - self.now).total_seconds())
        return [{"seconds_left": left}]

    def block(self, params):
        """``_SQL_BLOCK``: the guarded INSERT ... ON CONFLICT DO UPDATE.

        The three ``NOT EXISTS`` clauses, in the order the statement has them.
        """
        self.block_writes += 1
        env, ip = params["environment"], params["ip"]

        # 1. A successful sign-in from this address inside the lookback.
        cutoff = self.now - datetime.timedelta(
            seconds=params["success_lookback_seconds"]
        )
        if any(s["ip"] == ip and s["at"] >= cutoff for s in self.successes):
            return []
        # 2. An ENGINEER has signed in from this address — ever, no time bound.
        if any(s["ip"] == ip and s["engineer"] for s in self.successes):
            return []
        # 3. An engineer lifted a block on this source recently.
        grace = self.now - datetime.timedelta(seconds=params["lift_grace_seconds"])
        if any(
            b["environment"] == env
            and b["ip_address"] == ip
            and b["lifted_at"] is not None
            and b["lifted_at"] >= grace
            for b in self.blocks
        ):
            return []

        until = self.now + datetime.timedelta(seconds=params["block_seconds"])
        row = self._unlifted(env, ip)
        if row is None:
            row = {
                "block_id": self.next_block_id,
                "environment": env,
                "ip_address": ip,
                "blocked_at": self.now,
                "blocked_until": until,
                "attempt_count": params["attempts"],
                "distinct_email_count": params["distinct_emails"],
                "pattern": params["pattern"],
                "abuse_incident_id": params["incident_id"],
                "lifted_at": None,
                "lifted_by_user_id": None,
            }
            self.next_block_id += 1
            self.blocks.append(row)
        else:
            # A re-arm restarts the current period; GREATEST on the expiry means
            # it may extend, never shorten.
            row["blocked_at"] = self.now
            row["blocked_until"] = max(row["blocked_until"], until)
            row["attempt_count"] = max(row["attempt_count"], params["attempts"])
            row["distinct_email_count"] = max(
                row["distinct_email_count"], params["distinct_emails"]
            )
            row["pattern"] = params["pattern"]
            row["abuse_incident_id"] = (
                params["incident_id"] or row["abuse_incident_id"]
            )
        return [
            {
                key: row[key]
                for key in (
                    "block_id",
                    "ip_address",
                    "blocked_at",
                    "blocked_until",
                    "attempt_count",
                    "distinct_email_count",
                    "pattern",
                )
            }
        ]

    def link_incident(self, params):
        for row in self.blocks:
            if (
                row["block_id"] == params["block_id"]
                and row["abuse_incident_id"] is None
            ):
                row["abuse_incident_id"] = params["incident_id"]
        return []

    def list_blocks(self, params):
        rows = [
            {
                **row,
                "active": row["lifted_at"] is None
                and row["blocked_until"] > self.now,
            }
            for row in self.blocks
            if row["environment"] == params["environment"]
        ]
        if params["active_only"]:
            rows = [r for r in rows if r["active"]]
        rows.sort(key=lambda r: (r["active"], r["blocked_at"]), reverse=True)
        return rows[: params["limit"]]

    def lift(self, params):
        for row in self.blocks:
            if (
                row["block_id"] == params["block_id"]
                and row["environment"] == params["environment"]
                and row["lifted_at"] is None
            ):
                row["lifted_at"] = self.now
                row["lifted_by_user_id"] = params["actor_id"]
                return [
                    {
                        "block_id": row["block_id"],
                        "ip_address": row["ip_address"],
                        "blocked_until": row["blocked_until"],
                    }
                ]
        return []

    def _open(self, environment, ip):
        for row in self.incidents:
            if (
                row["environment"] == environment
                and row["ip_address"] == ip
                and row["resolved_at"] is None
            ):
                return row
        return None

    # -- the four statements ---------------------------------------------------

    def measure(self, params):
        self.measurements += 1
        cutoff = self.now - datetime.timedelta(seconds=params["window_seconds"])
        rows = [
            f
            for f in self.failures
            if f["ip"] == params["ip"] and f["at"] >= cutoff
        ]
        if not rows:
            return [
                {
                    "attempts": 0,
                    "distinct_emails": 0,
                    "first_at": None,
                    "last_at": None,
                }
            ]
        return [
            {
                "attempts": len(rows),
                "distinct_emails": len({r["email"] for r in rows}),
                "first_at": min(r["at"] for r in rows),
                "last_at": max(r["at"] for r in rows),
            }
        ]

    def close_if_quiet(self, params):
        row = self._open(params["environment"], params["ip"])
        if row is None:
            return []
        if row["last_seen_at"] < self.now - datetime.timedelta(
            seconds=params["quiet_seconds"]
        ):
            row["resolved_at"] = self.now
        return []

    def upsert(self, params):
        # The partial unique index: an INSERT cannot land while a row for this
        # (environment, source) is open, so the statement folds into the open one.
        row = self._open(params["environment"], params["ip"])
        if row is None:
            row = {
                "abuse_incident_id": self.next_id,
                "environment": params["environment"],
                "ip_address": params["ip"],
                "started_at": params["first_at"],
                "last_seen_at": self.now,
                "attempt_count": params["attempts"],
                "distinct_email_count": params["distinct_emails"],
                "window_seconds": params["window_seconds"],
                "city": params["city"],
                "region": params["region"],
                "country": params["country"],
                "pattern": params["pattern"],
                "alert_sent_at": None,
                "resolved_at": None,
            }
            self.next_id += 1
            self.incidents.append(row)
        else:
            row["started_at"] = min(row["started_at"], params["first_at"])
            row["last_seen_at"] = self.now
            row["attempt_count"] = max(row["attempt_count"], params["attempts"])
            row["distinct_email_count"] = max(
                row["distinct_email_count"], params["distinct_emails"]
            )
            row["pattern"] = params["pattern"]
            for key in ("city", "region", "country"):
                row[key] = row[key] or params[key]
        return [{"abuse_incident_id": row["abuse_incident_id"]}]

    def claim_alert(self, params):
        for row in self.incidents:
            if row["abuse_incident_id"] != params["incident_id"]:
                continue
            over = (
                row["distinct_email_count"] >= params["min_distinct"]
                or row["attempt_count"] >= params["min_attempts"]
            )
            # The claim: only a row whose alert_sent_at is STILL NULL may be won.
            if row["alert_sent_at"] is None and row["resolved_at"] is None and over:
                row["alert_sent_at"] = self.now
                return [dict(row)]
        return []


class FakeSession:
    """One "serverless instance"'s session onto the shared fake database."""

    def __init__(self, data: FakeAbuseData):
        self.data = data

    async def execute(self, statement, params=None):
        params = params or {}
        if statement is login_abuse._SQL_MEASURE:
            return _Result(self.data.measure(params))
        if statement is login_abuse._SQL_CLOSE_IF_QUIET:
            return _Result(self.data.close_if_quiet(params))
        if statement is login_abuse._SQL_UPSERT:
            return _Result(self.data.upsert(params))
        if statement is login_abuse._SQL_CLAIM_ALERT:
            return _Result(self.data.claim_alert(params))
        # The block store (#457) lives in the same fake database, because in
        # production it lives in the same real one and in the same transaction.
        if statement is login_block._SQL_IS_BLOCKED:
            return _Result(self.data.is_blocked(params))
        if statement is login_block._SQL_BLOCK:
            return _Result(self.data.block(params))
        if statement is login_block._SQL_LINK_INCIDENT:
            return _Result(self.data.link_incident(params))
        if statement is login_block._SQL_LIST:
            return _Result(self.data.list_blocks(params))
        if statement is login_block._SQL_LIFT:
            return _Result(self.data.lift(params))
        raise AssertionError(f"unexpected statement: {statement}")

    async def commit(self):
        return None

    async def rollback(self):
        return None


# ------------------------------------------------------------------ fixtures --


class _FakeSettings:
    """Both channels live, so a test that miscounts messages counts both."""

    environment = "production"
    resend_api_key = "re_test_key"
    alert_from_name = "BYU Finance Alumni API"
    alert_recipients = ["engineer@example.edu"]
    alert_sender = "alerts@example.edu"
    # hooks.slack.test, not .com: a realistic webhook literal is what gitleaks'
    # bundled rule looks for, and a fixture is not worth an allowlist entry.
    #
    # Two DIFFERENT URLs, so a test that miscounted channels — or a regression
    # that sent a security alert to the error channel — shows up as the wrong URL
    # rather than passing quietly. Which channel each kind routes to is asserted
    # directly in tests/test_slack_alerts.py.
    slack_webhook = "https://hooks.slack.test/services/ERROR/CHANNEL/FAKE"
    slack_security_webhook = "https://hooks.slack.test/services/SECURITY/CHANNEL/FAKE"


@pytest.fixture
def sent(monkeypatch):
    """Capture every message either channel would deliver, without a network call."""
    messages: list[dict] = []

    async def fake_email(url, *, api_key, payload, timeout):
        messages.append(
            {"channel": "email", "subject": payload["subject"], "body": payload["text"]}
        )
        return SimpleNamespace(is_success=True, status_code=200)

    async def fake_slack(url, *, payload, timeout):
        messages.append(
            {
                "channel": "slack",
                "url": url,
                "subject": payload["text"],
                "body": payload["blocks"][1]["text"]["text"],
            }
        )
        return SimpleNamespace(is_success=True, status_code=200)

    monkeypatch.setattr(failure_alert.mailer, "post_json", fake_email)
    monkeypatch.setattr(failure_alert.slack, "post_webhook", fake_slack)
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(login_abuse, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(login_block, "get_settings", lambda: _FakeSettings())
    return messages


def alerts(messages):
    """Distinct ALERTS, not deliveries: each alert fans out to both channels."""
    return [m for m in messages if m["channel"] == "slack"]


@pytest.fixture
def sim(monkeypatch):
    """A shared fake database with the in-process gate wired to its clock.

    Also points ``login_block`` at the same settings object, so the block rows
    and the incident rows agree about which environment they are in — they are
    scoped per environment in production and a fake that let them disagree would
    quietly test two unrelated stores.
    """
    data = FakeAbuseData()
    login_abuse.reset()
    monkeypatch.setattr(
        login_abuse, "time", SimpleNamespace(monotonic=lambda: data.monotonic)
    )
    monkeypatch.setattr(login_block, "get_settings", lambda: _FakeSettings())
    return data


ROMANIA = {"city": "Bucharest", "region": "Bucuresti", "country": "RO"}
SEATTLE = {"city": "Seattle", "region": "Washington", "country": "US"}
MIAMI = {"city": "Miami", "region": "Florida", "country": "US"}


def replay(sim, *, ip, attempts, addresses, seconds, geo):
    """Replay one campaign attempt-by-attempt, exactly as the login route sees it.

    Each attempt commits its ``login_failures`` row and then calls the detector —
    the real sequence in ``POST /auth/login/record``. The clock advances evenly
    across ``seconds``, which drives BOTH the wall clock the SQL reads and the
    monotonic clock the in-process evaluation gate reads.

    Addresses are cycled round-robin, which is how a scraper working a generated
    candidate list behaves. The assertions are all "how many messages", never "at
    which attempt", so the ordering cannot be what makes them pass.
    """
    step = seconds / attempts
    for n in range(attempts):
        sim.record_failure(ip=ip, email=f"target{n % addresses}@byu.edu")
        asyncio.run(login_abuse.observe_failure(FakeSession(sim), ip_address=ip, **geo))
        sim.advance(step)


# ------------------------------------------------- the three real attacks -----


def test_the_romania_campaign_produces_exactly_one_alert(sent, sim):
    """190 attempts across 68 addresses over ten minutes. Every attempt stayed
    under the 300/600s rate limit and averaged 2.8 tries per address — well under
    the per-email cooldown of 10 — so neither existing control saw anything."""
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)

    assert len(alerts(sent)) == 1
    assert len(sim.incidents) == 1
    subject = alerts(sent)[0]["subject"]
    assert "66.234.153.26" in subject
    assert "Login abuse" in subject


def test_the_seattle_campaign_produces_exactly_one_alert(sent, sim):
    """338 attempts across 78 addresses over six minutes."""
    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)

    assert len(alerts(sent)) == 1
    assert "159.26.103.94" in alerts(sent)[0]["subject"]


def test_the_sixteen_second_enumeration_burst_produces_exactly_one_alert(sent, sim):
    """222 attempts across 202 addresses in SIXTEEN SECONDS — one try per address,
    which is enumeration, not guessing. A per-email counter can never see this:
    every address is tried once.

    It is also the shape that constrains the design most. The whole campaign is
    over inside one evaluation interval, so a detector that waited out a
    cold-start grace, or that only ran on a cron, would have missed it entirely.
    """
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert len(alerts(sent)) == 1
    incident = sim.incidents[0]
    assert incident["distinct_email_count"] >= login_abuse.SPRAY_MIN_DISTINCT_EMAILS
    assert "enumeration" in incident["pattern"]


def test_all_three_sources_together_are_three_alerts_not_one_and_not_750(sent, sim):
    """The real morning: 750 attempts from three sources. Dedup is per SOURCE — one
    message each, and a fourth attacker would get a fourth. Collapsing them into
    one 'there is an attack' message would hide the second and third IPs, which
    are the things you act on."""
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)
    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert len(alerts(sent)) == 3
    assert {a["ip_address"] for a in sim.incidents} == {
        "66.234.153.26",
        "159.26.103.94",
        "134.82.68.139",
    }
    assert all(a["alert_sent_at"] is not None for a in sim.incidents)


# --------------------------------------------------- and NOT a real person ----


def test_a_person_mistyping_their_password_four_times_never_alerts(sent, sim):
    """The other half of the feature. A pager that fires on a bad morning is a
    pager people mute, and this one would be muted by the only person it pages."""
    replay(
        sim,
        ip="128.187.16.44",
        attempts=4,
        addresses=1,
        seconds=45,
        geo={"city": "Provo", "region": "Utah", "country": "US"},
    )

    assert sent == []
    assert sim.incidents == [], "an honest typo must not even create a row"


def test_a_person_failing_all_the_way_into_lockout_never_alerts(sent, sim):
    """Ten failures is where ``login_lockout`` stops them attempting at all
    (COOLDOWN_THRESHOLD). The detector must stay quiet across that entire range —
    the existing control already handled it, and reporting it twice is noise."""
    replay(
        sim,
        ip="128.187.16.44",
        attempts=10,
        addresses=1,
        seconds=300,
        geo={"city": "Provo", "region": "Utah", "country": "US"},
    )

    assert sent == []
    assert sim.incidents == []


def test_a_shared_office_address_with_several_fumbling_staff_never_alerts(sent, sim):
    """The realistic false positive. `ip_address` is the client address the
    frontend forwards, so a whole office behind one NAT shares a key: three people
    each failing four times is 12 attempts and 3 addresses from one IP. Both
    thresholds must still have headroom over that."""
    for person in range(3):
        for _ in range(4):
            sim.record_failure(ip="128.187.16.44", email=f"staff{person}@byu.edu")
            asyncio.run(
                login_abuse.observe_failure(
                    FakeSession(sim), ip_address="128.187.16.44"
                )
            )
            sim.advance(20)

    assert sent == []
    assert sim.incidents == []


def test_the_thresholds_sit_between_the_two(sent, sim):
    """State the separation as arithmetic rather than leaving it to the replays:
    every real source clears both thresholds by a wide margin, and the worst
    honest case cannot reach either."""
    # A fumbling human, and the whole office at once.
    assert not login_abuse.is_abusive(attempts=4, distinct_emails=1)
    assert not login_abuse.is_abusive(attempts=10, distinct_emails=1)
    assert not login_abuse.is_abusive(attempts=12, distinct_emails=3)
    # The three real sources.
    assert login_abuse.is_abusive(attempts=190, distinct_emails=68)
    assert login_abuse.is_abusive(attempts=338, distinct_emails=78)
    assert login_abuse.is_abusive(attempts=222, distinct_emails=202)
    # And the shape absent from that morning's data: classic guessing, one
    # address, high volume. Rule 1 stays at 1 forever; rule 2 is what catches it.
    assert login_abuse.is_abusive(attempts=60, distinct_emails=1)
    assert "guessing" in login_abuse.classify(attempts=60, distinct_emails=1)


# ------------------------------------------------------- one alert, not many --


def test_a_long_campaign_stays_one_alert_however_long_it_runs(sent, sim):
    """Once reported, the source goes quiet no matter how much more it tries."""
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)
    assert len(alerts(sent)) == 1

    replay(sim, ip="66.234.153.26", attempts=400, addresses=68, seconds=900, geo=ROMANIA)
    assert len(alerts(sent)) == 1


def test_concurrent_instances_send_exactly_one_alert(sent, sim):
    """The serverless case: twenty instances observe the same campaign at the same
    moment, each with its own session and no shared memory. Exactly one may open
    the incident, and exactly one may claim the message."""
    for n in range(40):
        sim.record_failure(ip="159.26.103.94", email=f"target{n}@byu.edu")

    async def scenario():
        return await asyncio.gather(
            *(
                login_abuse.evaluate(
                    FakeSession(sim), ip_address="159.26.103.94", **SEATTLE
                )
                for _ in range(20)
            )
        )

    claimed = asyncio.run(scenario())
    won = [c for c in claimed if c is not None]

    assert len(won) == 1, "twenty instances must not all claim the alert"
    assert len(sim.incidents) == 1, "twenty instances must not open twenty incidents"


def test_the_same_source_returning_later_is_reported_again(sent, sim):
    """Dedup is per CAMPAIGN, not "one alert ever". An incident that has gone quiet
    closes, so the same address coming back is news again — otherwise the detector
    stops working after its first success."""
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)
    assert len(alerts(sent)) == 1

    # Hours of silence, then it starts over.
    sim.advance(login_abuse._INCIDENT_QUIET_SECONDS + 600)
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)

    assert len(alerts(sent)) == 2
    assert len(sim.incidents) == 2
    assert sim.incidents[0]["resolved_at"] is not None


def test_a_pause_inside_one_campaign_does_not_become_a_second_alert(sent, sim):
    """A campaign that stops to re-target for a few minutes is still one campaign.
    Closing it too eagerly is how one attacker becomes a stream of messages."""
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)
    sim.advance(login_abuse._INCIDENT_QUIET_SECONDS - 300)
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)

    assert len(alerts(sent)) == 1
    assert len(sim.incidents) == 1


# ------------------------------------------------ cost and fragility on login --


def test_a_flood_is_throttled_into_a_handful_of_queries(sent, sim):
    """The in-process gate in front of the database. 222 attempts in 16 seconds
    must not become 222 aggregate queries on an unauthenticated public route."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    # ~4: one evaluation every _EVAL_INTERVAL_SECONDS across the 16-second burst.
    assert sim.measurements <= 6, sim.measurements
    assert len(alerts(sent)) == 1


def test_a_successful_login_and_an_unattributed_failure_cost_nothing(sent, sim):
    """The detector runs only on a failed login that carries a source address. No
    address means nothing to attribute the attempt to, and bucketing every
    unattributed failure together would alert on the aggregate of unrelated
    people."""
    asyncio.run(login_abuse.observe_failure(FakeSession(sim), ip_address=None))
    asyncio.run(login_abuse.observe_failure(FakeSession(sim), ip_address="   "))

    assert sim.measurements == 0
    assert sent == []


def _unconfigured(*, blocking: bool):
    return SimpleNamespace(
        environment="development",
        resend_api_key=None,
        alert_recipients=[],
        alert_sender=None,
        alert_from_name="x",
        slack_webhook=None,
        slack_security_webhook=None,
        login_auto_block_enabled=blocking,
    )


def test_no_channel_configured_means_no_incident_and_no_message(monkeypatch, sim):
    """With nowhere to send a message, none is sent and NO incident row is opened.

    ``login_abuse_incidents`` exists to dedup a message; with no channel there is
    no message to dedup, so writing rows there would be accumulating state nobody
    will ever read.

    ⚠️ THIS TEST USED TO ASSERT ``sim.measurements == 0`` — "unconfigured means
    untouched, not one query". #457 deliberately broke that, and this is the
    counterpart to :func:`test_blocking_does_not_depend_on_the_alert_webhook`
    below: the aggregate now runs because BLOCKING needs it, and blocking is a
    protection rather than an observability channel. Alerting is still exactly as
    off as it was.
    """
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _unconfigured(blocking=True))
    monkeypatch.setattr(login_abuse, "get_settings", lambda: _unconfigured(blocking=True))
    monkeypatch.setattr(login_block, "get_settings", lambda: _unconfigured(blocking=True))

    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert sim.incidents == []


def test_blocking_does_not_depend_on_the_alert_webhook(monkeypatch, sim):
    """A source is blocked with no Slack webhook and no alert mailbox set (#457).

    Wiring a security control to the presence of a webhook means rotating that
    webhook silently disables it — the exact "a forgotten env var must never
    become silence about an attack" failure this module's own docstring argues
    against. So blocking sits in front of the alerting gate, not behind it.
    """
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _unconfigured(blocking=True))
    monkeypatch.setattr(login_abuse, "get_settings", lambda: _unconfigured(blocking=True))
    monkeypatch.setattr(login_block, "get_settings", lambda: _unconfigured(blocking=True))

    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert sim.active_block("134.82.68.139", environment="development") is not None
    assert sim.incidents == [], "still no incident row and still no message"


def test_the_kill_switch_turns_the_whole_path_off(monkeypatch, sim):
    """With alerting unset AND ``LOGIN_AUTO_BLOCK_ENABLED=false``, nothing at all
    happens — not one query on the login path. That is the ONLY way back to the
    old "unconfigured means untouched" behaviour, and it has to be said out loud
    rather than achieved by forgetting to set something."""
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _unconfigured(blocking=False))
    monkeypatch.setattr(login_abuse, "get_settings", lambda: _unconfigured(blocking=False))
    monkeypatch.setattr(login_block, "get_settings", lambda: _unconfigured(blocking=False))

    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert sim.measurements == 0
    assert sim.incidents == []
    assert sim.blocks == []


def test_a_broken_detector_never_breaks_the_login_response(sent, sim, monkeypatch):
    """This sits on the unauthenticated pre-login path, whose response must be a
    pure function of the caller (anti-enumeration). A detector that could raise —
    or even that could vary the response — would be a security regression, so it
    swallows everything."""

    class ExplodingSession(FakeSession):
        async def execute(self, statement, params=None):
            raise RuntimeError("database gone")

    asyncio.run(
        login_abuse.observe_failure(ExplodingSession(sim), ip_address="1.2.3.4")
    )  # must not raise
    assert sent == []


def test_a_failing_delivery_does_not_retry_or_re_report(sim, monkeypatch):
    """The claim is committed before the message is sent, so an undeliverable alert
    costs ONE missed message rather than one attempt per login. And a failed alert
    is never itself alerted about — that is the only way to build a loop here."""
    attempts = []

    async def exploding(url, **kwargs):
        # One stub for both transports, so their two signatures (Resend takes an
        # api_key, Slack does not) both land here rather than one of them dying of
        # a TypeError and quietly looking like a suppressed send.
        attempts.append(kwargs["payload"])
        raise RuntimeError("unreachable")

    monkeypatch.setattr(failure_alert.mailer, "post_json", exploding)
    monkeypatch.setattr(failure_alert.slack, "post_webhook", exploding)
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(login_abuse, "get_settings", lambda: _FakeSettings())

    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)

    assert len(attempts) == 2, "one email + one Slack post, and no retry of either"


# --------------------------------------------------------- what is in the alert --


def test_the_alert_carries_what_the_owner_needs_to_act(sent, sim):
    """Source, geography, both counts, the window and the duration — everything the
    owner needs to decide whether to block the address, without opening a laptop.

    Note what the counts ARE: the totals AT THE MOMENT OF DETECTION, not the final
    tally of the campaign. The message goes out seconds in, while the attack is
    still running, because a report that waits for the attacker to finish is a
    report that arrives too late to act on."""
    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)
    message = alerts(sent)[0]
    body = message["body"]

    assert "159.26.103.94" in body
    assert "Seattle" in body
    assert "Washington" in body
    assert "production" in body
    assert "Duration" in body
    assert "in the last 15 minutes" in body

    # The counts in the message are the ones that were true WHEN IT WAS SENT, and
    # the subject and the body agree. (`sim.incidents[0]` has kept climbing since
    # — the row is a high-water mark of the whole campaign, the message is a
    # snapshot of the moment it became undeniable.)
    found = re.search(r"(\d+) addresses, (\d+) failed attempts", message["subject"])
    assert found, message["subject"]
    addresses, attempts = int(found.group(1)), int(found.group(2))
    assert addresses >= login_abuse.SPRAY_MIN_DISTINCT_EMAILS
    assert f"*Failed attempts:* {attempts} in the last 15 minutes" in body
    assert f"*Distinct addresses attempted:* {addresses}" in body
    assert sim.incidents[0]["attempt_count"] >= attempts


def test_the_abuse_alert_goes_to_the_security_channel_and_says_so(sent, sim):
    """This detector is the ONE thing in the app that posts to #security-alerts.
    An outage alert must never arrive there, and this must never arrive in
    #error-alerts while a security webhook exists — the channel routing is
    asserted end to end in tests/test_slack_alerts.py, and this is the check that
    the login path actually asks for it."""
    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)
    message = alerts(sent)[0]

    assert message["url"] == _FakeSettings.slack_security_webhook
    assert message["url"] != _FakeSettings.slack_webhook
    # Tagged, so it is still obviously an attack if the fallback ever puts it in
    # the error channel alongside 500s.
    assert message["subject"].startswith("SECURITY — ")


def test_the_alert_never_names_a_single_attempted_address(sent, sim):
    """The attempted addresses are unverified strings a stranger typed, some of
    them belong to real people, and a list of them is the scraped material the
    attacker was probing with. Re-publishing it into a Slack channel — where it is
    also an enumeration oracle — is exactly what must not happen."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    for message in sent:
        # Subject and body only — the captured `url` is the webhook the fixture
        # dialled, not content that was sent anywhere.
        text = message["subject"] + message["body"]
        assert "target0@byu.edu" not in text
        assert "@byu.edu" not in text
        # Nothing that even LOOKS like an address, so a future field that quietly
        # starts carrying one fails here rather than in the channel.
        assert "@" not in text


def test_the_alert_says_what_was_done_about_the_source(sent, sim):
    """The useful half of the message (#457). Before automatic blocking existed
    this alert ended by telling the reader to block the IP at the Vercel
    firewall — a control this account does not have. It now reports what already
    happened, with the expiry, so nobody has to go and do anything."""
    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)
    body = alerts(sent)[0]["body"]

    assert "*Action taken:* BLOCKED" in body
    assert "expires on its own" in body
    assert "Vercel firewall" not in body, "that instruction was never actionable"


def test_the_alert_says_when_a_source_was_deliberately_not_blocked(sent, sim):
    """The other outcome, and the one that would otherwise be baffling: an alert
    about a campaign with no block behind it. The reader has to be told the
    address was EXEMPT rather than left to assume the block failed."""
    sim.record_success(ip="159.26.103.94")  # a real sign-in from that address
    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)
    body = alerts(sent)[0]["body"]

    assert "*Action taken:* NOT blocked" in body
    assert "exempt" in body
    assert sim.blocks == []


def test_the_message_says_the_addresses_were_withheld_on_purpose(sent, sim):
    """So nobody has to wonder whether the list was omitted deliberately or the
    detector simply failed to collect it."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert "withheld on purpose" in alerts(sent)[0]["body"]


def test_the_pattern_names_the_shape_of_the_campaign():
    """Three fixed strings, never anything derived from input — this value is
    stored and then rendered into a Slack message."""
    assert "enumeration" in login_abuse.classify(attempts=222, distinct_emails=202)
    assert "spraying" in login_abuse.classify(attempts=338, distinct_emails=78)
    assert "guessing" in login_abuse.classify(attempts=60, distinct_emails=1)


# ------------------------------------------------- the constraint under it all --


MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "database"
    / "migrations"
    / "2026-08-19_login_abuse_incidents.sql"
)


def test_the_partial_unique_index_still_exists():
    """Every test above would still pass if this index were dropped — and the
    feature would silently become one message per serverless instance. It is the
    only thing making "one open incident per source" true, so it gets its own
    guard, exactly like the one in tests/test_failure_alert.py."""
    sql = MIGRATION.read_text(encoding="utf-8")

    index = re.search(
        r"CREATE\s+UNIQUE\s+INDEX[^;]*?ON\s+login_abuse_incidents\s*\(\s*environment\s*,"
        r"\s*ip_address\s*\)\s*WHERE\s+resolved_at\s+IS\s+NULL",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    assert index, "the one-open-incident-per-source index is missing"
    assert "ENABLE ROW LEVEL SECURITY" in sql, "new tables must be locked down"
    # The detector's only read is an aggregate over one source in a time window;
    # without this composite index that is a scan of every source in the window,
    # on a public route, under exactly the flood it exists to detect.
    assert "idx_login_failures_ip_occurred" in sql


def test_the_flood_tests_above_are_actually_testing_the_dedup(sent, sim, monkeypatch):
    """THE MUTATION CHECK, encoded so it cannot rot.

    Replace the claim with a naive one — "over threshold? then send" — and the
    Romania replay must go from one message to many. If this test ever fails,
    every "exactly one alert" assertion in this file has stopped meaning anything
    and is passing for some unrelated reason."""

    def naive_claim(self, params):
        for row in self.incidents:
            if row["abuse_incident_id"] == params["incident_id"]:
                return [dict(row)]
        return []

    monkeypatch.setattr(FakeAbuseData, "claim_alert", naive_claim)

    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)

    assert len(alerts(sent)) > 1, (
        "with the claim made naive this replay must flood; if it does not, the "
        "one-alert assertions elsewhere in this file prove nothing"
    )

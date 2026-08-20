"""Automatic login blocking, and above all the seven things it must never do (#457).

#456 detected the 2026-08-19 campaigns and blocked nothing, on the grounds that a
human could block the source at the Vercel firewall. That firewall is a Pro
feature and this account is on Hobby, so the owner asked for the obvious thing:

    "is there a way to block an ip if they try 5 different emails, that would
     have stopped these attacks way sooner"

The three real sources, replayed here as they were:

    66.234.153.26  (Romania)     190 attempts   68 addresses   over 10 minutes
    159.26.103.94  (Seattle WA)  338 attempts   78 addresses   over  6 minutes
    134.82.68.139  (Miami FL)    222 attempts  202 addresses   over 16 SECONDS

THE POINT OF THIS FILE IS THE NEGATIVE HALF. A block that stops those three is
easy; the hard part is that a block acts on an address the CLIENT SUPPLIES, so
the same mechanism that stops an attacker can lock the department out of its own
system if it is even slightly wrong. Most of what follows asserts that it does
not fire, and the mutation checks at the bottom exist because a safety test that
would pass with the safety removed is worse than no test at all.

The seven properties and where each is pinned:

  1. never block an address with a recent successful sign-in
     -> test_an_address_with_a_recent_successful_login_is_never_blocked
        test_a_forged_x_forwarded_for_cannot_lock_the_owner_out
        MUTATION: test_the_success_exemption_is_what_those_tests_depend_on
  2. blocks auto-expire
     -> test_a_block_expires_on_its_own
        test_nothing_has_to_run_for_a_block_to_lapse
        MUTATION: test_the_expiry_is_what_the_lapse_test_depends_on
  3. fail open when the store cannot be read
     -> test_an_unreadable_block_store_lets_the_login_through
        test_a_broken_block_store_cannot_break_the_route
  4. engineers stay able to sign in
     -> test_an_engineers_address_is_never_blocked_however_stale
        test_an_engineer_can_lift_a_block_from_anywhere
        MUTATION: test_the_engineer_exemption_is_what_that_test_depends_on
  5. durable, not in-memory
     -> test_a_block_is_visible_to_an_instance_that_never_saw_the_attack
        test_the_migration_still_declares_the_constraints
  6. scoped to the login path only
     -> test_only_the_two_login_routes_consult_the_block_store
  7. the refusal is not an enumeration oracle
     -> test_the_refusal_is_identical_for_a_real_and_a_fake_account
        test_a_blocked_caller_is_refused_before_the_address_is_looked_up

The fake database is :class:`tests.test_login_abuse.FakeAbuseData` — deliberately
the SAME one the detector's tests use, because in production the block rows, the
incident rows, the failures and the sign-ins are one database and one
transaction. It is a real implementation of the exemptions rather than a stub;
see its docstring for what that does and does not prove.
"""

import ast
import asyncio
import datetime
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.sql.elements import TextClause

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.login_failure import LoginFailure
from app.schemas.auth import UserContext
from app.services import alert_templates, login_abuse, login_block
from tests.test_login_abuse import (
    MIAMI,
    ROMANIA,
    SEATTLE,
    FakeAbuseData,
    FakeSession,
    _FakeSettings,
    _Result,
    replay,
)

UTC = datetime.UTC

PROVO = {"city": "Provo", "region": "Utah", "country": "US"}

# The owner's own address, in the two roles it plays in this file: the place he
# really signs in from, and the value an attacker would forge to lock him out.
OWNER_IP = "128.187.16.44"


@pytest.fixture
def sim(monkeypatch):
    """A shared fake database with both services pointed at it.

    ``login_abuse``'s in-process evaluation gate reads the same clock the SQL
    does, so a replay advances them together.
    """
    data = FakeAbuseData()
    login_abuse.reset()
    monkeypatch.setattr(
        login_abuse, "time", SimpleNamespace(monotonic=lambda: data.monotonic)
    )
    monkeypatch.setattr(login_abuse, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(login_block, "get_settings", lambda: _FakeSettings())
    return data


@pytest.fixture
def quiet(monkeypatch):
    """Alerting off, so these tests measure blocking and nothing else.

    Blocking deliberately does not sit behind the alerting gate, which is what
    makes this fixture possible at all — see
    tests/test_login_abuse.py::test_blocking_does_not_depend_on_the_alert_webhook.
    """
    monkeypatch.setattr(login_abuse.failure_alert, "alerting_enabled", lambda: False)


def blocked_for(sim, ip: str) -> int | None:
    """Seconds ``ip`` is refused for, through the real service call."""
    return asyncio.run(login_block.seconds_remaining(FakeSession(sim), ip_address=ip))


# ============================================================ 1. THE ATTACKS ==


def test_the_three_real_campaigns_are_all_blocked(sim, quiet):
    """The whole ask, on the real numbers. All three cross the threshold and all
    three end up refused — from the same measurement that already produced the
    Slack alert, so there is no second detector to keep in step."""
    for ip, attempts, addresses, seconds, geo in (
        ("66.234.153.26", 190, 68, 600, ROMANIA),
        ("159.26.103.94", 338, 78, 360, SEATTLE),
        ("134.82.68.139", 222, 202, 16, MIAMI),
    ):
        replay(sim, ip=ip, attempts=attempts, addresses=addresses, seconds=seconds, geo=geo)
        assert blocked_for(sim, ip) is not None, ip

    assert len(sim.blocks) == 3, "one block per source, not one per attempt"


def _blocked_after(sim, *, ip, attempts, addresses, seconds, geo) -> int | None:
    """Replay a campaign and return the attempt number at which it was stopped."""
    step = seconds / attempts
    stopped = None
    for n in range(attempts):
        sim.record_failure(ip=ip, email=f"target{n % addresses}@byu.edu")
        asyncio.run(login_abuse.observe_failure(FakeSession(sim), ip_address=ip, **geo))
        if stopped is None and sim.active_block(ip) is not None:
            stopped = n + 1
        sim.advance(step)
    return stopped


def test_each_campaign_is_stopped_in_its_first_seconds(sim, quiet):
    """WHEN the block lands, measured rather than asserted in prose.

        Romania   blocked at attempt   9 of 190   (28.4s into a 10-minute grind)
        Seattle   blocked at attempt  11 of 338   (11.7s into a 6-minute grind)
        Miami     blocked at attempt  71 of 222   ( 5.1s into a 16-second burst)

    91 attempts of 750 got through: the other 88% were refused. Nothing succeeded
    in either world, so what this buys is not "kept them out" — they were never
    getting in — it is that the flood stops itself instead of running to
    completion while the owner reads a Slack message.

    ⚠️ NOTE WHAT BOUNDS THE MIAMI CASE, because it decides the threshold
    question. 222 attempts in 16 seconds outruns the detector's in-process
    evaluation gate (one measurement per process per 5 seconds), so the block
    lands on the second tick no matter WHAT the threshold is — at five distinct
    addresses it is stopped at attempt 71 as well, byte for byte. The threshold
    only moves the two slower campaigns, and only by 4 and 5 attempts. That is
    the entire practical difference between the five that was asked for and the
    eight this ships with; see login_block's docstring for the cost on the other
    side of the trade.
    """
    romania = _blocked_after(
        sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA
    )
    seattle = _blocked_after(
        sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE
    )
    miami = _blocked_after(
        sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI
    )

    assert romania is not None and seattle is not None and miami is not None
    assert romania <= 15, romania
    assert seattle <= 20, seattle
    assert miami <= 80, miami
    assert romania + seattle + miami < 750 * 0.2, "under a fifth of the flood got through"


def test_the_block_and_the_alert_use_one_threshold():
    """No second number anywhere. ``login_block`` does not import the detector at
    all — ``login_abuse.evaluate`` decides with ``is_abusive`` and hands the
    already-measured counts across — so there is no way for the block, the Slack
    alert and the console's attack table to disagree about what an attack is, and
    retuning either constant moves all three in one edit."""
    assert not hasattr(login_block, "SPRAY_MIN_DISTINCT_EMAILS")
    assert not hasattr(login_block, "BURST_MIN_ATTEMPTS")
    # Parsed, not grepped: the prose in this module discusses the detector at
    # length and should. What must not exist is an executable reference to it.
    tree = ast.parse(Path(login_block.__file__).read_text(encoding="utf-8"))
    referenced = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom | ast.Import)
        for alias in node.names
    }
    assert "login_abuse" not in referenced, (
        "login_block must not re-derive the decision; login_abuse.evaluate calls "
        "is_abusive and passes the result in"
    )


# ================================================== 2. AND NOT A REAL PERSON ==


def test_a_person_mistyping_their_password_is_never_blocked(sim, quiet):
    """Four failures against one address. The single most important false
    positive to avoid, because it is the most common thing that happens."""
    replay(sim, ip=OWNER_IP, attempts=4, addresses=1, seconds=45, geo=PROVO)

    assert sim.blocks == []
    assert blocked_for(sim, OWNER_IP) is None


def test_a_confused_person_trying_four_of_their_addresses_is_never_blocked(sim, quiet):
    """The scenario that decided the threshold. Someone who cannot remember which
    address their account uses generates DISTINCT addresses, not repeats:
    ``jake@``, ``gunnjake@``, ``jake.gunn@``, ``jgunn@``. That is four — and at
    the five the owner asked for, one more typo would refuse them for an hour."""
    for address in ("jake", "gunnjake", "jake.gunn", "jgunn"):
        for _ in range(2):
            sim.record_failure(ip=OWNER_IP, email=f"{address}@byu.edu")
            asyncio.run(
                login_abuse.observe_failure(FakeSession(sim), ip_address=OWNER_IP)
            )
            sim.advance(15)

    assert sim.blocks == []


def test_a_shared_office_address_with_several_fumbling_staff_is_never_blocked(sim, quiet):
    """The realistic office case. ``ip_address`` is the client address, so a whole
    department behind one NAT shares one key: three people each failing four
    times is 12 attempts across 3 addresses. There are only about four accounts in
    this system, which is exactly why the threshold cannot sit at five."""
    for person in range(3):
        for _ in range(4):
            sim.record_failure(ip=OWNER_IP, email=f"staff{person}@byu.edu")
            asyncio.run(
                login_abuse.observe_failure(FakeSession(sim), ip_address=OWNER_IP)
            )
            sim.advance(20)

    assert sim.blocks == []
    assert blocked_for(sim, OWNER_IP) is None


# ========================= 3. PROPERTY 1 — THE SUCCESSFUL-LOGIN SHIELD ========


def test_an_address_with_a_recent_successful_login_is_never_blocked(sim, quiet):
    """THE SINGLE MOST IMPORTANT ASSERTION IN THIS FILE.

    The address crosses the threshold by a factor of twenty-five and is still not
    blocked, because somebody genuinely signed in from it recently."""
    sim.record_success(ip="159.26.103.94")
    sim.advance(3600)

    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)

    assert sim.blocks == [], "an address people really sign in from must never be blocked"
    assert blocked_for(sim, "159.26.103.94") is None


def test_a_forged_x_forwarded_for_cannot_lock_the_owner_out(sim, quiet):
    """The attack this exemption exists to defeat, stated as the attacker would
    run it.

    ``ip_address`` arrives in the request BODY, forwarded from
    ``x-forwarded-for``. An attacker who knows (or guesses) the office address
    puts it there, fails eight sign-ins against invented addresses, and — without
    this rule — the staff are refused for an hour from their own building. That
    converts a failed attack into a successful denial of service, which is
    strictly worse than the attack.

    The shield holds because it reads ``login_events``, and only an
    AUTHENTICATED caller can write one of those. The party who can forge the
    accusation cannot forge the defence."""
    sim.record_success(ip=OWNER_IP)  # the owner signed in this morning
    sim.advance(7200)

    # The attacker, claiming to be the owner's address.
    for n in range(40):
        sim.record_failure(ip=OWNER_IP, email=f"victim{n}@byu.edu")
        asyncio.run(login_abuse.observe_failure(FakeSession(sim), ip_address=OWNER_IP))
        sim.advance(2)

    assert blocked_for(sim, OWNER_IP) is None
    assert sim.blocks == []


def test_the_shield_lapses_once_the_sign_in_is_genuinely_old(sim, quiet):
    """It is a LOOKBACK, not a permanent whitelist. An address whose last real
    sign-in was months ago is blockable again — otherwise a recycled residential
    lease or a coffee-shop NAT would be immune forever."""
    sim.record_success(ip="159.26.103.94")
    sim.advance(login_block.SUCCESS_LOOKBACK_SECONDS + 86_400)

    replay(sim, ip="159.26.103.94", attempts=338, addresses=78, seconds=360, geo=SEATTLE)

    assert blocked_for(sim, "159.26.103.94") is not None


def test_the_success_exemption_is_what_those_tests_depend_on(sim, quiet, monkeypatch):
    """MUTATION CHECK for property 1, encoded so it cannot rot.

    Remove the successful-login clause from the store and the forged-header test
    above must start blocking the owner. If this ever fails, that test is passing
    for some unrelated reason and the most important safety property in this
    feature is untested."""
    original = FakeAbuseData.block

    def without_the_success_clause(self, params):
        # Same statement minus the first NOT EXISTS: pretend nobody ever signed in.
        keep, self.successes = self.successes, []
        try:
            return original(self, params)
        finally:
            self.successes = keep

    monkeypatch.setattr(FakeAbuseData, "block", without_the_success_clause)

    sim.record_success(ip=OWNER_IP)
    sim.advance(7200)
    replay(sim, ip=OWNER_IP, attempts=40, addresses=40, seconds=80, geo=PROVO)

    assert blocked_for(sim, OWNER_IP) is not None, (
        "with the successful-login exemption removed the owner's address must get "
        "blocked; if it does not, the tests above prove nothing"
    )


# ================================ 4. PROPERTY 4 — ENGINEERS ARE NEVER LOCKED ==


def test_an_engineers_address_is_never_blocked_however_stale(sim, quiet):
    """Same principle the maintenance switch is built on: the people who fix it
    must not be the people locked out by it.

    Note the clock. The engineer's last sign-in here is a YEAR old — far outside
    the successful-login lookback — and the address is still exempt, because the
    engineer clause has no time bound. Six weeks away is exactly when a
    forged-header attack on that address would be worth attempting."""
    sim.record_success(ip=OWNER_IP, engineer=True)
    sim.advance(365 * 24 * 3600)

    replay(sim, ip=OWNER_IP, attempts=200, addresses=90, seconds=600, geo=PROVO)

    assert sim.blocks == []
    assert blocked_for(sim, OWNER_IP) is None


def test_the_engineer_exemption_is_what_that_test_depends_on(sim, quiet, monkeypatch):
    """MUTATION CHECK for property 4. Drop the engineer clause and a year-old
    engineer sign-in stops shielding the address."""
    original = FakeAbuseData.block

    def without_the_engineer_clause(self, params):
        keep = [dict(s) for s in self.successes]
        for s in self.successes:
            s["engineer"] = False
        try:
            return original(self, params)
        finally:
            self.successes = keep

    monkeypatch.setattr(FakeAbuseData, "block", without_the_engineer_clause)

    sim.record_success(ip=OWNER_IP, engineer=True)
    sim.advance(365 * 24 * 3600)
    replay(sim, ip=OWNER_IP, attempts=200, addresses=90, seconds=600, geo=PROVO)

    assert blocked_for(sim, OWNER_IP) is not None, (
        "with the engineer exemption removed a stale engineer sign-in must stop "
        "shielding the address; if it does not, the test above proves nothing"
    )


# ============================================ 5. PROPERTY 2 — BLOCKS EXPIRE ===


def test_a_block_expires_on_its_own(sim, quiet):
    """An hour later the same address signs in normally. Nobody was woken up and
    nobody had to remember anything."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)
    assert blocked_for(sim, "134.82.68.139") is not None

    sim.advance(login_block.BLOCK_SECONDS + 1)

    assert blocked_for(sim, "134.82.68.139") is None


def test_nothing_has_to_run_for_a_block_to_lapse(sim, quiet):
    """The expiry is in the READ, not in a cleanup job. The row is still there —
    with its history — and is simply no longer in force. That is what makes a
    false positive heal itself even if every other part of this feature has
    stopped working."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)
    sim.advance(login_block.BLOCK_SECONDS + 1)

    assert blocked_for(sim, "134.82.68.139") is None
    assert len(sim.blocks) == 1, "expiry must not delete the row"
    assert sim.blocks[0]["lifted_at"] is None


def test_the_expiry_is_what_the_lapse_test_depends_on(sim, quiet, monkeypatch):
    """MUTATION CHECK for property 2. Take ``blocked_until > now()`` out of the
    read and an expired block keeps refusing people forever."""

    def ignoring_the_expiry(self, params):
        row = self._unlifted(params["environment"], params["ip"])
        return [] if row is None else [{"seconds_left": 1}]

    monkeypatch.setattr(FakeAbuseData, "is_blocked", ignoring_the_expiry)

    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)
    sim.advance(login_block.BLOCK_SECONDS * 100)

    assert blocked_for(sim, "134.82.68.139") is not None, (
        "with the expiry predicate removed a block must outlive its own deadline; "
        "if it does not, the expiry tests prove nothing"
    )


def test_a_block_can_be_extended_but_never_shortened(sim, quiet):
    """Re-arming takes GREATEST on the expiry. A source that keeps getting through
    stays blocked; a re-evaluation can never quietly cut an active block short."""
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)
    first = sim.blocks[0]["blocked_until"]

    sim.advance(600)
    replay(sim, ip="66.234.153.26", attempts=190, addresses=68, seconds=600, geo=ROMANIA)

    assert len(sim.blocks) == 1, "one row per source, re-armed in place"
    assert sim.blocks[0]["blocked_until"] >= first


# ================================================ 6. PROPERTY 3 — FAIL OPEN ===


class _ExplodingSession(FakeSession):
    """A session whose every statement fails, the way an unapplied migration or a
    dead database would look."""

    async def execute(self, statement, params=None):
        raise RuntimeError("relation \"login_ip_blocks\" does not exist")


def test_an_unreadable_block_store_lets_the_login_through(sim):
    """Property 3, at the service. The same argument ``maintenance.read_status``
    makes for the switch that can hide the whole site: a control that can refuse
    people must never refuse them because it could not be read."""
    result = asyncio.run(
        login_block.seconds_remaining(_ExplodingSession(sim), ip_address=OWNER_IP)
    )
    assert result is None


def test_a_missing_or_unforwarded_address_is_never_blocked(sim):
    """Nothing to match a block against. Bucketing every unattributed caller
    together would refuse people on the aggregate of strangers."""
    for value in (None, "", "   "):
        assert (
            asyncio.run(
                login_block.seconds_remaining(FakeSession(sim), ip_address=value)
            )
            is None
        )


def test_the_kill_switch_stops_enforcement_as_well_as_creation(sim, quiet, monkeypatch):
    """One switch, both halves. Flipping it off must UNBLOCK people immediately
    rather than only stopping new blocks — otherwise turning the feature off
    leaves everyone it already caught still refused."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)
    assert blocked_for(sim, "134.82.68.139") is not None

    monkeypatch.setattr(
        login_block,
        "get_settings",
        lambda: SimpleNamespace(environment="production", login_auto_block_enabled=False),
    )

    assert blocked_for(sim, "134.82.68.139") is None


# ================================================== 7. PROPERTY 5 — DURABLE ===


def test_a_block_is_visible_to_an_instance_that_never_saw_the_attack(sim, quiet):
    """The reason this is a table and not a counter.

    ``app/core/rate_limit.py`` keeps its windows in module memory, so on Vercel
    each warm instance has its own and shares it with nobody — which is exactly
    why it never fired on 2026-08-19. Here one 'instance' observes the campaign
    and a completely different session, with no shared memory, enforces it."""
    attacked = FakeSession(sim)
    for n in range(40):
        sim.record_failure(ip="134.82.68.139", email=f"target{n}@byu.edu")
        asyncio.run(
            login_abuse.observe_failure(attacked, ip_address="134.82.68.139", **MIAMI)
        )
        sim.advance(1)

    cold_instance = FakeSession(sim)
    seconds = asyncio.run(
        login_block.seconds_remaining(cold_instance, ip_address="134.82.68.139")
    )
    assert seconds is not None and seconds > 0


def test_concurrent_instances_produce_one_block_not_twenty(sim, quiet):
    """The partial unique index, from the serverless angle. Twenty instances all
    observe the same campaign at the same instant with no shared memory."""
    for n in range(40):
        sim.record_failure(ip="159.26.103.94", email=f"target{n}@byu.edu")

    async def scenario():
        return await asyncio.gather(
            *(
                login_block.apply(
                    FakeSession(sim),
                    ip_address="159.26.103.94",
                    attempts=40,
                    distinct_emails=40,
                    pattern="enumeration: many addresses, about one attempt each",
                )
                for _ in range(20)
            )
        )

    applied = asyncio.run(scenario())

    assert len(sim.blocks) == 1, "twenty instances must not open twenty blocks"
    assert {a["block_id"] for a in applied} == {sim.blocks[0]["block_id"]}


# ===================================== 8. PROPERTY 6 — SCOPED TO LOGIN ONLY ===


APP_DIR = Path(login_block.__file__).resolve().parent.parent

def _imports_block(source: str) -> bool:
    """True if this module imports ``login_block``, in either spelling.

    PARSED, NOT PATTERN-MATCHED. This was a line-anchored regex, and a regex over
    raw text cannot see an import the formatter has wrapped across lines
    (``from app.services import (`` / ``    login_block,`` / ``)``) — adding one
    more name to that import in ``admin.py`` is all it takes to make ruff wrap
    it. The regex then dropped a REAL call site out of the set below and failed
    this property test for a formatting change, which is the wrong alarm at the
    wrong time. The AST sees the import however it is spelled and, exactly like
    the anchored regex it replaces, never counts a mention inside a docstring or
    a comment.
    """
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if module == "app.services" and any(
            alias.name == "login_block" for alias in node.names
        ):
            return True
        if module == "app.services.login_block" or module.startswith(
            "app.services.login_block."
        ):
            return True
    return False


def test_only_the_two_login_routes_consult_the_block_store():
    """Property 6, asserted structurally because that is the only way to assert an
    ABSENCE across a whole application.

    The public survey is the one public page this system has and alumni worldwide
    use it; an over-broad block there would be far worse than the login block is
    good. So the set of modules that can reach ``login_block`` is pinned, and a
    future middleware or a survey route that imports it fails here rather than in
    production.
    """
    importers = {
        path.relative_to(APP_DIR).as_posix()
        for path in APP_DIR.rglob("*.py")
        if _imports_block(path.read_text(encoding="utf-8"))
    }
    assert importers == {
        "api/routes/auth.py",  # the two pre-login routes: enforcement
        "api/routes/admin.py",  # the engineer console: list + lift
        "services/login_abuse.py",  # the detector: creation
    }, importers


def test_the_survey_routes_do_not_touch_the_block_store():
    """Said again, directly, because it is the one that would hurt most."""
    survey = (APP_DIR / "api" / "routes" / "survey.py").read_text(encoding="utf-8")
    assert "login_block" not in survey


# ==================================== 9. PROPERTY 7 — NOT AN ENUMERATION ORACLE


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


class _RouteSession(FakeSession):
    """The fake database behind a real request to the login routes.

    Dispatches the detector's and the block store's statements to the shared
    :class:`FakeAbuseData`, and answers everything ``login_lockout`` and the
    retention purge touch. A ``LoginFailure`` handed to ``add`` is recorded as a
    failure row in the shared store, so a sequence of real HTTP requests drives
    the whole loop: record -> failure row -> measurement -> block -> refusal.

    ``user`` is the registered account a lookup by email finds, or None.
    """

    def __init__(self, data, user=None):
        super().__init__(data)
        self.user = user
        self.attempts: dict = {}
        self.added: list = []
        self.commits = 0
        self.email_lookups = 0

    async def execute(self, statement, params=None):
        try:
            return await super().execute(statement, params)
        except AssertionError:
            # The retention purge's two DELETEs — irrelevant here.
            return _Result([])

    async def scalar(self, _stmt):
        # login_lockout's "is this a registered account?" lookup. Counting it is
        # how the anti-enumeration tests below prove a blocked caller is refused
        # BEFORE the address is used for anything.
        self.email_lookups += 1
        return self.user

    async def get(self, model, pk):
        from app.models.login_attempt import LoginAttempt

        return self.attempts.get(pk) if model is LoginAttempt else None

    def add(self, obj):
        self.added.append(obj)
        from app.models.login_attempt import LoginAttempt

        if isinstance(obj, LoginAttempt):
            self.attempts[obj.email_lc] = obj
        if isinstance(obj, LoginFailure):
            self.data.record_failure(ip=obj.ip_address or "", email=obj.email)

    async def delete(self, obj):
        from app.models.login_attempt import LoginAttempt

        if isinstance(obj, LoginAttempt):
            self.attempts.pop(obj.email_lc, None)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


@pytest.fixture
def route(sim, quiet, monkeypatch):
    """A TestClient wired to the shared fake database."""
    session = _RouteSession(sim)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    with TestClient(app) as client:
        yield client, session, sim
    app.dependency_overrides.clear()


def _block_now(sim, ip: str) -> None:
    """Put ``ip`` under a real block, through the real service."""
    asyncio.run(
        login_block.apply(
            FakeSession(sim),
            ip_address=ip,
            attempts=222,
            distinct_emails=202,
            pattern="enumeration: many addresses, about one attempt each",
        )
    )


ATTACKER = {"ip_address": "134.82.68.139"}


def test_the_refusal_is_identical_for_a_real_and_a_fake_account(route):
    """Property 7. The login routes return ONE generic shape whatever email you
    send, and the block must not become the exception that separates a real
    account from an invented one."""
    client, session, sim = route
    _block_now(sim, ATTACKER["ip_address"])

    session.user = SimpleNamespace(locked_at=None, email="real@byu.edu")
    real = client.post(
        "/auth/login/precheck", json={"email": "real@byu.edu", "context": ATTACKER}
    )
    session.user = None
    fake = client.post(
        "/auth/login/precheck",
        json={"email": "definitely-not-an-account@example.org", "context": ATTACKER},
    )

    assert real.status_code == fake.status_code == 200
    assert real.json() == fake.json()
    assert real.json()["allowed"] is False


def test_the_refusal_introduces_no_new_reason_string(route):
    """It reuses ``cooldown`` — a time-boxed refusal that clears itself, which is
    exactly what a block is. The deployed frontend already collapses ``cooldown``
    and ``locked`` into one generic message, so this needs no frontend change and
    adds no state for an attacker to distinguish."""
    client, _session, sim = route
    _block_now(sim, ATTACKER["ip_address"])

    body = client.post(
        "/auth/login/precheck", json={"email": "x@byu.edu", "context": ATTACKER}
    ).json()

    assert set(body) == {"allowed", "reason", "retry_after_seconds"}
    assert body["reason"] == "cooldown"
    assert body["retry_after_seconds"] == pytest.approx(login_block.BLOCK_SECONDS, abs=5)


def test_a_blocked_caller_is_refused_before_the_address_is_looked_up(route):
    """Not even the QUERY PATTERN may differ by account. The route returns before
    ``login_lockout`` reads ``users`` at all, so there is no timing difference to
    measure and no row touched that depends on the submitted address."""
    client, session, sim = route
    _block_now(sim, ATTACKER["ip_address"])

    client.post("/auth/login/precheck", json={"email": "x@byu.edu", "context": ATTACKER})

    assert session.email_lookups == 0
    assert session.added == []


def test_a_blocked_source_writes_nothing_on_the_record_route(route):
    """A blocked source cannot create rows and cannot push anyone toward lockout.

    The second half matters more than it looks: the per-email counter keys on the
    EMAIL, so an attacker's reports of "victim@byu.edu failed" would otherwise
    still count against the victim. Declining to record them can only help — the
    only failures suppressed are from an address already barred from signing in.
    """
    client, session, sim = route
    _block_now(sim, ATTACKER["ip_address"])
    before = len(sim.failures)

    resp = client.post(
        "/auth/login/record",
        json={"email": "victim@byu.edu", "success": False, "context": ATTACKER},
    )

    assert resp.status_code == 200
    assert resp.json()["allowed"] is False
    assert session.added == [], "no login_attempts row, no login_failures row"
    assert len(sim.failures) == before
    assert session.attempts == {}


def test_an_unblocked_caller_is_completely_unaffected(route):
    """The other 99.9% of traffic. A normal failed sign-in still records, still
    counts toward the per-email cooldown, and still returns ``ok``."""
    client, session, _sim = route

    resp = client.post(
        "/auth/login/record",
        json={"email": "someone@byu.edu", "success": False, "context": {"ip_address": OWNER_IP}},
    )

    assert resp.json() == {"allowed": True, "reason": "ok", "retry_after_seconds": None}
    assert any(isinstance(o, LoginFailure) for o in session.added)


def test_a_client_that_sends_no_context_is_not_refused(route):
    """Backward compatibility, stated as a test. A frontend built before #457
    sends only ``email`` to the pre-check; it gets no block evaluation here and is
    caught on the record call instead. The feature degrades, it does not break."""
    client, _session, sim = route
    _block_now(sim, ATTACKER["ip_address"])

    body = client.post("/auth/login/precheck", json={"email": "x@byu.edu"}).json()

    assert body["allowed"] is True


def test_a_broken_block_store_cannot_break_the_route(route, monkeypatch):
    """Property 3, at the route. If the store raises, the login proceeds — the
    caller cannot tell the feature exists, let alone that it failed."""
    client, _session, _sim = route

    async def exploding(*args, **kwargs):
        raise RuntimeError("relation \"login_ip_blocks\" does not exist")

    monkeypatch.setattr(login_block, "_read_block", exploding)

    body = client.post(
        "/auth/login/precheck", json={"email": "x@byu.edu", "context": ATTACKER}
    ).json()

    assert body == {"allowed": True, "reason": "ok", "retry_after_seconds": None}


def test_the_whole_loop_over_real_requests(route):
    """End to end through the HTTP layer: an enumeration run stops itself.

    Each POST records a failure, the detector measures, and once the source
    crosses eight distinct addresses the next call is refused and stops writing.
    """
    client, _session, sim = route
    refused_at = None
    for n in range(40):
        resp = client.post(
            "/auth/login/record",
            json={
                "email": f"target{n}@byu.edu",
                "success": False,
                "context": {"ip_address": "134.82.68.139", "country": "US"},
            },
        )
        if refused_at is None and resp.json()["allowed"] is False:
            refused_at = n
            break
        sim.advance(6)  # past the detector's in-process evaluation interval

    assert refused_at is not None, "the campaign was never stopped"
    assert refused_at <= 12, refused_at


# =========================================== 10. THE ENGINEER'S CONTROLS ======


@pytest.fixture
def console(sim, monkeypatch):
    """The engineer console, wired to the shared fake database."""
    session = _RouteSession(sim)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        yield client, session, sim
    app.dependency_overrides.clear()


def test_an_engineer_can_see_the_active_blocks(console):
    client, _session, sim = console
    _block_now(sim, "134.82.68.139")

    body = client.get("/admin/login-ip-blocks").json()

    assert body["auto_block_enabled"] is True
    assert body["block_seconds"] == login_block.BLOCK_SECONDS
    assert [i["ip_address"] for i in body["items"]] == ["134.82.68.139"]
    assert body["items"][0]["active"] is True


def test_the_console_never_returns_an_attempted_address(console):
    """Same rule as the attack table and the Slack alert. The counts are what you
    act on; the addresses are unverified strings that stay in login_failures."""
    client, _session, sim = console
    _block_now(sim, "134.82.68.139")

    raw = client.get("/admin/login-ip-blocks").text

    assert "@" not in raw
    assert "distinct_email_count" in raw


def test_only_an_engineer_can_see_or_lift_blocks(console):
    client, _session, sim = console
    _block_now(sim, "134.82.68.139")
    block_id = sim.blocks[0]["block_id"]
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("super_admin")

    assert client.get("/admin/login-ip-blocks").status_code == 403
    assert client.delete(f"/admin/login-ip-blocks/{block_id}").status_code == 403


def test_an_engineer_can_lift_a_block_from_anywhere(console):
    """Property 4's recovery path. The gate is the engineer ROLE, not the caller's
    address — blocks are consulted only on the two unauthenticated pre-login
    routes, so an engineer signing in from a blocked address is unaffected and can
    always reach this endpoint to clear it."""
    client, _session, sim = console
    _block_now(sim, "134.82.68.139")
    block_id = sim.blocks[0]["block_id"]

    resp = client.delete(f"/admin/login-ip-blocks/{block_id}")

    assert resp.status_code == 200
    assert resp.json()["ip_address"] == "134.82.68.139"
    assert blocked_for(sim, "134.82.68.139") is None


def test_lifting_the_same_block_twice_is_a_clean_404(console):
    client, _session, sim = console
    _block_now(sim, "134.82.68.139")
    block_id = sim.blocks[0]["block_id"]

    assert client.delete(f"/admin/login-ip-blocks/{block_id}").status_code == 200
    assert client.delete(f"/admin/login-ip-blocks/{block_id}").status_code == 404


def test_a_lifted_source_is_not_immediately_re_blocked(sim, quiet):
    """A lift means "this was wrong". Without the grace period the next failed
    login would re-open the block and the console's lift control would be
    decorative — the false positive would outlive the fix."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)
    block_id = sim.blocks[0]["block_id"]
    asyncio.run(login_block.lift(FakeSession(sim), block_id=block_id, actor_user_id=1))
    assert blocked_for(sim, "134.82.68.139") is None

    sim.advance(3600)
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert blocked_for(sim, "134.82.68.139") is None, "a human override outranks the heuristic"


def test_the_lift_grace_does_expire(sim, quiet):
    """It is a grace period, not a permanent whitelist: a source lifted last week
    that starts a fresh campaign is blocked again."""
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)
    asyncio.run(
        login_block.lift(
            FakeSession(sim), block_id=sim.blocks[0]["block_id"], actor_user_id=1
        )
    )

    sim.advance(login_block.LIFT_GRACE_SECONDS + 3600)
    replay(sim, ip="134.82.68.139", attempts=222, addresses=202, seconds=16, geo=MIAMI)

    assert blocked_for(sim, "134.82.68.139") is not None


# =============================== 11. THE CONSTRAINTS UNDER IT ALL =============


MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "database"
    / "migrations"
    / "2026-08-19_login_ip_blocks.sql"
)
SCHEMA = Path(__file__).resolve().parent.parent / "database" / "schema.sql"
RLS = Path(__file__).resolve().parent.parent / "database" / "rls_lockdown.sql"


def test_the_migration_still_declares_the_constraints():
    """Every test above would still pass if these were dropped, and the feature
    would silently become "one block row per serverless instance, possibly
    permanent". They are the load-bearing part, so they get their own guard."""
    sql = MIGRATION.read_text(encoding="utf-8")

    assert re.search(
        r"CREATE\s+UNIQUE\s+INDEX[^;]*?ON\s+login_ip_blocks\s*\(\s*environment\s*,"
        r"\s*ip_address\s*\)\s*WHERE\s+lifted_at\s+IS\s+NULL",
        sql,
        re.IGNORECASE | re.DOTALL,
    ), "the one-active-block-per-source index is missing"
    # There must be no way to spell a permanent block.
    assert "blocked_until        timestamptz  NOT NULL" in sql
    assert "ck_login_ip_blocks_bounded" in sql
    assert "interval '24 hours'" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql, "new tables must be locked down"
    # The shield's index: the successful-login NOT EXISTS runs on a public route.
    assert "idx_login_events_ip_occurred" in sql


def test_the_schema_and_the_lockdown_agree():
    """The FERPA check enforces the lockdown file; this catches the omission
    before CI does, and pins the table into the canonical schema too."""
    assert "CREATE TABLE login_ip_blocks" in SCHEMA.read_text(encoding="utf-8")
    assert "ALTER TABLE login_ip_blocks ENABLE ROW LEVEL SECURITY;" in RLS.read_text(
        encoding="utf-8"
    )


def test_the_exemptions_are_in_the_statement_not_in_python():
    """The structural version of properties 1 and 4.

    Both exemptions are ``NOT EXISTS`` clauses inside the single statement that
    writes ``login_ip_blocks``, so no future caller and no half-finished refactor
    can create a block without them. If someone moves either one into a Python
    ``if``, this goes red — which is the whole reason it is written this way."""
    source = Path(login_block.__file__).read_text(encoding="utf-8")
    statement = source[source.index("_SQL_BLOCK = text(") : source.index("_SQL_LINK_INCIDENT")]

    assert statement.count("NOT EXISTS") == 3
    assert "FROM login_events" in statement
    assert ":engineer_role" in statement
    assert "lift_grace_seconds" in statement
    # And only ONE statement may insert into the table.
    assert source.count("INSERT INTO login_ip_blocks") == 1


# ============================== 9. THE SQL IS SQLALCHEMY-LEGAL, NOT JUST SQL ===
#
# Everything above this line drives a FAKE database. That is the right trade for
# testing the policy — the exemptions, the expiry, the fail-open — but it leaves
# exactly one hole, and the first run against a real Postgres fell straight into
# it: `text()` does not bind `:name::type`. SQLAlchemy's placeholder pattern
# refuses a name followed by a colon, so every parameter written with a
# Postgres-style cast stayed in the statement as literal text and the INSERT died
# with `syntax error at or near ":"`.
#
# It failed SILENTLY in the place it matters most. `apply` runs inside
# `login_abuse.evaluate`, whose caller swallows every exception so detection can
# never break a login response — so the feature would have shipped, enforced
# nothing, created nothing, and logged one warning nobody reads. The tests were
# all green.
#
# These two are the cheap structural guard: no database, no fixtures, just "did
# SQLAlchemy actually see every placeholder you wrote".


def _sql_statements(module):
    """Every ``text()`` statement defined at module level, by name."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.startswith("_SQL_") and isinstance(value, TextClause)
    }


#: A ``:placeholder``, ignoring ``::casts`` (the char before is never a colon)
#: and anything in a ``--`` comment (stripped before the scan).
_PLACEHOLDER = re.compile(r"(?<![:\w]):([a-z_][a-z0-9_]*)", re.IGNORECASE)


def _written_placeholders(stmt) -> set[str]:
    sql = "\n".join(
        line.split("--")[0] for line in str(stmt).splitlines()
    )
    return set(_PLACEHOLDER.findall(sql))


# ``alert_templates`` is in this list because it is the next module in the app
# to write ``text()`` statements, and the trap is not specific to blocking:
# any new module with raw SQL belongs here, or it ships with the same
# invisible-until-production bug.
@pytest.mark.parametrize("module", [login_block, login_abuse, alert_templates])
def test_every_placeholder_written_is_a_placeholder_sqlalchemy_bound(module):
    """The bug this file exists to never repeat.

    For each statement, the names WRITTEN as ``:name`` must equal the names
    SQLAlchemy actually registered as bind parameters. They diverge the moment
    someone writes ``:name::int`` — the cast swallows the parameter, the literal
    text reaches Postgres, and the statement is a syntax error against a real
    database while every faked test in this file still passes.

    Use ``CAST(:name AS int)`` instead. It is uglier and it works.
    """
    for name, stmt in _sql_statements(module).items():
        written = _written_placeholders(stmt)
        bound = set(stmt._bindparams)
        assert written == bound, (
            f"{module.__name__}.{name}: SQLAlchemy did not bind "
            f"{sorted(written - bound)} (a ':name::type' cast swallows the "
            f"parameter — write CAST(:name AS type))"
        )


def test_apply_passes_exactly_the_parameters_the_block_statement_needs():
    """A missing parameter is the other way this dies at runtime and nowhere else.

    ``session.execute`` raises on an unbound parameter, inside the same swallowed
    path, so the failure mode is identical: nothing blocked, nothing said.
    """
    source = Path(login_block.__file__).read_text(encoding="utf-8")
    call = source[source.index("async def apply(") :]
    call = call[: call.index("async def link_incident")]

    for name in sorted(set(login_block._SQL_BLOCK._bindparams)):
        assert f'"{name}"' in call, f"apply() never passes :{name}"

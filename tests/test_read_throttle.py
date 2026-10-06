"""Per-user bulk-read throttle + volume alert (2026-10-02 breach review).

No database. Every request here is shaped to PASS the limiter dependency and
then fail validation (422) — the limiter is a dependency, so it runs (and
counts) first; once the budget is spent the route answers 429 before
validation. Same technique as the #112a mutation-limit tests.
"""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core import rate_limit
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import failure_alert

_BROWSE_PER_MINUTE = rate_limit._BROWSE_WINDOWS[0][0]
_EXPORT_PER_TEN_MIN = rate_limit._EXPORT_WINDOWS[0][0]

# Cheap, DB-free browse requests: each passes the limiter, then 422s.
_LIST = "/alumni?limit=0"
_NOTES = "/notes"  # missing entity_type / entity_id
_HEADSHOTS = "/alumni/headshots/urls"  # missing alumni_ids


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _as(*roles: str, user_id: int = 1) -> None:
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(
        *roles, user_id=user_id
    )


async def _no_db_session():
    yield None


@pytest.fixture
def client():
    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def alerts(monkeypatch):
    """Capture SECURITY deliveries instead of sending them."""
    sent: list[dict] = []

    async def _fake_deliver(subject, intro, rows, *, purpose, slack_summary=None):
        sent.append(
            {
                "subject": subject,
                "intro": intro,
                "rows": rows,
                "purpose": purpose,
                "summary": slack_summary,
            }
        )
        return True

    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: True)
    monkeypatch.setattr(failure_alert, "deliver_alert", _fake_deliver)
    return sent


def _spend_browse(client, n: int) -> list[int]:
    return [client.get(_LIST).status_code for _ in range(n)]


# --- normal use is never throttled -------------------------------------------


def test_heavy_but_human_browsing_is_not_throttled(client, alerts):
    # A minute of clicking as fast as pages render: ~40 navigations, each a
    # profile-ish trio (list / notes / headshot batch). Half the budget.
    _as("view_only")
    statuses = []
    for _ in range(40):
        statuses.append(client.get(_LIST).status_code)
        statuses.append(client.get(_NOTES).status_code)
        statuses.append(client.get(_HEADSHOTS).status_code)
    assert 429 not in statuses
    assert set(statuses) == {422}
    assert alerts == []


def test_limits_leave_wide_margin_over_measured_use():
    # Guard against someone "tightening" these into staff's way: the comment in
    # rate_limit.py measures ~120 browse hits/min as the physical ceiling.
    (per_min, _), (per_hour, _) = rate_limit._BROWSE_WINDOWS
    assert per_min >= 240
    assert per_hour >= 2400
    (export_short, _), (export_hour, _) = rate_limit._EXPORT_WINDOWS
    assert export_short >= 20
    assert export_hour >= 60
    # ...and export stays much tighter than browsing.
    assert export_hour < per_min


# --- the brake ---------------------------------------------------------------


def test_browse_budget_exhausted_returns_429(client, alerts):
    _as("view_only")
    seen = _spend_browse(client, _BROWSE_PER_MINUTE + 1)
    assert seen[:_BROWSE_PER_MINUTE] == [422] * _BROWSE_PER_MINUTE
    assert seen[-1] == 429
    blocked = client.get(_NOTES)  # same bucket, different route
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert blocked.headers.get("Retry-After") == "60"


def test_browse_budget_is_per_user_not_per_ip(client, alerts):
    # Every TestClient request shares one client IP — exactly the campus-NAT
    # situation. User 1 burning their budget must not touch user 2's.
    _as("view_only", user_id=1)
    assert _spend_browse(client, _BROWSE_PER_MINUTE + 1)[-1] == 429
    _as("view_only", user_id=2)
    assert client.get(_LIST).status_code == 422
    _as("view_only", user_id=1)
    assert client.get(_LIST).status_code == 429


def test_engineer_is_not_exempt(client, alerts):
    # Follows precedent: no limiter in rate_limit.py exempts a role.
    _as("engineer")
    assert _spend_browse(client, _BROWSE_PER_MINUTE + 1)[-1] == 429


def test_export_bucket_is_separate_and_tighter(client, alerts):
    _as("super_admin")
    # Spend the whole browse budget: exports are unaffected.
    assert _spend_browse(client, _BROWSE_PER_MINUTE + 1)[-1] == 429
    seen = [
        client.post("/alumni/export", json={"columns": []}).status_code
        for _ in range(_EXPORT_PER_TEN_MIN + 1)
    ]
    assert seen[:_EXPORT_PER_TEN_MIN] == [422] * _EXPORT_PER_TEN_MIN
    assert seen[-1] == 429


def test_export_budget_shared_across_export_routes(client, alerts):
    # The alumni export (alumni.export guard) and the opportunity-link export
    # (view guard) spend ONE export budget.
    _as("super_admin")
    for _ in range(_EXPORT_PER_TEN_MIN):
        assert (
            client.post("/alumni/export", json={"columns": []}).status_code == 422
        )
    assert client.get("/opportunity-links/export?status=bogus").status_code == 429
    # A different user is unaffected.
    _as("super_admin", user_id=2)
    assert client.get("/opportunity-links/export?status=bogus").status_code == 422


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/alumni/1/headshot"),
        ("get", "/alumni/1"),
        ("get", "/alumni/1/profile"),
    ],
)
def test_single_record_reads_are_braked(client, alerts, method, path):
    # Spend the budget on the list, then the single-record read is refused
    # before its handler (and its database) is ever reached.
    _as("view_only")
    _spend_browse(client, _BROWSE_PER_MINUTE)
    assert getattr(client, method)(path).status_code == 429


@pytest.mark.parametrize(
    "path",
    [
        "/alumni/1/export",
        "/alumni/import/update/export",
        "/events/1/attendees/export",
        "/survey/schedules/no-reply/export",
        "/survey/schedules/2020/no-reply/export",
    ],
)
def test_every_export_route_is_braked(client, alerts, path):
    _as("super_admin")
    for _ in range(_EXPORT_PER_TEN_MIN):
        client.post("/alumni/export", json={"columns": []})
    assert client.get(path).status_code == 429


# --- the alert -----------------------------------------------------------------


def test_one_alert_per_runaway_client_without_pii(client, alerts, caplog):
    _as("view_only", user_id=7)
    _spend_browse(client, _BROWSE_PER_MINUTE)
    with caplog.at_level(logging.WARNING, logger="security"):
        # The tripping call is a profile read with an alumni id in the path.
        assert client.get("/alumni/8421/profile").status_code == 429
        # A runaway client keeps hammering: still exactly ONE alert.
        for _ in range(50):
            assert client.get(_LIST).status_code == 429

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["purpose"] == failure_alert.SECURITY
    text = " ".join(
        [alert["subject"], alert["intro"], alert["summary"]]
        + [f"{k} {v}" for k, v in alert["rows"]]
    )
    assert "7" in alert["subject"]
    assert "read:browse" in text
    assert "/alumni/{alumni_id}/profile" in text
    # No raw alumni id: the path is templated before it leaves the process.
    assert "8421" not in text

    events = [r for r in caplog.records if "read_throttled" in r.getMessage()]
    assert len(events) == 1
    assert "8421" not in events[0].getMessage()


def test_alerts_are_per_user(client, alerts):
    for uid in (1, 2):
        _as("view_only", user_id=uid)
        _spend_browse(client, _BROWSE_PER_MINUTE + 3)
    # One alert for EACH user: one user's cooldown must not silence another's.
    users = [dict(a["rows"])["User id"] for a in alerts]
    assert users == ["1", "2"]


def test_alert_failure_never_changes_the_429(client, monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("slack down")

    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: True)
    monkeypatch.setattr(failure_alert, "deliver_alert", _boom)
    _as("view_only")
    assert _spend_browse(client, _BROWSE_PER_MINUTE + 1)[-1] == 429


def test_alert_fires_again_after_cooldown(monkeypatch, alerts):
    import asyncio
    from types import SimpleNamespace

    clock = {"now": 10_000.0}
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock["now"])
    request = SimpleNamespace(
        method="GET",
        url=SimpleNamespace(path="/alumni"),
        scope={"path_params": {}},
    )
    windows = ((1, 60.0),)

    async def _trip():
        await rate_limit._alert_read_throttle(request, "read:browse", 3, windows)

    asyncio.run(_trip())
    asyncio.run(_trip())
    assert len(alerts) == 1
    clock["now"] += rate_limit._READ_ALERT_COOLDOWN_SECONDS + 1
    asyncio.run(_trip())
    assert len(alerts) == 2


# --- the windows -----------------------------------------------------------------


def test_long_window_catches_a_slow_walk_and_refusals_record_nothing(monkeypatch):
    clock = {"now": 50_000.0}
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock["now"])
    windows = ((5, 60.0), (8, 3600.0))

    def hit():
        rate_limit._check_windows("t:walk", 1, windows)

    for _ in range(5):
        hit()
    with pytest.raises(Exception) as exc:
        hit()
    assert exc.value.status_code == 429
    # Move past the minute window: it has room again, the hour window has 3.
    clock["now"] += 61
    for _ in range(3):
        hit()
    with pytest.raises(Exception) as exc:
        hit()  # minute window has room (3/5), the hour window is full (8/8)
    assert exc.value.status_code == 429
    # The refused call spent nothing in the minute window.
    minute = rate_limit._WINDOWS["t:walk:60s"][1]
    assert len([t for t in minute if t > clock["now"] - 60]) == 3

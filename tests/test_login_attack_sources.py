"""The engineer console's attack table: GET /admin/login-attack-sources (#456).

The Maintenance page needed the picture the owner had to assemble by hand on
2026-08-19 — one row per attacking source rather than 750 rows of attempts. This
covers the endpoint that backs it: the engineer gate, the window/limit caps, the
per-source roll-up, and two properties that are the whole point of the feature.

    1. THE RESPONSE CANNOT CARRY AN ATTEMPTED EMAIL ADDRESS. Counts, never
       addresses. Asserted against the serialised JSON, not against the field
       list, so adding a leaking field later fails here rather than in review.

    2. THE TABLE AND THE ALERT AGREE. `attack_type` is asserted to be exactly
       what `login_abuse.classify` produces for the same numbers, on the real
       prod figures. If someone re-derives the classification in the route, the
       two surfaces drift and these go red.

The three attack scenarios are the real production numbers from 2026-08-19:

    66.234.153.26  (Romania)     190 attempts   68 addresses   over 10 minutes
    159.26.103.94  (Seattle WA)  338 attempts   78 addresses   over  6 minutes
    134.82.68.139  (Miami FL)    222 attempts  202 addresses   over 16 SECONDS

and the counter-case is the one the table must not label an attack: a staff
member mistyping their password.

Fake session in the style of test_login_failures.py — no DB. What that cannot
prove is the SQL itself; the aggregate's shape is pinned by a separate test that
reads the statement text, and the index question is argued in the service
docstring rather than asserted.
"""

import datetime
import json
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.schemas.auth import UserContext
from app.services import login_abuse

UTC = datetime.UTC


def _ctx(*roles: str, user_id: int = 1) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


# --------------------------------------------------------------- the fake DB --


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def mappings(self):
        return self

    def all(self):
        return [dict(r) for r in self._rows]


class _SourcesSession:
    """Fake session for list_login_attack_sources.

    ``execute`` answers the one aggregate with canned group rows and records the
    bound parameters, so the window/limit the route actually applied can be
    asserted rather than assumed. ``add``/``commit`` capture the read-audit.
    """

    def __init__(self, rows):
        self.rows = rows
        self.params: list[dict] = []
        self.added: list = []
        self.commits = 0

    async def execute(self, _stmt, params=None):
        self.params.append(dict(params or {}))
        return _Result(self.rows)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _group(
    ip: str,
    *,
    attempts: int,
    distinct: int,
    first: datetime.datetime,
    last: datetime.datetime,
    city: str | None = None,
    region: str | None = None,
    country: str | None = None,
) -> dict:
    """One row as the GROUP BY would return it."""
    return {
        "ip_address": ip,
        "attempts": attempts,
        "distinct_emails": distinct,
        "first_seen": first,
        "last_seen": last,
        "city": city,
        "region": region,
        "country": country,
    }


# The real campaigns, with their real clocks.
_ROMANIA = _group(
    "66.234.153.26",
    attempts=190,
    distinct=68,
    first=datetime.datetime(2026, 8, 19, 8, 47, 12, tzinfo=UTC),
    last=datetime.datetime(2026, 8, 19, 8, 57, 23, tzinfo=UTC),
    city="Bucharest",
    country="RO",
)
_SEATTLE = _group(
    "159.26.103.94",
    attempts=338,
    distinct=78,
    first=datetime.datetime(2026, 8, 19, 8, 58, 55, tzinfo=UTC),
    last=datetime.datetime(2026, 8, 19, 9, 5, 12, tzinfo=UTC),
    city="Seattle",
    region="Washington",
    country="US",
)
_MIAMI = _group(
    "134.82.68.139",
    attempts=222,
    distinct=202,
    first=datetime.datetime(2026, 8, 19, 9, 25, 36, tzinfo=UTC),
    last=datetime.datetime(2026, 8, 19, 9, 25, 52, tzinfo=UTC),
    city="Miami",
    region="Florida",
    country="US",
)
# A staff member fumbling their own password four times. Must never read as one.
_TYPO = _group(
    "128.187.1.10",
    attempts=4,
    distinct=1,
    first=datetime.datetime(2026, 8, 19, 14, 2, 0, tzinfo=UTC),
    last=datetime.datetime(2026, 8, 19, 14, 3, 30, tzinfo=UTC),
    city="Provo",
    region="Utah",
    country="US",
)


def _get(rows, query: str = "", *, roles=("engineer",), user_id: int = 1):
    session = _SourcesSession(rows)

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    if roles is not None:
        app.dependency_overrides[get_current_db_user] = lambda: _ctx(
            *roles, user_id=user_id
        )
    with TestClient(app) as client:
        resp = client.get(f"/admin/login-attack-sources{query}")
    app.dependency_overrides.clear()
    return resp, session


# ------------------------------------------------------------------- the gate --


def test_requires_auth():
    resp, _ = _get([], roles=None)
    assert resp.status_code == 401


def test_forbidden_below_engineer():
    """Engineer-gated exactly like /login-failures — a super_admin is refused."""
    resp, _ = _get([], roles=("super_admin",))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


# ------------------------------------------------------- the roll-up itself --


def test_returns_one_row_per_source_with_counts_and_window():
    resp, _ = _get([_SEATTLE, _MIAMI, _ROMANIA])
    assert resp.status_code == 200
    body = resp.json()

    assert [r["ip_address"] for r in body["items"]] == [
        "159.26.103.94",
        "134.82.68.139",
        "66.234.153.26",
    ]
    seattle = body["items"][0]
    assert seattle["attempts"] == 338
    assert seattle["distinct_emails"] == 78
    assert seattle["city"] == "Seattle"
    assert seattle["region"] == "Washington"
    assert seattle["country"] == "US"
    # Both ends of the source's activity, so the console can show a duration.
    assert seattle["first_seen"].startswith("2026-08-19T08:58:55")
    assert seattle["last_seen"].startswith("2026-08-19T09:05:12")


def test_window_and_limit_are_echoed_and_applied():
    """What the route says it applied is what it bound into the query."""
    resp, session = _get([], "?hours=6&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["window_hours"] == 6
    assert body["limit"] == 10
    assert session.params[0] == {"window_seconds": 6 * 3600, "limit": 10}


def test_defaults_to_a_recent_window():
    """No params: a day of history, 50 sources. Sensible for a page you open
    during an incident, and bounded so it cannot aggregate all of history."""
    resp, session = _get([])
    body = resp.json()
    assert body["window_hours"] == 24
    assert body["limit"] == 50
    assert session.params[0]["window_seconds"] == 24 * 3600


@pytest.mark.parametrize(
    "query",
    ["?hours=0", "?hours=169", "?limit=0", "?limit=201", "?hours=-1"],
)
def test_window_and_limit_are_capped(query):
    """A week of window and 200 sources are the ceilings; below/above is a 422
    before any query runs."""
    resp, session = _get([], query)
    assert resp.status_code == 422
    assert session.params == []


def test_empty_window_is_a_valid_answer_not_an_error():
    """The state the page shows on a normal day: no sources, still a 200 with the
    window, so the console can say "nothing in the last 24 hours"."""
    resp, _ = _get([])
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "window_hours": 24, "limit": 50}


def test_read_is_audited_with_the_applied_parameters():
    resp, session = _get([_ROMANIA], "?hours=3&limit=5", user_id=7)
    assert resp.status_code == 200
    audit = next(a for a in session.added if type(a).__name__ == "AuditLog")
    assert audit.action_type == "read_login_attack_sources"
    assert audit.entity_type == "login_failure"
    assert audit.user_id == 7
    assert audit.field_name == "hours=3;limit=5"
    assert session.commits == 1


def test_missing_geo_is_null_not_a_fabricated_location():
    resp, _ = _get(
        [
            _group(
                "10.0.0.4",
                attempts=40,
                distinct=2,
                first=datetime.datetime(2026, 8, 19, 1, 0, tzinfo=UTC),
                last=datetime.datetime(2026, 8, 19, 1, 5, tzinfo=UTC),
            )
        ]
    )
    row = resp.json()["items"][0]
    assert row["city"] is None and row["region"] is None and row["country"] is None


# ------------------------------------------- property 1: no address ever leaks --


def test_response_never_contains_an_attempted_email_address():
    """THE non-negotiable one.

    Asserted against the whole serialised body rather than a field list, so a
    future field that happens to carry an address fails here. The counts survive
    — those are what the reader acts on.
    """
    resp, _ = _get([_ROMANIA, _SEATTLE, _MIAMI, _TYPO])
    raw = json.dumps(resp.json())

    assert "@" not in raw
    for field in ("email", "emails", "addresses", "attempted"):
        assert f'"{field}"' not in raw
    # ...while the count is present and correct.
    assert resp.json()["items"][0]["distinct_emails"] == 68


def test_schema_exposes_a_count_and_no_address_field():
    """Belt-and-braces on the contract itself: the generated frontend type must
    not even have somewhere to put an address."""
    from app.api.routes.admin import LoginAttackSource

    fields = set(LoginAttackSource.model_fields)
    assert "distinct_emails" in fields
    assert not {f for f in fields if "email" in f} - {"distinct_emails"}


def test_aggregate_selects_no_email_column():
    """The SQL counts DISTINCT email and selects the column nowhere else — the
    response cannot leak what the query never fetched."""
    sql = str(login_abuse._SQL_SOURCES)
    assert "count(DISTINCT email)" in sql
    # The COLUMN appears exactly once, inside that aggregate — the `_emails`
    # suffix of the output alias is not a reference to it.
    assert re.findall(r"\bemail\b", sql) == ["email"]
    # Grouping is per source, and unattributable attempts are excluded rather
    # than collapsed into one fake "unknown" campaign.
    assert "GROUP BY ip_address" in sql
    assert "ip_address IS NOT NULL" in sql


# ------------------------- property 2: the table and the alert cannot disagree --


@pytest.mark.parametrize(
    "row,expected_word",
    [
        (_ROMANIA, "spraying"),
        (_SEATTLE, "spraying"),
        (_MIAMI, "enumeration"),
    ],
)
def test_real_campaigns_are_classified_as_attacks(row, expected_word):
    resp, _ = _get([row])
    item = resp.json()["items"][0]
    assert item["is_attack"] is True
    assert expected_word in item["attack_type"]


def test_attack_type_is_exactly_what_the_alert_would_say():
    """Same numbers, same words. If the route ever re-derives the shape instead
    of calling the shared classifier, this is what catches it."""
    resp, _ = _get([_ROMANIA, _SEATTLE, _MIAMI])
    for item, row in zip(
        resp.json()["items"], (_ROMANIA, _SEATTLE, _MIAMI), strict=True
    ):
        assert item["attack_type"] == login_abuse.classify(
            attempts=row["attempts"], distinct_emails=row["distinct_emails"]
        )


def test_a_person_mistyping_their_password_is_not_an_attack():
    """Four failures against one address. The row is still SHOWN — seeing it is
    the reassurance — but it is not labelled as a campaign."""
    resp, _ = _get([_TYPO])
    item = resp.json()["items"][0]
    assert item["is_attack"] is False
    assert item["attack_type"] == login_abuse.NOT_AN_ATTACK
    for word in ("spraying", "enumeration", "guessing"):
        assert word not in item["attack_type"]


def test_guessing_shape_is_reported_even_against_one_address():
    """The shape absent from the 2026-08-19 data: many passwords, one address.
    Volume alone crosses the threshold, and the label says which rule fired."""
    resp, _ = _get(
        [
            _group(
                "203.0.113.7",
                attempts=60,
                distinct=1,
                first=datetime.datetime(2026, 8, 19, 3, 0, tzinfo=UTC),
                last=datetime.datetime(2026, 8, 19, 3, 20, tzinfo=UTC),
            )
        ]
    )
    item = resp.json()["items"][0]
    assert item["is_attack"] is True
    assert "guessing" in item["attack_type"]


def test_classify_source_wraps_the_shared_rules_at_every_boundary():
    """Unit-level: the wrapper must add exactly one thing — the below-threshold
    label — and delegate everything else unchanged."""
    for attempts, distinct in [(1, 1), (4, 1), (29, 7), (12, 3)]:
        assert not login_abuse.is_abusive(attempts, distinct)
        assert login_abuse.classify_source(attempts, distinct) == (
            login_abuse.NOT_AN_ATTACK
        )
    # Exactly at each threshold it becomes an attack, worded by `classify`.
    for attempts, distinct in [
        (8, login_abuse.SPRAY_MIN_DISTINCT_EMAILS),
        (login_abuse.BURST_MIN_ATTEMPTS, 1),
        (190, 68),
    ]:
        assert login_abuse.is_abusive(attempts, distinct)
        assert login_abuse.classify_source(attempts, distinct) == (
            login_abuse.classify(attempts, distinct)
        )

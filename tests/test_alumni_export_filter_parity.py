"""Export/list population parity for the filters added in #366.

``AlumniExportFilters`` had no field for ``near``/``radius``, ``designations``,
``graduate_degree``, ``spoke_after``/``spoke_before`` or ``missing_phone``, so an
export launched from a view using any of them resolved to a WIDER population
than the list showed — a disclosure defect (a "near Provo" list exported every
alumnus nationwide), not a cosmetic mismatch.

The assertion that actually pins the bug class is *population equality*: for each
filter, the statement ``POST /alumni/export`` builds must compile to the SAME SQL
(and the same bind parameters) as the one ``GET /alumni`` builds for the
equivalent query param. A test that merely checks the field exists on the model
would pass even if the field were dropped on the way to the query builder.

The list side is captured from the REAL route (``service.list_alumni`` is stubbed
and its kwargs recorded), so a route that forgets to forward a param fails here
too — the parity is asserted end-to-end, not between two hand-written calls.

#775 adds ``missing_linkedin`` and ``missing_photo`` to the same regime.
``missing_photo`` is the interesting one: a headshot is an object in the
``headshots`` bucket rather than a column, so BOTH sides must resolve it through
``headshot_index`` (the ``near`` pattern) instead of one side quietly dropping
the flag — which would export every alumnus for a report the operator scoped to
the ones with no photo.
"""

import asyncio
import inspect
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.dependencies.auth import get_current_db_user
from app.api.routes.alumni import list_alumni
from app.core.database import get_session
from app.core.errors import ServiceError
from app.main import app
from app.repositories.alumni import build_alumni_query
from app.schemas.alumni_export import AlumniExportFilters
from app.schemas.auth import UserContext
from app.services import alumni as alumni_service
from app.services import headshot_index
from app.services.alumni_export import build_export_query


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles) or ["full_access"],
    )


# --- fake session -------------------------------------------------------------
#
# The only DB work either path does while BUILDING its query is the geocoding
# lookup behind ``near``: ``resolve_location`` asks for the named city's
# coordinates, then for every city within the radius. This fake answers those two
# questions in that order, so the list and the export each get an identical key
# set and any difference in the compiled SQL is a real wiring difference.

_CITY_COORDS = [("provo", "UT", 40.2338, -111.6585)]
_CITY_KEYS = [("provo", "UT"), ("orem", "UT"), ("springville", "UT")]


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _GeoSession:
    """Alternates coordinate rows / radius key rows — one pair per resolve."""

    def __init__(self):
        self.calls = 0

    async def execute(self, stmt):
        rows = _CITY_COORDS if self.calls % 2 == 0 else _CITY_KEYS
        self.calls += 1
        return _Rows(rows)

    async def scalar(self, stmt):
        return 0


@pytest.fixture
def session():
    return _GeoSession()


@pytest.fixture
def client(session):
    async def _override():
        yield session

    app.dependency_overrides[get_session] = _override
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("full_access")
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


class _FakeBucket:
    """One page of headshot keys, shared by both sides of every parity check."""

    def __init__(self):
        self.error: Exception | None = None

    async def list_objects(self, bucket, *, prefix="", limit=100, offset=0):
        if self.error is not None:
            raise self.error
        if offset:
            return []
        return [
            {"name": key, "metadata": {"size": 1234, "mimetype": "image/jpeg"}}
            for key in ("jdoe12", "asmith3")
        ]


@pytest.fixture
def bucket(monkeypatch):
    """Fake the headshots listing and start every test from a cold cache.

    The cache is dropped afterwards too: a key set left behind would leak into
    an unrelated test as a filter that "mysteriously" matches.
    """
    fake = _FakeBucket()
    monkeypatch.setattr(headshot_index.storage, "list_objects", fake.list_objects)
    headshot_index.reset_cache()
    yield fake
    headshot_index.reset_cache()


@pytest.fixture
def captured(monkeypatch):
    """Record the filter kwargs GET /alumni hands the query builder."""
    seen: dict = {}

    async def _fake_list_alumni(_session, **kwargs):
        seen.clear()
        seen.update(kwargs)
        return [], 0

    async def _fake_log_search(_session, **kwargs):
        return None

    monkeypatch.setattr(alumni_service, "list_alumni", _fake_list_alumni)
    monkeypatch.setattr(alumni_service, "log_search", _fake_log_search)
    return seen


def _compiled(stmt) -> tuple[str, dict]:
    compiled = stmt.compile(dialect=postgresql.dialect())
    return str(compiled), dict(compiled.params)


def _list_sql(client, captured, query: str) -> tuple[str, dict]:
    """Compile the statement ``GET /alumni?<query>`` would run."""
    response = client.get(f"/alumni?{query}")
    assert response.status_code == 200, response.text
    kwargs = dict(captured)
    for paging in ("limit", "offset", "sort"):
        kwargs.pop(paging, None)
    return _compiled(build_alumni_query(**kwargs))


def _export_sql(session, **filters) -> tuple[str, dict]:
    """Compile the statement ``POST /alumni/export`` would run for *filters*."""
    stmt = asyncio.run(build_export_query(session, AlumniExportFilters(**filters)))
    return _compiled(stmt)


def _assert_same_population(client, captured, session, query: str, **filters):
    assert _list_sql(client, captured, query) == _export_sql(session, **filters)


# --- per-filter parity --------------------------------------------------------


def test_designations_parity(client, captured, session):
    _assert_same_population(
        client, captured, session, "designations=CFA&designations=CPA",
        designations=["CFA", "CPA"],
    )


def test_designations_parity_is_case_and_comma_insensitive(client, captured, session):
    # Both sides run the SAME parser (dropdowns.parse_designation_tokens), so a
    # lower-case / comma-joined body resolves to the same population as the
    # repeated upper-case query param.
    _assert_same_population(
        client, captured, session, "designations=CFA&designations=CPA",
        designations=["cfa,cpa"],
    )


def test_free_text_q_parity(client, captured, session):
    # #620 widened q to the employment record and made it typo/spacing tolerant.
    # The list and the export must still describe the SAME population — the
    # ranking that reorders the list lives in the ORDER BY only.
    _assert_same_population(client, captured, session, "q=Goldman+Sachs", q="Goldman Sachs")


def test_routed_free_text_q_parity(client, captured, session):
    # A routed sentence ("at <employer> in <place>") is several AND-ed
    # predicates; every one of them has to reach the export too.
    _assert_same_population(
        client,
        captured,
        session,
        "q=at+goldman+schs+in+new+york",
        q="at goldman schs in new york",
    )


def test_graduate_degree_parity(client, captured, session):
    _assert_same_population(
        client, captured, session, "graduate_degree=true", graduate_degree=True
    )


def test_spoke_window_parity(client, captured, session):
    _assert_same_population(
        client, captured, session,
        "spoke_after=2026-08-01&spoke_before=2026-08-31",
        spoke_after="2026-08-01", spoke_before="2026-08-31",
    )


def test_spoke_after_only_parity(client, captured, session):
    _assert_same_population(
        client, captured, session, "spoke_after=2026-01-01", spoke_after="2026-01-01"
    )


def test_missing_phone_parity(client, captured, session):
    _assert_same_population(
        client, captured, session, "missing_phone=true", missing_phone=True
    )


def test_missing_linkedin_parity(client, captured, session):
    _assert_same_population(
        client, captured, session, "missing_linkedin=true", missing_linkedin=True
    )


def test_missing_photo_parity(client, captured, session, bucket):
    # The one that cannot be answered from a column. Both sides must reach the
    # SAME resolved key set; the compiled statements are compared, so a side
    # that dropped the flag (or re-derived it differently) fails here.
    _assert_same_population(
        client, captured, session, "missing_photo=true", missing_photo=True
    )


def test_missing_photo_export_does_not_widen_to_everyone(client, captured, session, bucket):
    # The trap this file exists for, in its newest shape: a flag the export
    # model accepts but never turns into a predicate produces a CSV of the whole
    # roster that looks like a successful "no photo" report.
    filtered, _ = _export_sql(session, missing_photo=True)
    unfiltered, _ = _export_sql(session)
    assert filtered != unfiltered
    assert "net_id" in filtered


def test_missing_photo_export_fails_closed_when_storage_is_down(client, bucket):
    # An unreachable bucket must not degrade into "no predicate". The list and
    # the export both surface it (502) rather than handing over every alumnus.
    bucket.error = ServiceError("storage down")
    headshot_index.reset_cache()
    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name"], "filters": {"missing_photo": True}},
    )
    assert response.status_code == 502
    list_response = client.get("/alumni?missing_photo=true")
    assert list_response.status_code == 502


def test_near_parity(client, captured, session):
    # The headline case: a "near Provo, UT" list used to export nationwide.
    _assert_same_population(
        client, captured, session, "near=Provo%2C+UT", near="Provo, UT"
    )


def test_near_with_radius_override_parity(client, captured, session):
    # The radius override is folded into the phrase by the SHARED resolver, so
    # both sides ask the crosswalk the same question.
    _assert_same_population(
        client, captured, session, "near=Provo%2C+UT&radius=150",
        near="Provo, UT", radius=150,
    )


def test_all_new_filters_together_parity(client, captured, session):
    _assert_same_population(
        client, captured, session,
        (
            "near=Provo%2C+UT&radius=100&designations=CFA&graduate_degree=true"
            "&spoke_after=2026-01-01&spoke_before=2026-12-31&missing_phone=true"
            "&industry=Consulting&kind=all&include_archived=true"
        ),
        near="Provo, UT",
        radius=100,
        designations=["CFA"],
        graduate_degree=True,
        spoke_after="2026-01-01",
        spoke_before="2026-12-31",
        missing_phone=True,
        industry=["Consulting"],
        is_alumni=None,
        include_archived=True,
    )


# --- the trap: a set field must never mean "no predicate" ---------------------


def test_near_applies_a_predicate_the_unfiltered_export_does_not(client, captured, session):
    # Sanity check that the parity assertions above aren't comparing two
    # identical no-op queries: a located export must be NARROWER than one with
    # no filters at all.
    located, _ = _export_sql(session, near="Provo, UT")
    unfiltered, _ = _export_sql(session)
    assert located != unfiltered
    assert "current_employment" in located


def test_unresolvable_near_fails_closed_instead_of_exporting_everyone(client, session):
    # A phrase the geocoder can't pinpoint yields NO location predicate. The list
    # can afford that (the operator sees the widened result plus a "couldn't
    # pinpoint" note); the export refuses, because a silent fallback here is a
    # nationwide CSV for a view the operator scoped to one metro.
    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name"], "filters": {"near": "zzz not a place zzz"}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unresolvable_near_still_lists_with_a_resolved_false_envelope(client, captured):
    # The list's documented behaviour is unchanged by the export's fail-closed
    # rule — this is the deliberate (safe-direction) divergence.
    response = client.get("/alumni?near=zzz+not+a+place+zzz")
    assert response.status_code == 200
    assert response.json()["location"]["resolved"] is False
    assert captured["location_filter"] is None


def test_unknown_designation_is_422_not_a_dropped_predicate(client):
    # build_alumni_query only emits the designation EXISTS for tokens it knows,
    # so an unvalidated typo would drop the predicate and widen the export to
    # everyone. Both paths reject it instead.
    response = client.post(
        "/alumni/export",
        json={"columns": ["first_name"], "filters": {"designations": ["CFX"]}},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_radius_without_near_matches_the_list(client, captured, session):
    # ``radius`` alone is a no-op on both sides (it only qualifies a phrase), so
    # it must not silently become "no predicate" for a caller who meant one — it
    # never meant one.
    _assert_same_population(client, captured, session, "radius=100", radius=100)


# --- structural guard for the whole bug class --------------------------------


def test_every_list_filter_param_exists_on_the_export_body():
    """The regression net for #366: any filter added to ``GET /alumni`` must get
    a matching ``AlumniExportFilters`` field, or exports of that view silently
    widen again."""
    # Not filters: auth/session deps, paging, and ``kind`` (the list's tri-state
    # friends/alumni param, carried on the body as ``is_alumni``).
    not_filters = {"user", "session", "limit", "offset", "kind"}
    params = {
        name for name in inspect.signature(list_alumni).parameters if name not in not_filters
    }
    fields = set(AlumniExportFilters.model_fields) | {"is_alumni"}
    assert params - fields == set()

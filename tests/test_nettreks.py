"""NetTreks tab (fa-web-app #887): area membership, the Other complement, the
report lists, the CSV, and the route gates.

Two layers:

  * membership is a pure function of a city's coordinates
    (``nettreks.reports_for_point``), tested directly — an inside/outside pair
    for every radius, the Provo–SLC corridor, and Ogden (Wasatch Area Alumni but
    NOT a Wasatch host);
  * the population SQL (archived / friends / host flag / Other complement /
    counts-match-lists) runs for real against an in-memory SQLite world built
    from the ORM tables, with the few PostgreSQL math functions the city_geo
    prefilter uses registered on the connection.

No DATABASE_URL needed.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io
import math
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies.auth import get_current_db_user, get_permission_config
from app.core import rate_limit
from app.core.capabilities import DEFAULT_GRANTS
from app.core.database import Base, get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.geo import CityGeo
from app.schemas.auth import UserContext
from app.schemas.nettreks import NetTrekAreaKey as A
from app.schemas.nettreks import NetTrekReportKind as K
from app.services import nettreks as nt

_MI_PER_DEG_LAT = nt._EARTH_MI * math.pi / 180  # ~69.09


def _north(center, miles):
    """A point ``miles`` due north of ``center`` (great-circle exact)."""
    return (center[0] + miles / _MI_PER_DEG_LAT, center[1])


# --- membership (pure) --------------------------------------------------------

_CITY_AREAS = [a for a in nt.NETTREK_AREAS if a.host_corridor is None]


def test_the_seven_areas_are_amys():
    assert [a.name for a in nt.NETTREK_AREAS] == [
        "Dallas",
        "San Francisco",
        "Los Angeles",
        "Phoenix",
        "Seattle",
        "Washington D.C.",
        "Wasatch",
    ]
    for area in _CITY_AREAS:
        assert area.host_radius_miles == 40.0
        assert area.alumni_radius_miles == 60.0


@pytest.mark.parametrize("area", _CITY_AREAS, ids=lambda a: a.key.value)
def test_host_radius_inside_and_outside(area):
    inside = nt.reports_for_point(*_north(area.center, 39.5))
    outside = nt.reports_for_point(*_north(area.center, 40.5))
    assert (area.key, K.hosts) in inside
    assert (area.key, K.hosts) not in outside
    # 40.5 mi is still well inside the 60-mile Area Alumni ring.
    assert (area.key, K.area_alumni) in outside


@pytest.mark.parametrize("area", nt.NETTREK_AREAS, ids=lambda a: a.key.value)
def test_alumni_radius_inside_and_outside(area):
    assert (area.key, K.area_alumni) in nt.reports_for_point(
        *_north(area.center, 59.5)
    )
    assert (area.key, K.area_alumni) not in nt.reports_for_point(
        *_north(area.center, 60.5)
    )


def test_wasatch_area_alumni_center_is_the_provo_slc_midpoint():
    wasatch = next(a for a in nt.NETTREK_AREAS if a.key is A.wasatch)
    assert wasatch.center == pytest.approx((40.4973, -111.77475))
    assert wasatch.host_corridor == ((40.2338, -111.6585), (40.7608, -111.8910))


@pytest.mark.parametrize(
    "city,lat,lng",
    [
        ("Provo", 40.2338, -111.6585),
        ("Orem", 40.2969, -111.6946),
        ("Lehi", 40.3916, -111.8508),
        ("Draper", 40.5247, -111.8638),
        ("Sandy", 40.5649, -111.8389),
        ("Salt Lake City", 40.7608, -111.8910),
        ("Springville", 40.1652, -111.6108),  # just south of Provo
    ],
)
def test_the_provo_slc_corridor_is_wasatch_hosts(city, lat, lng):
    got = nt.reports_for_point(lat, lng)
    assert (A.wasatch, K.hosts) in got
    assert (A.wasatch, K.area_alumni) in got


def test_ogden_is_wasatch_area_alumni_but_not_a_wasatch_host():
    got = nt.reports_for_point(41.2230, -111.9738)
    assert (A.wasatch, K.area_alumni) in got
    assert (A.wasatch, K.hosts) not in got


def test_corridor_tolerance_is_measured_from_the_line_not_the_ends():
    mid = nt.WASATCH_MIDPOINT
    assert nt.point_to_segment_miles(mid, nt.PROVO, nt.SALT_LAKE_CITY) < 0.01
    # Due east of the midpoint: 9 mi east is ~8.5 mi off the (slanted) line ->
    # a Wasatch host; 12 mi east is ~11.4 mi off -> not.
    per_deg_lng = _MI_PER_DEG_LAT * math.cos(math.radians(mid[0]))
    near = (mid[0], mid[1] + 9 / per_deg_lng)
    far = (mid[0], mid[1] + 12 / per_deg_lng)
    assert (A.wasatch, K.hosts) in nt.reports_for_point(*near)
    assert (A.wasatch, K.hosts) not in nt.reports_for_point(*far)
    assert (A.wasatch, K.area_alumni) in nt.reports_for_point(*far)
    # Past SLC, distance is to the SLC endpoint.
    past = _north(nt.SALT_LAKE_CITY, 12)
    assert nt.point_to_segment_miles(
        past, nt.PROVO, nt.SALT_LAKE_CITY
    ) == pytest.approx(12, abs=0.3)


def test_a_far_away_city_is_in_no_report():
    assert nt.reports_for_point(43.6150, -116.2023) == set()  # Boise


# --- the SQLite world ---------------------------------------------------------

# city_geo rows (city_norm, state, lat, lng).
_CITIES = [
    ("dallas", "TX", 32.7767, -96.7970),
    ("fort worth", "TX", 32.7555, -97.3308),  # ~31 mi: Dallas host + alumni
    ("sherman", "TX", 33.6357, -96.6089),  # ~60.5 mi: outside both
    ("denison", "TX", 33.7557, -96.5367),  # further out still
    ("provo", "UT", 40.2338, -111.6585),
    ("lehi", "UT", 40.3916, -111.8508),
    ("salt lake city", "UT", 40.7608, -111.8910),
    ("ogden", "UT", 41.2230, -111.9738),
    ("boise", "ID", 43.6150, -116.2023),  # in no area
]


def _register_pg_math(dbapi_conn, _record):
    dbapi_conn.create_function("radians", 1, math.radians)
    dbapi_conn.create_function("sin", 1, math.sin)
    dbapi_conn.create_function("cos", 1, math.cos)
    dbapi_conn.create_function("acos", 1, math.acos)
    dbapi_conn.create_function("least", 2, min)
    dbapi_conn.create_function("greatest", 2, max)


class _AsyncSession:
    """The async-session surface the service uses, over a sync ORM session.
    Writes (the audit rows) are captured rather than persisted."""

    def __init__(self, session: Session):
        self._s = session
        self.added: list = []

    async def execute(self, stmt):
        return self._s.execute(stmt)

    async def scalar(self, stmt):
        return self._s.scalar(stmt)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def rollback(self):
        pass


class _World:
    def __init__(self, session: Session):
        self.s = session
        self.db = _AsyncSession(session)
        for cn, st, lat, lng in _CITIES:
            session.add(CityGeo(city_norm=cn, state=st, lat=lat, lng=lng))
        session.flush()

    def alum(
        self,
        alumni_id,
        *,
        city=None,
        state=None,
        host=False,
        archived=False,
        is_alumni=True,
        deceased=False,
        last=None,
        gender="F",
        email=None,
        employer=None,
    ):
        self.s.add(
            Alumni(
                alumni_id=alumni_id,
                first_name=f"F{alumni_id}",
                middle_name="M",
                last_name=last or f"L{alumni_id:03d}",
                gender=gender,
                graduation_year=2010,
                linkedin_url=f"https://linkedin.example/in/{alumni_id}",
                archived=archived,
                is_alumni=is_alumni,
                deceased=deceased,
            )
        )
        if city is not None or state is not None or employer is not None:
            self.s.add(
                CurrentEmployment(
                    current_employment_id=alumni_id,
                    alumni_id=alumni_id,
                    current_employer=employer or f"Co{alumni_id}",
                    current_industry="Banking",
                    current_industry_secondary="Tech",
                    current_city=city,
                    current_state=state,
                )
            )
        self.s.add(
            AlumniContactInfo(
                contact_info_id=alumni_id,
                alumni_id=alumni_id,
                personal_email=email or f"p{alumni_id}@dev.example",
                work_email=f"w{alumni_id}@dev.example",
                phone="555-0100",
            )
        )
        if host:
            self.s.add(
                AlumniProgramEngagement(
                    engagement_profile_id=alumni_id,
                    alumni_id=alumni_id,
                    nettrek_host_willing=True,
                )
            )
        self.s.flush()


@pytest.fixture
def world():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", _register_pg_math)
    with engine.begin() as conn:
        Base.metadata.create_all(
            conn,
            tables=[
                Alumni.__table__,
                CurrentEmployment.__table__,
                AlumniContactInfo.__table__,
                AlumniProgramEngagement.__table__,
                CityGeo.__table__,
            ],
        )
    with Session(engine) as session:
        yield _World(session)
    engine.dispose()


def _populate(w: _World):
    # Dallas: two hosts (one in the city, one in Fort Worth), one non-host.
    w.alum(1, city="Dallas", state="Texas", host=True)
    w.alum(2, city=" fort worth ", state="TX", host=True)  # messy spelling
    w.alum(3, city="Dallas", state="Texas")
    # Sherman: just outside Dallas's 60 mi -> an Other host.
    w.alum(4, city="Sherman", state="Texas", host=True)
    # Wasatch: corridor host, corridor non-host, Ogden host.
    w.alum(5, city="Lehi", state="Utah", host=True)
    w.alum(6, city="Provo", state="Utah")
    w.alum(7, city="Ogden", state="Utah", host=True)
    # Hosts with no / unknown company city -> Other.
    w.alum(8, host=True)  # no employment row at all
    w.alum(9, employer="Acme", host=True)  # employment row, blank city/state
    w.alum(10, city="Nowhereville", state="Utah", host=True)  # not in city_geo
    w.alum(11, city="Boise", state="Idaho", host=True)
    # Excluded from everything: archived, friends and the deceased.
    w.alum(12, city="Dallas", state="Texas", host=True, archived=True)
    w.alum(13, city="Dallas", state="Texas", host=True, is_alumni=False)
    w.alum(14, city="Boise", state="Idaho", host=True, archived=True)
    w.alum(16, city="Dallas", state="Texas", host=True, deceased=True)
    w.alum(17, host=True, deceased=True)  # would otherwise land in Other
    # A non-host with no city is on no list at all.
    w.alum(15)


def _ids(w, key, kind):
    page = asyncio.run(
        nt.list_report(w.db, key, kind, limit=200, offset=0, can_edit=True)
    )
    assert page.total == len(page.items)
    return {r.alumni_id for r in page.items}


def test_city_area_hosts_and_area_alumni(world):
    _populate(world)
    assert _ids(world, A.dallas, K.hosts) == {1, 2}
    assert _ids(world, A.dallas, K.area_alumni) == {1, 2, 3}


def test_a_host_appears_on_both_lists(world):
    _populate(world)
    for key in (A.dallas, A.wasatch):
        hosts = _ids(world, key, K.hosts)
        assert hosts
        assert hosts <= _ids(world, key, K.area_alumni)


def test_wasatch_corridor_hosts_and_ogden(world):
    _populate(world)
    assert _ids(world, A.wasatch, K.hosts) == {5}
    # Ogden's host is Wasatch Area Alumni, not a Wasatch host.
    assert _ids(world, A.wasatch, K.area_alumni) == {5, 6, 7}


def test_other_hosts_include_no_city_and_exclude_area_hosts(world):
    _populate(world)
    other = _ids(world, A.other, K.hosts)
    # Ogden (7) is in no HOST catchment, so they're an Other host even though
    # they're on the Wasatch Area Alumni list.
    assert other == {4, 7, 8, 9, 10, 11}
    for key in (A.dallas, A.wasatch):
        assert not other & _ids(world, key, K.hosts)


def test_archived_and_friends_are_excluded_everywhere(world):
    _populate(world)
    every = set()
    for area in nt.NETTREK_AREAS:
        every |= _ids(world, area.key, K.hosts) | _ids(world, area.key, K.area_alumni)
    every |= _ids(world, A.other, K.hosts)
    assert not every & {12, 13, 14}


def test_summary_counts_match_the_lists(world):
    _populate(world)
    summary = asyncio.run(nt.get_summary(world.db))
    keys = [a.key for a in summary.areas]
    assert keys == [a.key for a in nt.NETTREK_AREAS] + [A.other]
    for area in summary.areas:
        assert area.hosts.count == len(_ids(world, area.key, K.hosts))
        assert area.hosts.title == f"{area.name} NetTrek Hosts"
        if area.key is A.other:
            assert area.area_alumni is None
        else:
            assert area.area_alumni.count == len(
                _ids(world, area.key, K.area_alumni)
            )
            assert area.area_alumni.title == f"{area.name} Area Alumni"
    by_key = {a.key: a for a in summary.areas}
    assert by_key[A.dallas].hosts.count == 2
    assert by_key[A.dallas].area_alumni.count == 3
    assert by_key[A.other].hosts.count == 6


def test_pagination(world):
    _populate(world)
    page = asyncio.run(
        nt.list_report(world.db, A.other, K.hosts, limit=2, offset=2, can_edit=True)
    )
    assert page.total == 6
    assert [r.alumni_id for r in page.items] == [8, 9]  # sorted by last name


def test_gender_is_blank_for_a_non_editor(world):
    _populate(world)
    page = asyncio.run(
        nt.list_report(world.db, A.dallas, K.hosts, limit=50, offset=0, can_edit=False)
    )
    assert {r.gender for r in page.items} == {None}


def _csv_rows(text):
    return list(csv.reader(io.StringIO(text)))


def test_csv_header_order_and_cells(world):
    _populate(world)
    world.alum(20, city="Dallas", state="Texas", employer="=HYPERLINK(1)")
    text = asyncio.run(
        nt.export_report_csv(
            world.db, A.dallas, K.area_alumni, actor_user_id=42, can_edit=True
        )
    )
    rows = _csv_rows(text)
    assert rows[0] == [
        "First Name",
        "Middle Name",
        "Last Name",
        "Company",
        "Industry",
        "Secondary Industry",
        "Company City",
        "Company State",
        "Permanent Email",
        "Work Email",
        "Phone",
        "LinkedIn URL",
        "Gender",
        "Graduation Year",
        "Willing to Host a NetTrek",
    ]
    assert len(rows) == 1 + 4
    first = rows[1]  # L001, a host
    assert first[:3] == ["F1", "M", "L001"]
    assert first[6:9] == ["Dallas", "Texas", "p1@dev.example"]
    assert first[13:] == ["2010", "Yes"]
    non_host = next(r for r in rows[1:] if r[2] == "L003")
    assert non_host[14] == ""
    # The formula-injection guard is applied.
    evil = next(r for r in rows[1:] if r[2] == "L020")
    assert evil[3] == "\t=HYPERLINK(1)"


def test_csv_is_audited_without_the_rows(world):
    _populate(world)
    asyncio.run(
        nt.export_report_csv(world.db, A.dallas, K.hosts, actor_user_id=42, can_edit=True)
    )
    (entry,) = world.db.added
    assert entry.action_type == "export_nettrek_report"
    assert entry.user_id == 42
    assert entry.new_value == "report=dallas:hosts; rows=2"
    assert entry.action_type in rate_limit.EXPORT_AUDIT_ACTIONS


def test_export_filenames():
    day = datetime.date(2026, 10, 28)
    assert nt.export_filename(A.dallas, K.hosts, day) == "dallas_nettrek_hosts_2026-10-28.csv"
    assert (
        nt.export_filename(A.dallas, K.area_alumni, day)
        == "dallas_area_alumni_2026-10-28.csv"
    )
    assert (
        nt.export_filename(A.washington_dc, K.hosts, day)
        == "washington_dc_nettrek_hosts_2026-10-28.csv"
    )


# --- routes -------------------------------------------------------------------


def _ctx(*roles):
    return UserContext(
        user_id=7,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


def _get(path, session, ctx=None):
    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_permission_config] = lambda: dict(DEFAULT_GRANTS)
    if ctx is not None:
        app.dependency_overrides[get_current_db_user] = lambda: ctx
    try:
        with TestClient(app) as client:
            return client.get(path)
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_db_user, None)


_PATHS = ["/nettreks", "/nettreks/dallas/hosts", "/nettreks/dallas/hosts/export"]


@pytest.mark.parametrize("path", _PATHS)
def test_routes_require_auth(world, path):
    assert _get(path, world.db).status_code == 401


@pytest.mark.parametrize("path", _PATHS)
def test_routes_are_403_without_alumni_export(world, path):
    resp = _get(path, world.db, _ctx("view_only"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_summary_route(world):
    _populate(world)
    resp = _get("/nettreks", world.db, _ctx("full_access"))
    assert resp.status_code == 200
    areas = resp.json()["areas"]
    assert [a["key"] for a in areas][-1] == "other"
    assert areas[0]["hosts"] == {"kind": "hosts", "title": "Dallas NetTrek Hosts", "count": 2}


def test_report_route_is_audited(world):
    _populate(world)
    resp = _get("/nettreks/wasatch/area_alumni?limit=2", world.db, _ctx("full_access"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Wasatch Area Alumni"
    assert body["total"] == 3
    assert len(body["items"]) == 2
    (entry,) = world.db.added
    assert entry.action_type == "view"
    assert entry.entity_type == "nettrek_report"
    assert entry.user_id == 7
    assert "report=wasatch:area_alumni" in entry.new_value


def test_export_route_downloads_and_audits(world):
    _populate(world)
    resp = _get("/nettreks/dallas/area_alumni/export", world.db, _ctx("full_access"))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith(
        'attachment; filename="dallas_area_alumni_'
    )
    assert len(_csv_rows(resp.text)) == 1 + 3
    (entry,) = world.db.added
    assert entry.action_type == "export_nettrek_report"


@pytest.mark.parametrize(
    "path", ["/nettreks/other/area_alumni", "/nettreks/other/area_alumni/export"]
)
def test_other_has_no_area_alumni_report(world, path):
    assert _get(path, world.db, _ctx("full_access")).status_code == 404


@pytest.mark.parametrize(
    "path", ["/nettreks/boise/hosts", "/nettreks/dallas/everyone/export"]
)
def test_unknown_area_or_kind_is_422(world, path):
    assert _get(path, world.db, _ctx("full_access")).status_code == 422

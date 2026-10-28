"""NetTreks tab: per-area Hosts and Area Alumni reports (fa-web-app #887).

Amy's seven areas plus "Other". For each area there are two reports:

  * **<Area> NetTrek Hosts** — alumni willing to host a NetTrek
    (``alumni_program_engagement.nettrek_host_willing``) whose company is in the
    area's HOST catchment.
  * **<Area> Area Alumni** — everyone whose company is in the area's (wider)
    ALUMNI catchment. A host is therefore on both lists.

"Other" has only a Hosts report: every host who is in NONE of the seven host
catchments — including a host with no company city at all, or one whose city
does not resolve in ``city_geo``.

Location is the COMPANY (work) city: ``current_employment.current_city`` +
``.current_state``, resolved through the ``city_geo`` crosswalk exactly as the
location search does (``app.services.geo_search``). There is no residence data
in the system (#287).

How membership is decided
-------------------------
Membership is a property of a CITY, not of an alum, so it is decided once per
``city_geo`` row:

  1. :func:`_candidate_cities` pulls the ``city_geo`` rows inside ANY area's
     alumni radius (a SQL prefilter using ``geo_search._distance_mi_expr``).
  2. :func:`reports_for_point` classifies each one in Python — pure, and the
     thing the membership tests exercise directly.
  3. Alumni are matched to those ``(city_norm, state)`` keys with
     ``geo_search.alumni_location_filter`` — the same predicate the "near"
     search uses — and the landing page counts are computed from one grouped
     query over the SAME key mapping, so a link's count and the list behind it
     cannot disagree.

Population: non-archived alumni only (``archived = false``) and alumni only, not
friends of the program (``is_alumni = true``) — the main alumni list's defaults
(``repositories.alumni.build_alumni_query``).
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import math
from dataclasses import dataclass

from sqlalchemy import String, and_, cast, false, func, not_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.geo import CityGeo
from app.schemas.nettreks import (
    NetTrekAreaKey,
    NetTrekAreaSummary,
    NetTrekReportCount,
    NetTrekReportKind,
    NetTrekReportPage,
    NetTrekRow,
    NetTrekSummary,
)
from app.services.alumni_export import _fmt as _csv_cell
from app.services.geo_regions import CityKey
from app.services.geo_search import (
    _EARTH_MI,
    _alumni_state_code_expr,
    _distance_mi_expr,
    alumni_location_filter,
    haversine_miles,
)

log = logging.getLogger(__name__)

LatLng = tuple[float, float]

# --- the areas ---------------------------------------------------------------

#: Host catchment (miles) around a city area's center.
HOST_RADIUS_MILES = 40.0
#: Area Alumni catchment (miles) around every area's center (Wasatch included).
ALUMNI_RADIUS_MILES = 60.0

PROVO: LatLng = (40.2338, -111.6585)
SALT_LAKE_CITY: LatLng = (40.7608, -111.8910)
#: Center of the Wasatch Area Alumni circle: the Provo–SLC midpoint (~Lehi/Draper).
WASATCH_MIDPOINT: LatLng = (
    (PROVO[0] + SALT_LAKE_CITY[0]) / 2,
    (PROVO[1] + SALT_LAKE_CITY[1]) / 2,
)
#: Wasatch HOSTS are the "Provo through SLC" corridor: a company city within this
#: many miles of the straight line from Provo to Salt Lake City. 10 miles takes
#: in Provo, Orem, American Fork, Lehi, Draper, Sandy, West Jordan, Murray, SLC
#: and the cities around them, and leaves out Ogden (~32 mi north of SLC) and
#: Park City (~18 mi east of the line) — those are Wasatch AREA ALUMNI (60 mi of
#: the midpoint) but not Wasatch hosts.
WASATCH_CORRIDOR_TOLERANCE_MILES = 10.0


@dataclass(frozen=True)
class NetTrekArea:
    """One NetTrek area.

    A city area's hosts are within ``host_radius_miles`` of ``center``; Wasatch
    instead sets ``host_corridor`` (hosts within
    :data:`WASATCH_CORRIDOR_TOLERANCE_MILES` of that segment) and leaves
    ``host_radius_miles`` ``None``. Area Alumni are always within
    ``alumni_radius_miles`` of ``center``."""

    key: NetTrekAreaKey
    name: str
    center: LatLng
    host_radius_miles: float | None
    alumni_radius_miles: float
    host_corridor: tuple[LatLng, LatLng] | None = None


#: THE list of NetTrek areas (Amy's seven), in landing-page order. "Other" is
#: not an area — it is the complement of every host catchment below.
NETTREK_AREAS: tuple[NetTrekArea, ...] = (
    NetTrekArea(
        NetTrekAreaKey.dallas, "Dallas", (32.7767, -96.7970),
        HOST_RADIUS_MILES, ALUMNI_RADIUS_MILES,
    ),
    NetTrekArea(
        NetTrekAreaKey.san_francisco, "San Francisco", (37.7749, -122.4194),
        HOST_RADIUS_MILES, ALUMNI_RADIUS_MILES,
    ),
    NetTrekArea(
        NetTrekAreaKey.los_angeles, "Los Angeles", (34.0522, -118.2437),
        HOST_RADIUS_MILES, ALUMNI_RADIUS_MILES,
    ),
    NetTrekArea(
        NetTrekAreaKey.phoenix, "Phoenix", (33.4484, -112.0740),
        HOST_RADIUS_MILES, ALUMNI_RADIUS_MILES,
    ),
    NetTrekArea(
        NetTrekAreaKey.seattle, "Seattle", (47.6062, -122.3321),
        HOST_RADIUS_MILES, ALUMNI_RADIUS_MILES,
    ),
    NetTrekArea(
        NetTrekAreaKey.washington_dc, "Washington D.C.", (38.9072, -77.0369),
        HOST_RADIUS_MILES, ALUMNI_RADIUS_MILES,
    ),
    NetTrekArea(
        NetTrekAreaKey.wasatch, "Wasatch", WASATCH_MIDPOINT,
        None, ALUMNI_RADIUS_MILES,
        host_corridor=(PROVO, SALT_LAKE_CITY),
    ),
)
OTHER_NAME = "Other"

_AREAS_BY_KEY: dict[NetTrekAreaKey, NetTrekArea] = {a.key: a for a in NETTREK_AREAS}

#: A report is ``(area key, kind)``.
ReportId = tuple[NetTrekAreaKey, NetTrekReportKind]

_HOSTS = NetTrekReportKind.hosts
_AREA_ALUMNI = NetTrekReportKind.area_alumni


def area_name(key: NetTrekAreaKey) -> str:
    return OTHER_NAME if key is NetTrekAreaKey.other else _AREAS_BY_KEY[key].name


def report_title(key: NetTrekAreaKey, kind: NetTrekReportKind) -> str:
    suffix = "NetTrek Hosts" if kind is _HOSTS else "Area Alumni"
    return f"{area_name(key)} {suffix}"


def report_exists(key: NetTrekAreaKey, kind: NetTrekReportKind) -> bool:
    """Every area has both reports except Other, which has only Hosts."""
    return not (key is NetTrekAreaKey.other and kind is _AREA_ALUMNI)


# --- geometry (pure) ---------------------------------------------------------


def point_to_segment_miles(point: LatLng, a: LatLng, b: LatLng) -> float:
    """Distance in miles from ``point`` to the straight segment ``a``–``b``.

    Projects all three onto a local flat plane (equirectangular, centered on the
    segment's midpoint latitude) and measures to the nearest point ON the
    segment — past either end it is the distance to that endpoint. Over a
    ~40-mile corridor the projection's error is a small fraction of a mile,
    far inside the corridor tolerance."""
    lat0 = math.radians((a[0] + b[0]) / 2)

    def _xy(p: LatLng) -> tuple[float, float]:
        return (
            _EARTH_MI * math.radians(p[1]) * math.cos(lat0),
            _EARTH_MI * math.radians(p[0]),
        )

    px, py = _xy(point)
    ax, ay = _xy(a)
    bx, by = _xy(b)
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    t = 0.0 if length_sq == 0 else ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _is_host_area(area: NetTrekArea, lat: float, lng: float) -> bool:
    if area.host_corridor is not None:
        a, b = area.host_corridor
        return (
            point_to_segment_miles((lat, lng), a, b)
            <= WASATCH_CORRIDOR_TOLERANCE_MILES
        )
    radius = area.host_radius_miles or 0.0
    return haversine_miles(lat, lng, *area.center) <= radius


def reports_for_point(lat: float, lng: float) -> set[ReportId]:
    """Which area reports a company located at ``(lat, lng)`` belongs to.

    ``(area, hosts)`` means a HOST there belongs on that area's Hosts list (a
    non-host does not — the host flag is applied on top); ``(area,
    area_alumni)`` means everyone there belongs on its Area Alumni list. Never
    returns an ``other`` report: Other is the complement of every Hosts set."""
    out: set[ReportId] = set()
    for area in NETTREK_AREAS:
        if haversine_miles(lat, lng, *area.center) <= area.alumni_radius_miles:
            out.add((area.key, _AREA_ALUMNI))
        if _is_host_area(area, lat, lng):
            out.add((area.key, _HOSTS))
    return out


# --- city resolution (DB) ----------------------------------------------------

# The SQL prefilter is padded a little so the spherical-law-of-cosines distance
# in SQL and the haversine in Python can never disagree at a boundary city: the
# prefilter only has to be a SUPERSET; Python makes the decision.
_PREFILTER_PAD_MILES = 1.0


async def _candidate_cities(session: AsyncSession) -> list[tuple[str, str, float, float]]:
    """``city_geo`` rows ``(city_norm, state, lat, lng)`` inside any area's
    alumni catchment. Every host catchment sits inside its area's alumni
    catchment (the corridor reaches ~30 mi from the midpoint), so this is the
    full set of cities any report can match."""
    near_any = or_(
        *(
            _distance_mi_expr(*area.center)
            <= area.alumni_radius_miles + _PREFILTER_PAD_MILES
            for area in NETTREK_AREAS
        )
    )
    rows = (
        await session.execute(
            select(CityGeo.city_norm, CityGeo.state, CityGeo.lat, CityGeo.lng).where(
                near_any
            )
        )
    ).all()
    return [(cn, st, float(lat), float(lng)) for cn, st, lat, lng in rows]


async def resolve_report_keys(session: AsyncSession) -> dict[ReportId, list[CityKey]]:
    """Every report's ``(city_norm, state)`` key set, from ONE ``city_geo``
    read. Reports whose area has no ``city_geo`` city come back empty (which
    ``alumni_location_filter`` turns into match-nothing)."""
    keys: dict[ReportId, list[CityKey]] = {
        (area.key, kind): [] for area in NETTREK_AREAS for kind in NetTrekReportKind
    }
    for cn, st, lat, lng in await _candidate_cities(session):
        for report in reports_for_point(lat, lng):
            keys[report].append((cn, st))
    return keys


def _all_host_keys(keys: dict[ReportId, list[CityKey]]) -> list[CityKey]:
    return sorted({k for (_, kind), ks in keys.items() if kind is _HOSTS for k in ks})


# --- population + predicates -------------------------------------------------

_HOST_WILLING = func.coalesce(AlumniProgramEngagement.nettrek_host_willing, false())


def _population_conditions() -> list:
    """Non-archived alumni, not friends — the main alumni list's defaults
    (``build_alumni_query``: ``include_archived=False``, ``is_alumni=True``)."""
    return [Alumni.archived.is_(False), Alumni.is_alumni.is_(True)]


def _located_in(keys: list[CityKey]):
    """Company city is one of ``keys``. Never NULL: an alum with no employment
    row, or a blank city/state, is plainly NOT located there (so ``not_`` of
    this is safe for the Other report)."""
    return and_(
        CurrentEmployment.current_city.is_not(None),
        CurrentEmployment.current_state.is_not(None),
        alumni_location_filter(keys),
    )


def report_predicate(
    key: NetTrekAreaKey,
    kind: NetTrekReportKind,
    keys: dict[ReportId, list[CityKey]],
):
    """The WHERE clause (on top of the population) for one report."""
    if key is NetTrekAreaKey.other:
        # Every host in NO host catchment — including hosts with no company city
        # or a city ``city_geo`` does not know.
        return and_(_HOST_WILLING.is_(True), not_(_located_in(_all_host_keys(keys))))
    located = _located_in(keys[(key, kind)])
    if kind is _HOSTS:
        return and_(_HOST_WILLING.is_(True), located)
    return located


def _from_clause(stmt):
    """Alumni with their (one-row-per-alum) job, contact and engagement rows,
    all OUTER-joined so nobody without one of them is dropped."""
    return (
        stmt.select_from(Alumni)
        .outerjoin(CurrentEmployment, CurrentEmployment.alumni_id == Alumni.alumni_id)
        .outerjoin(AlumniContactInfo, AlumniContactInfo.alumni_id == Alumni.alumni_id)
        .outerjoin(
            AlumniProgramEngagement,
            AlumniProgramEngagement.alumni_id == Alumni.alumni_id,
        )
    )


# --- landing-page counts -----------------------------------------------------


async def get_summary(session: AsyncSession) -> NetTrekSummary:
    """Every report with its count.

    One grouped query: alumni per (company city key, host flag). Each group is
    then credited to the reports its city key belongs to, using the SAME key
    mapping the report lists filter on. The grouping keys are labelled in a
    subquery and grouped OUTSIDE it, so PostgreSQL never has to match the long
    state-code CASE expression (with its bind parameters) between SELECT and
    GROUP BY."""
    keys = await resolve_report_keys(session)
    by_city: dict[CityKey, set[ReportId]] = {}
    for report, report_keys in keys.items():
        for k in report_keys:
            by_city.setdefault(k, set()).add(report)

    per_alum = _from_clause(
        select(
            func.lower(func.trim(cast(CurrentEmployment.current_city, String))).label(
                "city_norm"
            ),
            _alumni_state_code_expr().label("state_code"),
            _HOST_WILLING.label("host"),
        )
    ).where(*_population_conditions()).subquery()
    rows = (
        await session.execute(
            select(
                per_alum.c.city_norm,
                per_alum.c.state_code,
                per_alum.c.host,
                func.count(),
            ).group_by(per_alum.c.city_norm, per_alum.c.state_code, per_alum.c.host)
        )
    ).all()

    counts: dict[ReportId, int] = {r: 0 for r in keys}
    other_hosts = 0
    for city_norm, state_code, host, n in rows:
        reports = (
            by_city.get((city_norm, state_code), set())
            if city_norm is not None and state_code is not None
            else set()
        )
        in_host_area = False
        for area_key, kind in reports:
            if kind is _AREA_ALUMNI:
                counts[(area_key, kind)] += n
            elif host:
                counts[(area_key, kind)] += n
                in_host_area = True
        if host and not in_host_area:
            other_hosts += n

    areas = [
        NetTrekAreaSummary(
            key=area.key,
            name=area.name,
            hosts=NetTrekReportCount(
                kind=_HOSTS,
                title=report_title(area.key, _HOSTS),
                count=counts[(area.key, _HOSTS)],
            ),
            area_alumni=NetTrekReportCount(
                kind=_AREA_ALUMNI,
                title=report_title(area.key, _AREA_ALUMNI),
                count=counts[(area.key, _AREA_ALUMNI)],
            ),
        )
        for area in NETTREK_AREAS
    ]
    areas.append(
        NetTrekAreaSummary(
            key=NetTrekAreaKey.other,
            name=OTHER_NAME,
            hosts=NetTrekReportCount(
                kind=_HOSTS,
                title=report_title(NetTrekAreaKey.other, _HOSTS),
                count=other_hosts,
            ),
            area_alumni=None,
        )
    )
    return NetTrekSummary(areas=areas)


# --- report rows -------------------------------------------------------------

_ROW_COLUMNS = (
    Alumni.alumni_id,
    Alumni.first_name,
    Alumni.middle_name,
    Alumni.last_name,
    CurrentEmployment.current_employer,
    CurrentEmployment.current_industry,
    CurrentEmployment.current_industry_secondary,
    CurrentEmployment.current_city,
    CurrentEmployment.current_state,
    AlumniContactInfo.personal_email,
    AlumniContactInfo.work_email,
    AlumniContactInfo.phone,
    Alumni.linkedin_url,
    Alumni.gender,
    Alumni.graduation_year,
    _HOST_WILLING,
)


def _report_select(key, kind, keys):
    return (
        _from_clause(select(*_ROW_COLUMNS))
        .where(*_population_conditions(), report_predicate(key, kind, keys))
        .order_by(Alumni.last_name, Alumni.first_name, Alumni.alumni_id)
    )


def _to_row(row, *, can_edit: bool) -> NetTrekRow:
    (
        alumni_id, first, middle, last, employer, industry, industry2, city,
        state, personal, work, phone, linkedin, gender, year, host,
    ) = row
    return NetTrekRow(
        alumni_id=alumni_id,
        first_name=first,
        middle_name=middle,
        last_name=last,
        company=employer,
        industry=industry,
        secondary_industry=industry2,
        company_city=city,
        company_state=state,
        permanent_email=personal,
        work_email=work,
        phone=phone,
        linkedin_url=linkedin,
        # Gender is a view_only-hidden field (VIEW_ONLY_HIDDEN_FIELDS): blank it
        # for a non-editor who has been granted alumni.export, as the survey
        # exports blank NetID. The column stays so the layout never moves.
        gender=gender if can_edit else None,
        graduation_year=year,
        willing_to_host=bool(host),
    )


async def list_report(
    session: AsyncSession,
    key: NetTrekAreaKey,
    kind: NetTrekReportKind,
    *,
    limit: int,
    offset: int,
    can_edit: bool,
) -> NetTrekReportPage:
    keys = await resolve_report_keys(session)
    stmt = _report_select(key, kind, keys)
    total = await session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    rows = (await session.execute(stmt.limit(limit).offset(offset))).all()
    return NetTrekReportPage(
        area=key,
        area_name=area_name(key),
        kind=kind,
        title=report_title(key, kind),
        items=[_to_row(r, can_edit=can_edit) for r in rows],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )


# --- CSV ---------------------------------------------------------------------

#: The issue's columns, in its order.
NETTREK_EXPORT_COLUMNS: tuple[str, ...] = (
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
)

#: The audit action every NetTrek CSV writes (counted in the global export
#: budget via ``rate_limit.EXPORT_AUDIT_ACTIONS``).
NETTREK_EXPORT_ACTION = "export_nettrek_report"


def export_filename(
    key: NetTrekAreaKey, kind: NetTrekReportKind, today: datetime.date | None = None
) -> str:
    """``dallas_nettrek_hosts_<date>.csv`` / ``dallas_area_alumni_<date>.csv``.
    Built only from the validated enum values and a generated date — never
    caller free text (header injection)."""
    middle = "nettrek_hosts" if kind is _HOSTS else "area_alumni"
    day = (today or datetime.date.today()).isoformat()
    return f"{key.value}_{middle}_{day}.csv"


async def export_report_csv(
    session: AsyncSession,
    key: NetTrekAreaKey,
    kind: NetTrekReportKind,
    *,
    actor_user_id: int | None,
    can_edit: bool,
) -> str:
    """The whole report as CSV. Every cell goes through the shared
    formula-injection guard. Audit-logged as :data:`NETTREK_EXPORT_ACTION` with
    the report and row count — never the rows."""
    keys = await resolve_report_keys(session)
    rows = (await session.execute(_report_select(key, kind, keys))).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(list(NETTREK_EXPORT_COLUMNS))
    for raw in rows:
        r = _to_row(raw, can_edit=can_edit)
        writer.writerow(
            [
                _csv_cell(r.first_name, "str"),
                _csv_cell(r.middle_name, "str"),
                _csv_cell(r.last_name, "str"),
                _csv_cell(r.company, "str"),
                _csv_cell(r.industry, "str"),
                _csv_cell(r.secondary_industry, "str"),
                _csv_cell(r.company_city, "str"),
                _csv_cell(r.company_state, "str"),
                _csv_cell(r.permanent_email, "str"),
                _csv_cell(r.work_email, "str"),
                _csv_cell(r.phone, "str"),
                _csv_cell(r.linkedin_url, "str"),
                _csv_cell(r.gender, "str"),
                _csv_cell(r.graduation_year, "int"),
                # "Yes" or blank, per the issue (not the export's Yes/No).
                "Yes" if r.willing_to_host else "",
            ]
        )

    if actor_user_id is None:
        log.warning("%s audit skipped: no actor (rows=%s)", NETTREK_EXPORT_ACTION, len(rows))
    else:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type=NETTREK_EXPORT_ACTION,
                entity_type="nettrek_report",
                entity_id=None,
                new_value=f"report={key.value}:{kind.value}; rows={len(rows)}",
            )
        )
        await session.commit()
    return buffer.getvalue()

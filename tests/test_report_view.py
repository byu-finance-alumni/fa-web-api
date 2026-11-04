"""Sorting the Outreach people tables (fa-web-app #917)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.dialects import postgresql

from app.models.engagement import AlumniProgramEngagement
from app.schemas.nettreks import NetTrekAreaKey as A
from app.schemas.nettreks import NetTrekReportKind as K
from app.schemas.nettreks import ReportSortColumn as C
from app.schemas.nettreks import ReportSortDir as D
from app.schemas.outreach import OutreachGroupKey as G
from app.schemas.outreach import OutreachListKey as L
from app.services import nettreks as nt
from app.services import outreach_lists as ol
from app.services.report_view import ReportView
from tests.test_nettreks import _ctx, _get, world  # noqa: F401


def _ids(w, view=None, *, can_edit=True, limit=50, offset=0, key=A.dallas, kind=K.area_alumni):
    page = asyncio.run(
        nt.list_report(
            w.db, key, kind, limit=limit, offset=offset, can_edit=can_edit, view=view
        )
    )
    return [r.alumni_id for r in page.items]


def _sorted(column, direction=D.asc):
    return ReportView(sort=column, direction=direction)


def _dallas(w, n, **fields):
    w.alum(n, city="Dallas", state="Texas", **fields)


@pytest.mark.parametrize("direction", list(D))
@pytest.mark.parametrize(
    ("column", "fields"),
    [
        (C.first_name, ("first", "ann", "Bob", "CAT")),
        (C.middle_name, ("middle", "ann", "Bob", "CAT")),
        (C.last_name, ("last", "adams", "Baker", "CARTER")),
        (C.company, ("employer", "acme", "Bolt", "CRANE")),
        (C.industry, ("industry", "banking", "Consulting", "TECH")),
        (C.secondary_industry, ("secondary", "banking", "Consulting", "TECH")),
        (C.permanent_email, ("email", "a@x.example", "B@x.example", "c@x.example")),
        (C.phone, ("phone", "2015551234", "+1 (301) 555-1234", "401.555.1234")),
        (C.linkedin_url, ("linkedin", "https://a.example", "https://b.example", "https://c.example")),
        (C.graduation_year, ("year", 1999, 2005, 2020)),
    ],
    ids=lambda v: v.value if isinstance(v, C) else None,
)
def test_each_column_sorts_both_ways_case_insensitively(world, column, fields, direction):
    name, *values = fields
    for n, value in zip((1, 2, 3), values, strict=True):
        _dallas(world, n, **{name: value})
    expected = [1, 2, 3] if direction is D.asc else [3, 2, 1]
    assert _ids(world, _sorted(column, direction)) == expected


@pytest.mark.parametrize("direction", list(D))
def test_blanks_stay_last_both_ways(world, direction):
    _dallas(world, 1, employer=" ", middle="  ", year=None, phone="", linkedin=" ")
    _dallas(world, 2, employer="Acme", middle="A", year=2000, phone="2015551234",
            linkedin="https://a.example")
    _dallas(world, 3, employer="Bolt", middle="B", year=2010, phone="3015551234",
            linkedin="https://b.example")
    for column in (C.company, C.middle_name, C.graduation_year, C.phone, C.linkedin_url):
        order = _ids(world, _sorted(column, direction))
        assert order[-1] == 1, column
        assert order[:2] == ([2, 3] if direction is D.asc else [3, 2]), column


def test_no_employment_row_sorts_last_too(world):
    world.alum(1, host=True)  # no city, no job row: an Other host
    world.alum(2, city="Boise", state="Idaho", host=True, employer="Acme")
    for direction in D:
        ids = _ids(world, _sorted(C.company, direction), key=A.other, kind=K.hosts)
        assert ids == [2, 1]


def test_state_sorts_on_the_code_the_table_shows(world):
    # Stored spelling must not split one displayed value: "Texas" is TX, so it
    # sorts after ID and beside "tx", not after "Utah".
    world.alum(1, city="Boise", state="Texas", host=True)
    world.alum(2, city="Boise", state="ID", host=True)
    world.alum(3, city="Boise", state="tx", host=True)
    world.alum(4, city="Boise", state="Virginia", host=True)
    world.alum(5, city="Boise", state="Utah", host=True)
    world.alum(6, host=True)
    ids = _ids(world, _sorted(C.company_state), key=A.other, kind=K.hosts)
    assert ids == [2, 1, 3, 5, 4, 6]


def test_gender_sorts_on_what_the_table_shows(world):
    _dallas(world, 1, gender="male")
    _dallas(world, 2, gender="F")
    _dallas(world, 3, gender="Prefer not to say")
    _dallas(world, 4, gender="female")
    assert _ids(world, _sorted(C.gender)) == [2, 4, 1, 3]
    assert _ids(world, _sorted(C.gender, D.desc)) == [1, 2, 4, 3]


def test_industry_sorts_on_the_shown_primary_not_the_displayed_rule(world):
    # The Industry column shows the stored primary, so an "Other" row with a
    # secondary sorts under Other, where the eye finds it.
    _dallas(world, 1, industry="Other", secondary="Accounting")
    _dallas(world, 2, industry="Banking", secondary=None)
    _dallas(world, 3, industry="Tech", secondary=None)
    assert _ids(world, _sorted(C.industry)) == [2, 1, 3]


def test_ties_are_stable_across_pages(world):
    for n in range(1, 8):
        _dallas(world, n, employer="Same", last="Same", first="Same")
    view = _sorted(C.company, D.desc)
    pages = [_ids(world, view, limit=3, offset=o) for o in (0, 3, 6)]
    assert pages == [[1, 2, 3], [4, 5, 6], [7]]


def test_gender_sort_is_ignored_for_a_non_editor(world):
    _dallas(world, 1, gender="M", last="A")
    _dallas(world, 2, gender="F", last="B")
    assert _ids(world, _sorted(C.gender), can_edit=True) == [2, 1]
    # Same order as no sort at all, either direction: the order leaks nothing.
    for direction in D:
        assert _ids(world, _sorted(C.gender, direction), can_edit=False) == [1, 2]


def _willing(w, alumni_id, **flags):
    w.s.add(
        AlumniProgramEngagement(
            engagement_profile_id=alumni_id + 1000, alumni_id=alumni_id, **flags
        )
    )
    w.s.flush()


def test_outreach_lists_sort_the_same_way(world):
    world.alum(1, city="Provo", state="Utah", employer="Bolt")
    world.alum(2, city="Lehi", state="UT", employer="Acme")
    _willing(world, 1, guest_speaker_willing=True)
    _willing(world, 2, guest_speaker_willing=True)
    ref = ol.make_ref(G.speakers, L.utah, None)
    page = asyncio.run(
        ol.list_page(
            world.db, ref, limit=50, offset=0, can_edit=True, view=_sorted(C.company)
        )
    )
    assert [r.alumni_id for r in page.items] == [2, 1]


def test_default_order_is_unchanged():
    keys = {(a.key, k): [("dallas", "TX")] for a in nt.NETTREK_AREAS for k in K}
    sql = str(nt._report_select(A.dallas, K.hosts, keys).compile()).lower()
    assert sql.endswith(
        "order by alumni.last_name, alumni.first_name, alumni.alumni_id"
    )


def test_state_sort_writes_the_state_case_only_once_more():
    # The location filter's JIT trap (2026-11-03): one sort adds one CASE, not
    # one per key.
    big = [(f"city {i}", "TX") for i in range(1200)]
    keys = {(a.key, k): big for a in nt.NETTREK_AREAS for k in K}
    sql = str(
        nt._report_select(A.other, K.hosts, keys, _sorted(C.company_state)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    ).lower()
    assert sql.count("case when") == 2


@pytest.mark.parametrize(
    "path",
    [
        "/nettreks/dallas/hosts?sort=willing_to_host",
        "/nettreks/dallas/hosts?sort=alumni_id",
        "/nettreks/dallas/hosts?sort=last_name&dir=up",
        "/outreach/speakers/utah?sort=1;drop",
        "/outreach/speakers/utah?dir=sideways",
    ],
)
def test_sort_is_whitelisted(world, path):
    assert _get(path, world.db, _ctx("full_access")).status_code == 422
    assert world.db.added == []


def test_sort_routes_answer(world):
    _dallas(world, 1, last="A")
    _dallas(world, 2, last="B")
    resp = _get(
        "/nettreks/dallas/area_alumni?sort=last_name&dir=desc",
        world.db,
        _ctx("full_access"),
    )
    assert resp.status_code == 200
    assert [r["alumni_id"] for r in resp.json()["items"]] == [2, 1]

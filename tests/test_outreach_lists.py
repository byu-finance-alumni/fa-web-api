"""Outreach interest lists: Conference Interest, Event Speakers, Event Sponsors,
Alumni Mentors.

Every predicate runs for real against an in-memory SQLite world built from the
ORM tables (the NetTreks test harness), including each fallback (no industry ->
Financial Services / Industry Not Listed; no work state -> Out of State) and
every exclusion the NetTrek reports apply. Plus the permission gate, the
audit rows and the CSV.
"""

from __future__ import annotations

import asyncio
import csv
import datetime
import io

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core import rate_limit
from app.core.database import Base
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.geo import CityGeo
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.schemas.outreach import OutreachGroupKey as G
from app.schemas.outreach import OutreachListKey as L
from app.services import outreach_lists as ol
from tests.test_nettreks import _AsyncSession, _ctx, _get, _register_pg_math

FLAGS = (
    "nettrek_host_willing",
    "finance_conference_willing",
    "guest_speaker_willing",
    "company_event_sponsor_willing",
    "mentor_willing",
    "women_in_finance_mentor_willing",
)


class _World:
    def __init__(self, session: Session):
        self.s = session
        self.db = _AsyncSession(session)
        self._labels: dict[str, int] = {}

    def alum(
        self,
        alumni_id,
        *,
        industry=None,
        secondary=None,
        state=None,
        city=None,
        archived=False,
        is_alumni=True,
        deceased=False,
        label=None,
        **flags,
    ):
        self.s.add(
            Alumni(
                alumni_id=alumni_id,
                first_name=f"F{alumni_id}",
                last_name=f"L{alumni_id:03d}",
                gender="F",
                graduation_year=2012,
                archived=archived,
                is_alumni=is_alumni,
                deceased=deceased,
            )
        )
        if industry or secondary or state or city:
            self.s.add(
                CurrentEmployment(
                    current_employment_id=alumni_id,
                    alumni_id=alumni_id,
                    current_employer=f"Co{alumni_id}",
                    current_industry=industry,
                    current_industry_secondary=secondary,
                    current_city=city,
                    current_state=state,
                )
            )
        self.s.add(
            AlumniContactInfo(
                contact_info_id=alumni_id,
                alumni_id=alumni_id,
                personal_email=f"p{alumni_id}@dev.example",
            )
        )
        if label:
            if label not in self._labels:
                # BigInteger PKs don't autoincrement on SQLite; number by hand.
                self._labels[label] = len(self._labels) + 1
                self.s.add(
                    StatusLabel(
                        status_label_id=self._labels[label], status_label_name=label
                    )
                )
                self.s.flush()
            self.s.add(
                AlumniStatusLabel(
                    alumni_status_label_id=alumni_id,
                    alumni_id=alumni_id,
                    status_label_id=self._labels[label],
                )
            )
        if flags:
            assert set(flags) <= set(FLAGS), flags
            self.s.add(
                AlumniProgramEngagement(
                    engagement_profile_id=alumni_id,
                    alumni_id=alumni_id,
                    **{f: bool(flags.get(f)) for f in FLAGS},
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
                StatusLabel.__table__,
                AlumniStatusLabel.__table__,
            ],
        )
    with Session(engine) as session:
        yield _World(session)
    engine.dispose()


EXCLUDED = {90, 91, 92, 93, 94}


def _excluded(w, **flags):
    """One of everything the NetTrek reports leave off, each otherwise on the
    list ``flags`` puts them on."""
    w.alum(90, archived=True, **flags)
    w.alum(91, is_alumni=False, **flags)
    w.alum(92, deceased=True, **flags)
    w.alum(93, label="Do Not Contact", **flags)
    w.alum(94, label="Deceased", **flags)


def _ids(w, group, key, industry=None):
    ref = ol.make_ref(group, key, industry)
    page = asyncio.run(ol.list_page(w.db, ref, limit=200, offset=0, can_edit=True))
    assert page.total == len(page.items)
    return {r.alumni_id for r in page.items}


def _summary(w, group):
    return asyncio.run(ol.get_summary(w.db, group))


# --- Conference Interest -----------------------------------------------------------


def _conference(w):
    w.alum(1, industry="Real Estate", finance_conference_willing=True)
    w.alum(2, industry=" real estate ", finance_conference_willing=True)  # messy
    w.alum(3, industry="Corporate Finance", finance_conference_willing=True)
    w.alum(4, industry="Investment Banking", finance_conference_willing=True)
    w.alum(5, finance_conference_willing=True)  # no employment row at all
    w.alum(6, state="Utah", finance_conference_willing=True)  # row, no industry
    # Displayed industry (#891): "Other" + a secondary shows the secondary.
    w.alum(7, industry="Other", secondary="Real Estate", finance_conference_willing=True)
    w.alum(8, industry="Other", finance_conference_willing=True)  # stays "Other"
    w.alum(9, industry="Unknown", finance_conference_willing=True)
    # Interested in something else, or nothing: on no conference list.
    w.alum(10, industry="Real Estate", mentor_willing=True)
    w.alum(11, industry="Real Estate")
    _excluded(w, industry="Real Estate", finance_conference_willing=True)


def test_conference_lists_split_by_displayed_industry(world):
    _conference(world)
    assert _ids(world, G.conference, L.real_estate) == {1, 2, 7}
    assert _ids(world, G.conference, L.corporate_finance) == {3}
    # Every other industry, INCLUDING no industry at all.
    assert _ids(world, G.conference, L.financial_services) == {4, 5, 6, 8, 9}


def test_conference_summary_counts_match_the_lists(world):
    _conference(world)
    s = _summary(world, G.conference)
    assert s.title == "Conference Interest"
    assert [(link.key, link.title, link.count) for link in s.links] == [
        (L.real_estate, "Real Estate Conference Alumni Interest", 3),
        (L.corporate_finance, "Corporate Finance Conference Alumni Interest", 1),
        (L.financial_services, "Financial Services Conference Alumni Interest", 5),
    ]


# --- Event Speakers / Event Sponsors ---------------------------------------------------


@pytest.mark.parametrize(
    ("group", "flag", "other_flag"),
    [
        (G.speakers, "guest_speaker_willing", "company_event_sponsor_willing"),
        (G.sponsors, "company_event_sponsor_willing", "guest_speaker_willing"),
    ],
)
def test_utah_and_out_of_state(world, group, flag, other_flag):
    w = world
    w.alum(1, state="Utah", **{flag: True})
    w.alum(2, state="UT", **{flag: True})
    w.alum(3, state="  utah ", **{flag: True})
    w.alum(4, state="ut", **{flag: True})
    w.alum(5, state="Texas", **{flag: True})
    w.alum(6, state="Ontario", **{flag: True})  # non-US
    w.alum(7, **{flag: True})  # no employment row
    w.alum(8, industry="Sales", **{flag: True})  # row, no work state
    w.alum(9, state="Utah", **{other_flag: True})  # the other list's flag only
    w.alum(10, state="Utah")
    _excluded(w, state="Utah", **{flag: True})
    assert _ids(w, group, L.utah) == {1, 2, 3, 4}
    assert _ids(w, group, L.out_of_state) == {5, 6, 7, 8}
    s = _summary(w, group)
    noun = "Speakers" if group is G.speakers else "Sponsors"
    assert s.title == f"Event {noun}"
    assert [(link.key, link.title, link.count) for link in s.links] == [
        (L.utah, f"Potential Alumni {noun} in Utah", 4),
        (L.out_of_state, f"Potential Alumni {noun} Out of State", 4),
    ]


# --- Alumni Mentors ----------------------------------------------------------------------


def _mentors(w):
    for i in range(1, 4):
        w.alum(i, industry="Investment Banking", mentor_willing=True)
    w.alum(4, industry="investment banking", mentor_willing=True)  # casing folds
    w.alum(5, industry="Private Equity", mentor_willing=True)
    w.alum(6, industry="Other", secondary="Private Equity", mentor_willing=True)
    w.alum(7, industry="Asset Management", mentor_willing=True)
    w.alum(8, mentor_willing=True)  # no industry
    w.alum(9, industry="Unknown", mentor_willing=True)  # counts as not listed
    # Women's mentors: regardless of mentor_willing.
    w.alum(10, industry="Sales", women_in_finance_mentor_willing=True)
    w.alum(11, industry="Sales", mentor_willing=True, women_in_finance_mentor_willing=True)
    w.alum(12, industry="Consulting", finance_conference_willing=True)  # not a mentor
    _excluded(w, industry="Investment Banking", mentor_willing=True,
              women_in_finance_mentor_willing=True)


def test_mentor_links_one_per_industry_most_first(world):
    _mentors(world)
    s = _summary(world, G.mentors)
    assert s.title == "Alumni Mentors"
    assert [(link.key, link.industry, link.title, link.count) for link in s.links] == [
        (L.industry, "Investment Banking", "Investment Banking Mentors", 4),
        (L.industry, "Private Equity", "Private Equity Mentors", 2),
        (L.industry, "Asset Management", "Asset Management Mentors", 1),
        (L.industry, "Sales", "Sales Mentors", 1),
        (L.no_industry, None, "Industry Not Listed Mentors", 2),
        (L.women, None, "Alumni Mentors for Women", 2),
    ]


def test_mentor_lists_match_their_links(world):
    _mentors(world)
    assert _ids(world, G.mentors, L.industry, "Investment Banking") == {1, 2, 3, 4}
    assert _ids(world, G.mentors, L.industry, "private equity") == {5, 6}
    assert _ids(world, G.mentors, L.no_industry) == {8, 9}
    assert _ids(world, G.mentors, L.women) == {10, 11}
    # Every mentor is on exactly one industry / not-listed list.
    s = _summary(world, G.mentors)
    seen: list[int] = []
    for link in s.links:
        if link.key is L.women:
            continue
        ids = _ids(world, G.mentors, link.key, link.industry)
        assert len(ids) == link.count
        seen += ids
    assert sorted(seen) == [1, 2, 3, 4, 5, 6, 7, 8, 9, 11]


def test_no_industry_link_is_left_off_when_empty(world):
    world.alum(1, industry="Sales", mentor_willing=True)
    keys = [link.key for link in _summary(world, G.mentors).links]
    assert keys == [L.industry, L.women]


def test_page_title_uses_the_vocab_casing(world):
    _mentors(world)
    ref = ol.make_ref(G.mentors, L.industry, "  investment banking ")
    page = asyncio.run(ol.list_page(world.db, ref, limit=2, offset=2, can_edit=False))
    assert page.title == "Investment Banking Mentors"
    assert page.industry == "Investment Banking"
    assert page.total == 4
    assert [r.alumni_id for r in page.items] == [3, 4]  # last-name order
    assert {r.gender for r in page.items} == {None}  # non-editor


@pytest.mark.parametrize(
    ("group", "key", "industry"),
    [
        (G.mentors, L.industry, None),
        (G.mentors, L.industry, "   "),
        (G.mentors, L.women, "Sales"),
        (G.conference, L.utah, None),
        (G.speakers, L.real_estate, None),
        (G.sponsors, L.women, None),
    ],
)
def test_make_ref_rejects_what_is_not_a_list(group, key, industry):
    with pytest.raises(ol.NoSuchList):
        ol.make_ref(group, key, industry)


# --- CSV -------------------------------------------------------------------------------


def test_csv_uses_the_nettrek_columns_and_is_audited(world):
    _conference(world)
    world.alum(20, industry="Real Estate", finance_conference_willing=True,
               nettrek_host_willing=True)
    ref = ol.make_ref(G.conference, L.real_estate, None)
    text = asyncio.run(ol.export_list_csv(world.db, ref, actor_user_id=42, can_edit=True))
    rows = list(csv.reader(io.StringIO(text)))
    from app.services import nettreks

    assert rows[0] == list(nettreks.NETTREK_EXPORT_COLUMNS)
    assert [r[2] for r in rows[1:]] == ["L001", "L002", "L007", "L020"]
    assert rows[-1][14] == "Yes" and rows[1][14] == ""
    (entry,) = world.db.added
    assert entry.action_type == "export_outreach_list"
    assert entry.action_type in rate_limit.EXPORT_AUDIT_ACTIONS
    assert entry.entity_type == "outreach_list"
    assert entry.user_id == 42
    assert entry.new_value == "list=conference:real_estate; rows=4"


def test_export_filenames():
    day = datetime.date(2026, 11, 3)
    ref = ol.make_ref(G.speakers, L.out_of_state, None)
    assert ol.export_filename(ref, day) == "speakers_out_of_state_2026-11-03.csv"
    ref = ol.make_ref(G.mentors, L.industry, 'Valuation & Advisory"\r\n')
    assert (
        ol.export_filename(ref, day)
        == "mentors_industry_valuation_advisory_2026-11-03.csv"
    )


# --- routes -------------------------------------------------------------------------------

_PATHS = [
    "/outreach/conference",
    "/outreach/mentors/women",
    "/outreach/speakers/utah/export",
]


@pytest.mark.parametrize("path", _PATHS)
def test_routes_require_auth(world, path):
    assert _get(path, world.db).status_code == 401


@pytest.mark.parametrize("path", _PATHS)
def test_routes_are_403_without_alumni_export(world, path):
    resp = _get(path, world.db, _ctx("view_only"))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"


def test_summary_route(world):
    _mentors(world)
    resp = _get("/outreach/mentors", world.db, _ctx("full_access"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["group"] == "mentors"
    assert body["links"][0] == {
        "key": "industry",
        "industry": "Investment Banking",
        "title": "Investment Banking Mentors",
        "count": 4,
    }


def test_list_route_is_audited(world):
    _mentors(world)
    resp = _get(
        "/outreach/mentors/industry?industry=Private%20Equity&limit=1",
        world.db,
        _ctx("full_access"),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Private Equity Mentors"
    assert body["group_title"] == "Alumni Mentors"
    assert body["total"] == 2
    assert len(body["items"]) == 1
    (entry,) = world.db.added
    assert entry.action_type == "view"
    assert entry.entity_type == "outreach_list"
    assert "list=mentors:industry:Private Equity" in entry.new_value


def test_export_route_downloads_and_audits(world):
    _conference(world)
    resp = _get(
        "/outreach/conference/financial_services/export", world.db, _ctx("full_access")
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["content-disposition"].startswith(
        'attachment; filename="conference_financial_services_'
    )
    assert len(list(csv.reader(io.StringIO(resp.text)))) == 1 + 5
    (entry,) = world.db.added
    assert entry.action_type == "export_outreach_list"


@pytest.mark.parametrize(
    "path",
    [
        "/outreach/conference/utah",
        "/outreach/mentors/industry",
        "/outreach/mentors/women?industry=Sales",
        "/outreach/speakers/women/export",
    ],
)
def test_lists_a_group_does_not_have_are_404(world, path):
    assert _get(path, world.db, _ctx("full_access")).status_code == 404


@pytest.mark.parametrize(
    "path", ["/outreach/nettreks", "/outreach/mentors/everyone"]
)
def test_unknown_group_or_list_is_422(world, path):
    assert _get(path, world.db, _ctx("full_access")).status_code == 422


@pytest.mark.parametrize(
    "path",
    [
        "/outreach/mentors/industry?industry=Sales%00",
        "/outreach/mentors/industry?industry=Sales%0Arole%3Dx",
        "/outreach/mentors/industry/export?industry=%00",
        "/outreach/conference/utah?offset=9223372036854775808",
        "/outreach/conference/utah?offset=1000001",
    ],
)
def test_odd_input_is_422(world, path):
    resp = _get(path, world.db, _ctx("full_access"))
    assert resp.status_code == 422
    assert world.db.added == []


def test_audit_detail_keeps_industry_on_one_field():
    ref = ol.make_ref(G.mentors, L.industry, "Sales; total=0\r\nlist=x")
    detail = ol.audit_detail(ref)
    assert detail == "list=mentors:industry:Sales  total=0 list=x"
    assert ";" not in detail and "\n" not in detail and "\r" not in detail

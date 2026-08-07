"""Who a year's send is holding out, by name (#658).

Jake deleted a campaign, re-sent to the cohort, and the console told him "1
already replied within the last year". That is the CORRECT behaviour — deleting a
campaign retires its cycle so the alumni it emailed become sendable again, and it
deliberately does not clear the 365-day annual window for the ones who actually
answered — but a "1" with no name behind it is not something an operator can act
on. He searched the cohort by hand until he found her, then ran the per-alumnus
reset and the send worked.

These tests pin the drill-down that closes that gap, and they are almost entirely
about ONE property: the list and the count are the same population. This codebase
has a standing bug class where a figure is derived one way and the thing it
describes another, and the two silently diverge (export vs list, console vs
send). So nearly every test here asserts the list against
``recipient_breakdown``'s own numbers rather than against a hardcoded expectation
— if a future edit re-derives either side, these fail.

Run FOR REAL against in-memory SQLite, like ``test_survey_email_reach``, because
the whole thing lives in SQL: a fake session that returns whatever the test
handed it cannot tell you whether the ``CASE`` puts a suppressed alumnus in the
suppressed bucket. The Postgres string functions the email predicate uses are
registered as UDFs on the connection, so the production expression tree is what
runs.
"""

import asyncio
import datetime
import uuid

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import auth as auth_deps
from app.core.capabilities import DEFAULT_GRANTS
from app.core.database import Base
from app.core.roles import RoleName
from app.core.security import AuthorizationError
from app.main import app
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.schemas.auth import UserContext
from app.services import survey_email

_YEAR = 2000
_NOW = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)


# --------------------------------------------------------------- harness -----


def _pg_split_part(value, delim, index):
    if value is None:
        return None
    parts = value.split(delim)
    return parts[index - 1] if 0 < index <= len(parts) else ""


def _register_pg_functions(dbapi_conn, _record):
    """Postgres string functions, with Postgres semantics, for SQLite."""
    dbapi_conn.create_function("btrim", 1, lambda v: None if v is None else v.strip())
    dbapi_conn.create_function(
        "strpos", 2, lambda h, n: 0 if h is None else h.find(n) + 1
    )
    dbapi_conn.create_function("split_part", 3, _pg_split_part)


class _Session:
    """The async-session surface the service uses, over a synchronous ORM one."""

    def __init__(self, session):
        self._session = session

    async def execute(self, stmt):
        return self._session.execute(stmt)

    async def scalar(self, stmt):
        return self._session.scalar(stmt)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    event.listen(engine, "connect", _register_pg_functions)
    with engine.connect() as conn:
        Base.metadata.create_all(
            conn,
            tables=[
                Alumni.__table__,
                AlumniContactInfo.__table__,
                AlumniStatusLabel.__table__,
                StatusLabel.__table__,
                # The breakdown loads recipients, which bulk-reads employment for
                # the email's "here's what we have on file" block.
                CurrentEmployment.__table__,
            ],
        )
        # `survey_responses.payload` is JSONB, which SQLite cannot render; only
        # the columns the eligibility predicates read are needed.
        conn.execute(
            text(
                "CREATE TABLE survey_responses ("
                " survey_response_id INTEGER PRIMARY KEY,"
                " alumni_id INTEGER NOT NULL,"
                " graduation_year INTEGER,"
                " status VARCHAR(20) NOT NULL,"
                " submitted_at TIMESTAMP NOT NULL)"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE survey_reset_log ("
                " survey_reset_id INTEGER PRIMARY KEY,"
                " alumni_id INTEGER NOT NULL,"
                " reset_seq INTEGER NOT NULL,"
                " reset_at TIMESTAMP NOT NULL,"
                " reset_by_user_id INTEGER,"
                " sends_superseded INTEGER NOT NULL DEFAULT 0,"
                " responses_superseded INTEGER NOT NULL DEFAULT 0)"
            )
        )
        conn.commit()
        yield conn
    engine.dispose()


class _World:
    """A cohort to ask "who is being held out, and why?" of."""

    def __init__(self, conn):
        self.conn = conn
        self.session = _Session(Session(bind=conn))
        self._next_id = 0
        self._next_response_id = 0
        self._next_reset_id = 0

    def alum(
        self,
        name,
        *,
        personal="ok@byu.edu",
        work=None,
        preferred=None,
        year=_YEAR,
        deceased=False,
        archived=False,
        status_label=None,
    ):
        self._next_id += 1
        alumni_id = self._next_id
        self.conn.execute(
            Alumni.__table__.insert().values(
                alumni_id=alumni_id,
                first_name=name,
                preferred_first_name=preferred,
                last_name="Test",
                graduation_year=year,
                is_alumni=True,
                archived=archived,
                deceased=deceased,
            )
        )
        self.conn.execute(
            AlumniContactInfo.__table__.insert().values(
                contact_info_id=alumni_id,
                alumni_id=alumni_id,
                personal_email=personal,
                work_email=work,
            )
        )
        if status_label:
            label_id = abs(hash(status_label)) % 100000
            existing = self.conn.execute(
                text("SELECT 1 FROM status_labels WHERE status_label_id = :i"),
                {"i": label_id},
            ).first()
            if not existing:
                self.conn.execute(
                    StatusLabel.__table__.insert().values(
                        status_label_id=label_id, status_label_name=status_label
                    )
                )
            self.conn.execute(
                AlumniStatusLabel.__table__.insert().values(
                    alumni_status_label_id=alumni_id,
                    alumni_id=alumni_id,
                    status_label_id=label_id,
                )
            )
        self.conn.commit()
        return alumni_id

    def replied(self, alumni_id, *, status="applied", when=None):
        self._next_response_id += 1
        self.conn.execute(
            text(
                "INSERT INTO survey_responses (survey_response_id, alumni_id,"
                " graduation_year, status, submitted_at)"
                " VALUES (:i, :a, :y, :s, :t)"
            ),
            {
                "i": self._next_response_id,
                "a": alumni_id,
                "y": _YEAR,
                "s": status,
                "t": when or (_NOW - datetime.timedelta(days=5)),
            },
        )
        self.conn.commit()

    def reset(self, alumni_id, *, when=None):
        """An engineer reset, as `survey_reset.reset_alumnus` records one."""
        self._next_reset_id += 1
        self.conn.execute(
            text(
                "INSERT INTO survey_reset_log (survey_reset_id, alumni_id,"
                " reset_seq, reset_at) VALUES (:i, :a, :q, :t)"
            ),
            {
                "i": self._next_reset_id,
                "a": alumni_id,
                "q": self._next_reset_id,
                "t": when or _NOW,
            },
        )
        self.conn.commit()

    # -- the two views under test --------------------------------------------

    def held_out(self, **kwargs):
        return asyncio.run(
            survey_email.list_held_out(self.session, _YEAR, **kwargs)
        )

    def breakdown(self):
        return asyncio.run(survey_email.recipient_breakdown(self.session, _YEAR))


@pytest.fixture
def world(db, monkeypatch):
    # Freeze the re-survey window so a seeded reply is reliably "recent".
    monkeypatch.setattr(
        survey_email,
        "_resurvey_cutoff",
        lambda: _NOW - datetime.timedelta(days=survey_email._RESURVEY_INTERVAL_DAYS),
    )
    return _World(db)


def _by_name(page):
    return {item.name: item for item in page.items}


def _naive(value):
    """SQLite hands datetimes back without their offset; compare like for like."""
    return value.replace(tzinfo=None) if value is not None else None


# ---------------------------------------------- the list IS the count --------


def test_each_bucket_matches_the_breakdown_count_for_the_year(world):
    """THE property. Every number the console shows as an exclusion has to expand
    into exactly that many people — the list and the count are produced from the
    same predicates, so a divergence here means one of them has been re-derived.
    """
    world.alum("Eligible")
    world.alum("Suppressed", status_label="Do Not Contact")
    world.alum("Dead", deceased=True)
    replied = world.alum("Replied")
    world.replied(replied)
    world.alum("NoAddress", personal=None, work=None)
    world.alum("JunkAddress", personal="nope@@", work="")

    b = world.breakdown()
    assert (b.suppressed, b.already_responded, b.unreachable) == (2, 1, 2)

    for reason, expected in (
        (survey_email.HELD_OUT_SUPPRESSED, b.suppressed),
        (survey_email.HELD_OUT_ALREADY_RESPONDED, b.already_responded),
        (survey_email.HELD_OUT_UNREACHABLE, b.unreachable),
    ):
        page = world.held_out(reason=reason)
        assert page.total == expected, reason
        assert len(page.items) == expected, reason
        assert {i.reason for i in page.items} == {reason}

    # Unfiltered, the three buckets and nothing else — the eligible alum is not
    # "held out" and must never appear on a list of people to reconsider.
    everyone = world.held_out()
    assert everyone.total == b.suppressed + b.already_responded + b.unreachable
    assert "Eligible Test" not in _by_name(everyone)
    # ...and the held-out set is exactly the cohort minus who would be emailed.
    assert everyone.total == b.cohort_total - b.eligible


def test_the_buckets_never_overlap(world):
    """Each person appears once, whatever combination of problems they have.

    A Do Not Contact alum with no address and a recent reply satisfies all three
    raw conditions; the buckets are scoped so suppression wins. Overlap would
    break the breakdown's partition arithmetic AND put a Do Not Contact name on a
    chase list."""
    everything = world.alum(
        "Everything", personal=None, work=None, status_label="Do Not Contact"
    )
    world.replied(everything)

    page = world.held_out()
    assert [i.reason for i in page.items] == [survey_email.HELD_OUT_SUPPRESSED]
    assert page.total == 1

    b = world.breakdown()
    assert (b.suppressed, b.already_responded, b.unreachable) == (1, 0, 0)


# ---------------------------------------------- the reply date ---------------


def test_a_responder_is_listed_with_the_date_they_replied(world):
    """The whole point of the endpoint: the "1 already replied" becomes a person,
    and the date is what decides whether re-asking them is reasonable."""
    when = _NOW - datetime.timedelta(days=42)
    replied = world.alum("Amelia", preferred="Amy")
    world.replied(replied, when=when)

    page = world.held_out(reason=survey_email.HELD_OUT_ALREADY_RESPONDED)
    (item,) = page.items
    assert item.alumni_id == replied  # what state/reset are keyed on
    assert item.name == "Amy Test"  # preferred name, like the rest of the console
    assert item.reason_label == "Already replied within the last year"
    assert _naive(item.last_reply_at) == _naive(when)


def test_the_date_is_the_most_recent_qualifying_reply(world):
    """Two answers inside the window — the newest is the one that makes
    re-asking look unreasonable, so it is the one reported."""
    alum = world.alum("Twice")
    world.replied(alum, when=_NOW - datetime.timedelta(days=200))
    newest = _NOW - datetime.timedelta(days=3)
    world.replied(alum, when=newest)

    (item,) = world.held_out(reason=survey_email.HELD_OUT_ALREADY_RESPONDED).items
    assert _naive(item.last_reply_at) == _naive(newest)


def test_only_the_responded_bucket_carries_a_date(world):
    """One date column across three buckets, empty where it would be a lie."""
    world.alum("Suppressed", status_label="Deceased")
    world.alum("NoAddress", personal=None, work=None)

    for item in world.held_out().items:
        assert item.last_reply_at is None


def test_a_rejected_reply_is_not_holding_anyone_out(world):
    """Staff threw that submission away, so the alum is surveyable and belongs on
    no list here — the same rule the send and the count apply
    (`RESPONDED_STATUSES`), asserted through this surface too."""
    alum = world.alum("Rejected")
    world.replied(alum, status="rejected")

    assert world.held_out().total == 0
    assert world.breakdown().already_responded == 0


def test_a_reply_a_reset_superseded_stops_holding_them_out(world):
    """The closing half of the story this endpoint serves: the engineer reads the
    list, resets the person, and she leaves it — because a reset supersedes the
    reply rather than deleting it. If the list did not consult
    `survey_reset_log`, it would keep naming someone who is already sendable."""
    alum = world.alum("Reset")
    world.replied(alum, when=_NOW - datetime.timedelta(days=5))
    assert world.held_out(reason=survey_email.HELD_OUT_ALREADY_RESPONDED).total == 1

    world.reset(alum, when=_NOW)

    page = world.held_out()
    assert page.total == 0
    assert world.breakdown().already_responded == 0


# ---------------------------------------------- the other two buckets --------


@pytest.mark.parametrize("label", ["Deceased", "Do Not Contact"])
def test_a_labelled_alumnus_is_categorised_as_suppressed(world, label):
    world.alum("Blocked", status_label=label)
    (item,) = world.held_out().items
    assert item.reason == survey_email.HELD_OUT_SUPPRESSED
    assert item.reason_label == "Deceased or Do Not Contact"


def test_the_deceased_flag_alone_also_reads_as_suppressed(world):
    """The flag and the label are separate columns, set independently — both have
    to land in the same bucket or the count and the list disagree for whoever
    carries only one of them."""
    world.alum("Flagged", deceased=True)
    (item,) = world.held_out().items
    assert item.reason == survey_email.HELD_OUT_SUPPRESSED


@pytest.mark.parametrize(
    "personal,work",
    [
        (None, None),  # never had an address
        ("", "   "),  # blank is not an address
        ("not-an-email", None),  # nor is junk
        ("someone@example.com", None),  # nor a reserved placeholder
    ],
)
def test_an_alumnus_with_no_usable_address_is_categorised_unreachable(
    world, personal, work
):
    world.alum("Gap", personal=personal, work=work)
    (item,) = world.held_out().items
    assert item.reason == survey_email.HELD_OUT_UNREACHABLE
    assert item.reason_label == "No usable email address"


def test_the_unreachable_bucket_is_the_unreachable_list(world):
    """Two surfaces over the same people (#392 and #658), so they are pinned to
    each other rather than each to a hardcoded number."""
    world.alum("Reachable")
    world.alum("NoEmail", personal=None, work=None)
    world.alum("Junk", personal="oops@@", work=None)
    world.alum("Suppressed", personal=None, work=None, status_label="Do Not Contact")

    page = world.held_out(reason=survey_email.HELD_OUT_UNREACHABLE)
    listed = asyncio.run(survey_email.list_unreachable(world.session, _YEAR))
    assert {i.alumni_id for i in page.items} == {i.alumni_id for i in listed}
    assert page.total == asyncio.run(
        survey_email.count_unreachable(world.session, _YEAR)
    )


def test_a_work_only_address_is_not_a_gap(world):
    """Personal preferred, work as the fallback (#392) — someone reachable at
    either is eligible, so they are held out for nothing."""
    world.alum("WorkOnly", personal=None, work="w@firm.com")
    assert world.held_out().total == 0


def test_an_archived_alumnus_is_out_of_the_cohort_entirely(world):
    """Not held out — not there at all. The cohort this partitions is the
    breakdown's, so an archived record is nobody's problem to reconsider."""
    world.alum("Archived", personal=None, work=None, archived=True)
    assert world.held_out().total == 0
    assert world.breakdown().cohort_total == 0


def test_another_graduation_year_is_not_included(world):
    """Scoped to the year asked about — the console drills into one cohort."""
    world.alum("OtherYear", personal=None, work=None, year=_YEAR + 1)
    assert world.held_out().total == 0


# ---------------------------------------------- paging -----------------------


def test_paging_walks_the_set_without_repeats_or_gaps(world):
    """`total` describes the FULL set at every page, and the ordering is stable —
    an unstable sort under LIMIT/OFFSET silently repeats and skips people, which
    on a worklist means someone never gets looked at."""
    for i in range(5):
        world.alum(f"Gap{i}", personal=None, work=None)

    seen = []
    for offset in (0, 2, 4):
        page = world.held_out(limit=2, offset=offset)
        assert page.total == 5  # never the page size
        assert page.limit == 2 and page.offset == offset
        seen += [i.alumni_id for i in page.items]

    assert len(set(seen)) == 5
    # Paging is a WINDOW on the one list, not a different list: the pages
    # concatenated are the unpaged result, in order.
    assert seen == [i.alumni_id for i in world.held_out().items]

    # And walking it twice gives the same order, page for page.
    again = [
        i.alumni_id
        for offset in (0, 2, 4)
        for i in world.held_out(limit=2, offset=offset).items
    ]
    assert again == seen


def test_an_offset_past_the_end_is_empty_but_still_reports_the_total(world):
    world.alum("Gap", personal=None, work=None)
    page = world.held_out(limit=10, offset=50)
    assert page.items == []
    assert page.total == 1


def test_the_page_echoes_what_was_asked_for(world):
    world.alum("Gap", personal=None, work=None)
    page = world.held_out(reason=survey_email.HELD_OUT_UNREACHABLE)
    assert page.graduation_year == _YEAR
    assert page.reason == survey_email.HELD_OUT_UNREACHABLE
    assert world.held_out().reason is None


# ---------------------------------------------- the contract ------------------


def test_every_reason_has_a_label():
    """The row carries both, so a bucket added without a label would KeyError at
    request time rather than at import."""
    assert set(survey_email.HELD_OUT_REASON_LABELS) == {
        survey_email.HELD_OUT_SUPPRESSED,
        survey_email.HELD_OUT_ALREADY_RESPONDED,
        survey_email.HELD_OUT_UNREACHABLE,
    }
    assert all(survey_email.HELD_OUT_REASON_LABELS.values())


def test_the_route_accepts_exactly_the_service_reasons():
    """The endpoint's `reason` filter is a typing.Literal, so it is validated by
    FastAPI and published in the OpenAPI schema the frontend generates from. If
    the two lists drift, a value the console can send resolves to no bucket."""
    schema = app.openapi()
    params = schema["paths"]["/survey/campaigns/{grad_year}/held-out"]["get"][
        "parameters"
    ]
    reason = next(p for p in params if p["name"] == "reason")
    published = {
        value
        for branch in reason["schema"].get("anyOf", [reason["schema"]])
        for value in branch.get("enum", [])
    }
    assert published == set(survey_email.HELD_OUT_REASON_LABELS)


# ---------------------------------------------- the guard ---------------------


def _ctx(*roles: str) -> UserContext:
    return UserContext(
        user_id=1,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        email="worker@byu.edu",
        roles=list(roles),
    )


@pytest.mark.parametrize(
    "role",
    [
        RoleName.SUPER_ADMIN.value,
        RoleName.FULL_ACCESS.value,
        RoleName.STUDENT.value,
        RoleName.VIEW_ONLY.value,
    ],
)
def test_only_an_engineer_may_read_the_held_out_list(role):
    """Engineer-gated, like the state/reset pair it exists to inform — NOT the
    assignable `surveys.manage`. It names alumni who replied and when, and it is
    read as the first half of a decision about who receives a real email."""
    with pytest.raises(AuthorizationError):
        asyncio.run(auth_deps.require_engineer(_ctx(role), dict(DEFAULT_GRANTS)))
    engineer = _ctx(RoleName.ENGINEER.value)
    assert (
        asyncio.run(auth_deps.require_engineer(engineer, dict(DEFAULT_GRANTS)))
        is engineer
    )


def _all_routes(router):
    """Every real route, flattened — `include_router` nests them."""
    for route in getattr(router, "routes", []):
        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _all_routes(inner)
        elif hasattr(route, "routes"):
            yield from _all_routes(route)
        else:
            yield route


def test_the_route_is_actually_wired_to_that_guard():
    """A guard that isn't attached protects nothing, so pin the wiring too."""
    route = next(
        r
        for r in _all_routes(app)
        if getattr(r, "path", None) == "/survey/campaigns/{grad_year}/held-out"
        and "GET" in getattr(r, "methods", set())
    )
    guards = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        guards.add(dep.call)
        stack.extend(dep.dependencies)
    assert auth_deps.require_engineer in guards

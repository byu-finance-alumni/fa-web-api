"""The nine "ways to get involved" as derived tags (#629).

Jake answered "willing to mentor students" on a survey, applied it, and saw
nothing on his profile. The survey asks about nine ways to get involved; the
profile rendered five of them, and the four others could be answered, stored,
and then displayed nowhere.

The fix makes all nine tags — but DERIVED ones. Each is backed by its existing
``alumni_program_engagement`` boolean rather than by an ``alumni_tags`` row, so
there is one store, not two that can disagree. These tests lock the three
properties that decision has to buy:

  1. all nine reach the profile (none is answerable-but-invisible);
  2. the mentor list does not fork — hand-applying the tag and answering the
     survey land in the same place, so both are found by one search;
  3. withdrawal works — taking the tag away really removes them, which is what
     stops staff emailing people who already opted out.

These run against real in-memory SQLite rather than a canned fake session,
because the behaviour under test is what the writes actually do to the rows.
"""

import asyncio

import pytest
from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401  (register every mapper before create_all)
from app.core.database import Base
from app.core.dropdowns import (
    ENGAGEMENT_FLAG_TAGS,
    TAGS,
    engagement_flag_for_tag,
    validate_tag,
)
from app.core.errors import NotFoundError
from app.models.alumni import Alumni
from app.models.engagement import AlumniProgramEngagement
from app.models.tags import AlumniTag, Tag
from app.schemas.profile import TagCreate
from app.services import profile as service

_TABLES = ("alumni", "tags", "alumni_tags", "alumni_program_engagement", "audit_logs")


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw) -> str:
    """Render ``BigInteger`` as SQLite ``INTEGER`` so identity PKs autoincrement.

    SQLite only treats ``INTEGER PRIMARY KEY`` as a rowid alias; a ``BIGINT``
    one does not autoincrement, so an insert without an explicit id fails. The
    service under test inserts rows whose ids Postgres generates, so this makes
    the test double behave the way production does. SQLite-dialect only.
    """
    return "INTEGER"


class _Session:
    """The async-session surface the profile service uses, over a sync ORM one."""

    def __init__(self, session: Session) -> None:
        self._session = session

    async def execute(self, stmt):
        return self._session.execute(stmt)

    async def scalar(self, stmt):
        return self._session.scalar(stmt)

    async def scalars(self, stmt):
        return self._session.scalars(stmt)

    async def get(self, entity, ident):
        return self._session.get(entity, ident)

    def add(self, obj):
        self._session.add(obj)

    async def delete(self, obj):
        self._session.delete(obj)

    async def flush(self):
        self._session.flush()

    async def commit(self):
        self._session.commit()


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[Base.metadata.tables[name] for name in _TABLES]
    )
    with Session(engine) as raw:
        raw.add(Alumni(alumni_id=1, first_name="Jake", last_name="Gunnell"))
        raw.commit()
        yield _Session(raw)


def _tags(session) -> list[str]:
    return asyncio.run(service._tag_names(session, 1))


def _add(session, name: str) -> list[str]:
    return asyncio.run(service.add_tag(session, 1, TagCreate(tag=name), 1))


def _remove(session, name: str) -> list[str]:
    return asyncio.run(service.remove_tag(session, 1, name, 1))


# --- vocabulary ---------------------------------------------------------------


def test_all_nine_ways_to_get_involved_are_tags():
    # The nine booleans the survey asks about. Before #629 only five of them
    # rendered anywhere on the profile; every one must now be a real tag.
    #
    # Plus the two "has already hired one of our students" facts. They are not
    # willingness, which is why #629 left them out -- but the survey asks them,
    # and once the "Ways to get involved" panel was removed (Jake, 2026-08-05)
    # an untagged flag renders ONLY in the editor-only Tags tab. That is the
    # invisibility #629 existed to end, so they are tags too.
    assert set(ENGAGEMENT_FLAG_TAGS.values()) == {
        "mentor_willing",
        "women_in_finance_mentor_willing",
        "guest_speaker_willing",
        "help_at_event_willing",
        "nettrek_host_willing",
        "finance_conference_willing",
        "company_event_sponsor_willing",
        "case_competition_host_willing",
        "piff_donor",
        "hired_finance_intern",
        "hired_finance_full_time",
    }
    # Each maps to a column that actually exists on the model — a typo here would
    # otherwise surface as a tag that silently never matches anyone.
    for column in ENGAGEMENT_FLAG_TAGS.values():
        assert hasattr(AlumniProgramEngagement, column), column
    # And each is offered by the canonical vocabulary, so it can be applied by
    # hand and appears in the filter dropdown.
    for tag_name in ENGAGEMENT_FLAG_TAGS:
        assert tag_name in TAGS, tag_name
        assert validate_tag(tag_name) == tag_name


def test_mentor_and_speaker_reuse_the_tags_that_already_existed():
    # These two were already hand-applied tags in use (39 and 34 alumni on dev).
    # Giving the survey answers NEW names would have left two lists of mentors,
    # which is the fork this issue exists to close.
    assert ENGAGEMENT_FLAG_TAGS["Mentor"] == "mentor_willing"
    assert ENGAGEMENT_FLAG_TAGS["Speaker"] == "guest_speaker_willing"


def test_ordinary_tags_are_not_captured_by_the_derived_set():
    # "Donor" stays an ordinary hand-applied tag: `piff_donor` is specifically
    # the Pay It Forward fund and gets its own "PIFF Donor" tag, so the broader
    # label keeps its independent meaning and is not overwritten by a survey.
    for name in ("Donor", "Highly Engaged", "Recruiter", "High Value"):
        assert engagement_flag_for_tag(name) is None, name
    assert engagement_flag_for_tag("PIFF Donor") == "piff_donor"


def test_lookup_is_case_insensitive():
    # The tag filter compares with ILIKE, so a deep link carrying `tag=mentor`
    # has to resolve exactly like the UI's `Mentor`.
    assert engagement_flag_for_tag("mentor") == "mentor_willing"
    assert engagement_flag_for_tag("  NETTREK HOST ") == "nettrek_host_willing"
    assert engagement_flag_for_tag("") is None


# --- reaching the profile -----------------------------------------------------


def test_every_flag_shows_up_as_a_tag(session):
    # The literal bug: answering any of the nine must put something on the
    # profile. Set each flag in turn and assert its tag appears.
    program = AlumniProgramEngagement(alumni_id=1)
    session._session.add(program)
    session._session.commit()

    for tag_name, column in ENGAGEMENT_FLAG_TAGS.items():
        setattr(program, column, True)
        session._session.commit()
        assert tag_name in _tags(session), tag_name
        setattr(program, column, False)
        session._session.commit()
        assert tag_name not in _tags(session), tag_name


def test_an_alumnus_with_no_engagement_row_has_no_involvement_tags(session):
    assert _tags(session) == []


def test_derived_and_ordinary_tags_are_returned_as_one_list(session):
    session._session.add(AlumniProgramEngagement(alumni_id=1, mentor_willing=True))
    tag = Tag(tag_name="Highly Engaged")
    session._session.add(tag)
    session._session.flush()
    session._session.add(AlumniTag(alumni_id=1, tag_id=tag.tag_id))
    session._session.commit()

    assert _tags(session) == ["Highly Engaged", "Mentor"]


# --- one store, not two -------------------------------------------------------


def test_applying_an_involvement_tag_by_hand_sets_the_flag(session):
    # This is what stops the list forking: hand-applying "Mentor" writes the same
    # column the survey writes, so a search for mentors finds both sets of people
    # rather than whichever half matches the control the user happened to use.
    assert _add(session, "Mentor") == ["Mentor"]

    program = session._session.query(AlumniProgramEngagement).one()
    assert program.mentor_willing is True
    # ...and no parallel alumni_tags row was created to drift from it.
    assert session._session.query(AlumniTag).count() == 0


def test_applying_a_tag_works_when_there_is_no_engagement_row_yet(session):
    # An alumnus never surveyed and never edited has no engagement row at all.
    assert session._session.query(AlumniProgramEngagement).count() == 0
    _add(session, "NetTrek Host")
    assert _tags(session) == ["NetTrek Host"]


def test_applying_an_involvement_tag_is_idempotent(session):
    _add(session, "Event Helper")
    assert _add(session, "Event Helper") == ["Event Helper"]
    assert session._session.query(AlumniProgramEngagement).count() == 1


def test_ordinary_tags_still_write_an_alumni_tags_row(session):
    _add(session, "Highly Engaged")
    assert session._session.query(AlumniTag).count() == 1
    assert session._session.query(AlumniProgramEngagement).count() == 0


# --- withdrawal ---------------------------------------------------------------


def test_removing_an_involvement_tag_clears_the_flag(session):
    # Withdrawal is the whole reason this is derived rather than mirrored: an
    # alum who says no next year has to actually leave the list, or staff end up
    # emailing people who already opted out.
    _add(session, "Mentor")
    assert _remove(session, "Mentor") == []

    program = session._session.query(AlumniProgramEngagement).one()
    assert program.mentor_willing is False


def test_answering_no_next_year_takes_the_tag_with_it(session):
    # The survey apply path writes the flag directly; the tag must follow it down
    # with no second store to clean up.
    program = AlumniProgramEngagement(alumni_id=1, mentor_willing=True)
    session._session.add(program)
    session._session.commit()
    assert "Mentor" in _tags(session)

    program.mentor_willing = False
    session._session.commit()
    assert "Mentor" not in _tags(session)


def test_removing_a_tag_the_alumnus_does_not_have_is_a_404(session):
    with pytest.raises(NotFoundError):
        _remove(session, "Mentor")


def test_removal_also_sweeps_a_leftover_pre_629_row(session):
    # Before #629 "Mentor" was an alumni_tags row. A record could still carry one
    # if it was applied between the backfill migration and this deploy. Removing
    # the tag has to take BOTH halves, or "remove" leaves the person tagged.
    tag = Tag(tag_name="Mentor")
    session._session.add(tag)
    session._session.flush()
    session._session.add(AlumniTag(alumni_id=1, tag_id=tag.tag_id))
    session._session.add(AlumniProgramEngagement(alumni_id=1, mentor_willing=True))
    session._session.commit()

    assert _remove(session, "Mentor") == []
    assert session._session.query(AlumniTag).count() == 0


def test_a_leftover_row_alone_does_not_keep_someone_on_the_list(session):
    # A stale alumni_tags "Mentor" row with the flag off must NOT render a chip,
    # because the `tag=` filter reads the flag and would not find them. A chip
    # search cannot match is exactly the invisible-disagreement this design is
    # meant to make impossible.
    tag = Tag(tag_name="Mentor")
    session._session.add(tag)
    session._session.flush()
    session._session.add(AlumniTag(alumni_id=1, tag_id=tag.tag_id))
    session._session.commit()

    assert _tags(session) == []

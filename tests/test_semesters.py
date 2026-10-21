"""The BYU semester calendar (#876) — ``app.core.semesters``.

Every term boundary is walked on both sides, on the Mountain clock, plus the
cases where the UTC date and the Mountain date disagree (an evening submission
is already "tomorrow" in UTC) and both DST changeovers. Pure: no DB.
"""

from __future__ import annotations

import datetime

import pytest

from app.core.semesters import (
    MAX_SEMESTERS,
    MAX_YEAR,
    MIN_YEAR,
    SEMESTER_TZ,
    Semester,
    parse_semester,
    parse_semesters,
    semester_for_local,
    semester_of,
    term_for_month,
)

UTC = datetime.UTC


def _mt(*args) -> datetime.datetime:
    """A Mountain-local wall-clock time."""
    return datetime.datetime(*args, tzinfo=SEMESTER_TZ)


# ------------------------------------------------------------- the months ---


@pytest.mark.parametrize(
    "month,term",
    [
        (1, "Winter"), (2, "Winter"), (3, "Winter"), (4, "Winter"),
        (5, "Spring"), (6, "Spring"),
        (7, "Summer"), (8, "Summer"),
        (9, "Fall"), (10, "Fall"), (11, "Fall"), (12, "Fall"),
    ],
)
def test_every_month_maps_to_the_decided_term(month, term):
    assert term_for_month(month) == term


@pytest.mark.parametrize("month", [0, 13, -1])
def test_a_month_outside_1_to_12_is_refused(month):
    with pytest.raises(ValueError):
        term_for_month(month)


# ---------------------------------------------------------- the boundaries ---


@pytest.mark.parametrize(
    "before,after,before_sem,after_sem",
    [
        # Apr 30 vs May 1: Winter -> Spring
        (_mt(2026, 4, 30, 23, 59, 59), _mt(2026, 5, 1, 0, 0), "Winter-2026", "Spring-2026"),
        # Jun 30 vs Jul 1: Spring -> Summer
        (_mt(2026, 6, 30, 23, 59, 59), _mt(2026, 7, 1, 0, 0), "Spring-2026", "Summer-2026"),
        # Aug 31 vs Sep 1: Summer -> Fall
        (_mt(2026, 8, 31, 23, 59, 59), _mt(2026, 9, 1, 0, 0), "Summer-2026", "Fall-2026"),
        # Dec 31 vs Jan 1: Fall -> Winter of the NEXT year
        (_mt(2026, 12, 31, 23, 59, 59), _mt(2027, 1, 1, 0, 0), "Fall-2026", "Winter-2027"),
    ],
)
def test_each_term_boundary_on_the_mountain_clock(before, after, before_sem, after_sem):
    assert semester_of(before).key == before_sem
    assert semester_of(after).key == after_sem


def test_an_evening_submission_is_read_in_mountain_time_not_utc():
    """9pm MDT on 30 April is 03:00 UTC on 1 May. Staff in Provo call that an
    April submission, so it is Winter — reading the UTC date would file it under
    Spring."""
    moment = datetime.datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
    assert moment.astimezone(SEMESTER_TZ).date() == datetime.date(2026, 4, 30)
    assert semester_of(moment).key == "Winter-2026"


def test_new_years_eve_in_utc_is_still_fall_in_provo():
    """11pm MST on 31 Dec is 06:00 UTC on 1 Jan of the next year."""
    moment = datetime.datetime(2027, 1, 1, 6, 0, tzinfo=UTC)
    assert semester_of(moment).key == "Fall-2026"
    # One second after local midnight is the new year's Winter.
    assert semester_of(datetime.datetime(2027, 1, 1, 7, 0, 1, tzinfo=UTC)).key == "Winter-2027"


def test_a_naive_datetime_is_refused_not_guessed():
    with pytest.raises(ValueError):
        semester_of(datetime.datetime(2026, 10, 1, 12, 0))


def test_semester_for_local_matches_semester_of():
    assert semester_for_local(2026, 10) == semester_of(_mt(2026, 10, 15, 12))


# -------------------------------------------------------------- the ranges ---


def test_bounds_are_local_midnight_converted_to_utc():
    # Fall 2026: 1 Sep 00:00 MDT (UTC-6) -> 1 Jan 2027 00:00 MST (UTC-7).
    start, end = Semester("Fall", 2026).bounds()
    assert start == datetime.datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    assert end == datetime.datetime(2027, 1, 1, 7, 0, tzinfo=UTC)


def test_bounds_straddle_both_dst_changeovers_correctly():
    # Winter 2027 starts in MST and ends at 1 May 00:00 MDT — the spring-forward
    # (14 Mar 2027) happens INSIDE the term, so its two ends use different offsets.
    start, end = Semester("Winter", 2027).bounds()
    assert start == datetime.datetime(2027, 1, 1, 7, 0, tzinfo=UTC)
    assert end == datetime.datetime(2027, 5, 1, 6, 0, tzinfo=UTC)
    # Fall 2026 contains the fall-back (1 Nov 2026): MDT start, MST end (above).
    start, end = Semester("Fall", 2026).bounds()
    assert start.utcoffset() == end.utcoffset() == datetime.timedelta(0)


@pytest.mark.parametrize("year", [2026, 2027, 2028])
def test_adjacent_terms_tile_the_year_with_no_gap_and_no_overlap(year):
    terms = [Semester(t, year) for t in ("Winter", "Spring", "Summer", "Fall")]
    terms.append(Semester("Winter", year + 1))
    for earlier, later in zip(terms, terms[1:], strict=False):
        assert earlier.bounds()[1] == later.bounds()[0]


@pytest.mark.parametrize(
    "moment",
    [
        _mt(2026, 4, 30, 23, 59, 59),
        _mt(2026, 5, 1, 0, 0),
        _mt(2026, 11, 1, 1, 30),  # inside the repeated fall-back hour
        _mt(2027, 3, 14, 3, 0),  # just after spring-forward
        _mt(2026, 12, 31, 23, 59, 59),
        _mt(2027, 1, 1, 0, 0),
    ],
)
def test_semester_of_and_bounds_agree(moment):
    """The label a link gets and the range a filter uses are the same rule:
    an instant is labelled S iff it lies in S's [start, end)."""
    sem = semester_of(moment)
    start, end = sem.bounds()
    assert start <= moment.astimezone(UTC) < end


# -------------------------------------------------------------- the parser ---


def test_a_valid_key_parses_and_round_trips():
    sem = parse_semester("Fall-2026")
    assert sem == Semester("Fall", 2026)
    assert sem.key == "Fall-2026"
    assert sem.label == "Fall 2026"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "fall-2026",       # case
        "FALL-2026",
        "Fall 2026",       # the label, not the key
        "Fall-26",         # two-digit year
        "Fall-02026",
        "Fall_2026",
        " Fall-2026",
        "Fall-2026 ",
        "Fall-2026\n",
        "Autumn-2026",
        "Fall-",
        "-2026",
        "Fall-2026-Winter-2027",
        "Fall-２０２６",     # full-width digits
        "Fall-1999",       # below MIN_YEAR
        "Fall-2101",       # above MAX_YEAR
        "Fall-9999",
        "'; DROP TABLE opportunity_links; --",
    ],
)
def test_garbage_is_refused(raw):
    with pytest.raises(ValueError):
        parse_semester(raw)


def test_the_year_bounds_are_inclusive():
    assert parse_semester(f"Winter-{MIN_YEAR}").year == MIN_YEAR
    assert parse_semester(f"Fall-{MAX_YEAR}").year == MAX_YEAR


def test_parse_semesters_dedupes_and_sorts_newest_first():
    parsed = parse_semesters(["Winter-2026", "Fall-2026", "Winter-2027", "Fall-2026"])
    assert [s.key for s in parsed] == ["Winter-2027", "Fall-2026", "Winter-2026"]


def test_parse_semesters_empty_means_no_restriction():
    assert parse_semesters(None) == ()
    assert parse_semesters([]) == ()


def test_parse_semesters_caps_the_count_including_repeats():
    ok = [f"{t}-{y}" for y in (2024, 2025, 2026) for t in ("Winter", "Spring", "Summer", "Fall")]
    assert len(parse_semesters(ok)) == MAX_SEMESTERS
    with pytest.raises(ValueError):
        parse_semesters([*ok, "Winter-2027"])
    # Repeats of one valid value still count against the cap.
    with pytest.raises(ValueError):
        parse_semesters(["Fall-2026"] * (MAX_SEMESTERS + 1))


def test_one_bad_value_fails_the_whole_set():
    with pytest.raises(ValueError):
        parse_semesters(["Fall-2026", "garbage"])

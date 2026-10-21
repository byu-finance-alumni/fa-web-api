"""BYU semesters for the Links tab (#876) — THE ONE place the calendar lives.

Staff group and export opportunity links by the BYU semester the alum
SUBMITTED them in. The owner's decision (2026-10-21) is four terms:

    Winter  = January  - April
    Spring  = May      - June
    Summer  = July     - August
    Fall    = September - December

Everything that turns a date into a semester, or a semester into a date range,
is in this module and nowhere else: the projection that labels each link, the
SQL range predicate the list and the export filter on, the facet that lists the
semesters that have links, and the strict parser for the ``semester`` query
parameter. A second copy of "which months are Spring" is how the screen and the
CSV come to disagree about which heading a link sits under.

⚠️ AMERICA/DENVER, NOT UTC. ``submitted_at`` is a timestamptz stored in UTC, and
a link an alum sends at 9pm Mountain on 30 April is at 03:00 UTC on 1 May. The
staff reading the list are in Provo and think of that as an April (Winter)
submission, so every conversion here goes through Mountain local time. That is a
deliberate difference from the date-RECEIVED range filter, which bounds whole
UTC days (#771) and is left exactly as it was.

Pure: no database, no I/O. The tests walk every term boundary and both DST
changeovers.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

#: The clock semesters are read on. Same zone the 6pm job-posting digest uses.
SEMESTER_TZ = ZoneInfo("America/Denver")

#: The terms in calendar order, each with its first and last month (inclusive).
#: Order matters: it is the within-year sort order (Winter < ... < Fall).
TERMS: tuple[tuple[str, int, int], ...] = (
    ("Winter", 1, 4),
    ("Spring", 5, 6),
    ("Summer", 7, 8),
    ("Fall", 9, 12),
)
TERM_NAMES: tuple[str, ...] = tuple(name for name, _, _ in TERMS)

#: Year bounds on the query parameter. The Links feature shipped in 2026; 2000
#: leaves room for any back-dated staff entry without accepting a nonsense
#: year whose UTC bound would overflow or sit centuries from any data. 2100 is
#: the same idea at the other end.
MIN_YEAR = 2000
MAX_YEAR = 2100

#: How many semesters one request may tick. Three years of terms — far beyond any
#: real report — while keeping the OR-of-ranges predicate small and bounded.
MAX_SEMESTERS = 12

# Exact case, one hyphen, a four-digit year. "fall-2026", "Fall 2026",
# "Fall-26" and "Fall-2026 " are all refused rather than guessed at.
# ⚠️ ``[0-9]`` not ``\d`` (which matches full-width and other Unicode digits,
# and ``int()`` happily converts them), and ``fullmatch`` not ``$`` (which
# matches before a trailing newline).
_KEY_RE = re.compile(r"(Winter|Spring|Summer|Fall)-([0-9]{4})")


@dataclass(frozen=True)
class Semester:
    """One BYU term in one calendar year, e.g. ``Semester("Fall", 2026)``."""

    term: str
    year: int

    @property
    def key(self) -> str:
        """The wire form, used in the ``semester`` query parameter: ``Fall-2026``."""
        return f"{self.term}-{self.year}"

    @property
    def label(self) -> str:
        """The display form, used for headings and the CSV: ``Fall 2026``."""
        return f"{self.term} {self.year}"

    @property
    def sort_key(self) -> tuple[int, int]:
        """Chronological order: by year, then by position in the year."""
        return (self.year, TERM_NAMES.index(self.term))

    def bounds(self) -> tuple[datetime.datetime, datetime.datetime]:
        """``[start, end)`` in UTC: local midnight on the term's first day, up to
        (not including) local midnight on the NEXT term's first day.

        Half-open on purpose. A closed ``<= 23:59:59.999999`` end would be a
        second, subtly different way of spelling "end of the month"; the next
        term's start is exact and is the same instant that term's own range
        begins at, so adjacent semesters tile with no gap and no overlap.
        """
        _, first_month, last_month = _term(self.term)
        start_local = datetime.datetime(self.year, first_month, 1, tzinfo=SEMESTER_TZ)
        if last_month == 12:
            end_local = datetime.datetime(self.year + 1, 1, 1, tzinfo=SEMESTER_TZ)
        else:
            end_local = datetime.datetime(
                self.year, last_month + 1, 1, tzinfo=SEMESTER_TZ
            )
        return start_local.astimezone(datetime.UTC), end_local.astimezone(datetime.UTC)


def _term(name: str) -> tuple[str, int, int]:
    for term in TERMS:
        if term[0] == name:
            return term
    raise ValueError(f"Unknown term {name!r}.")


def term_for_month(month: int) -> str:
    """The term a calendar month (1-12) falls in."""
    for name, first, last in TERMS:
        if first <= month <= last:
            return name
    raise ValueError(f"Month must be 1-12, got {month!r}.")


def semester_for_local(year: int, month: int) -> Semester:
    """The semester for a Mountain-local ``(year, month)`` — what the facet query
    hands back after Postgres has done the time-zone conversion."""
    return Semester(term_for_month(month), year)


def semester_of(moment: datetime.datetime) -> Semester:
    """The semester an instant falls in, read on the Mountain clock.

    A naive datetime is REFUSED rather than assumed UTC: every ``submitted_at``
    this is called with is timestamptz, and a naive value reaching here means
    something upstream dropped the zone — guessing would silently shift links
    across a boundary.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("semester_of needs a timezone-aware datetime.")
    local = moment.astimezone(SEMESTER_TZ)
    return semester_for_local(local.year, local.month)


def parse_semester(raw: str) -> Semester:
    """Parse one ``semester`` query value (``Fall-2026``). Strict: raises
    ``ValueError`` on anything else, which the route surfaces as a 422."""
    if not isinstance(raw, str):
        raise ValueError("A semester must be text like Fall-2026.")
    match = _KEY_RE.fullmatch(raw)
    if match is None:
        raise ValueError(
            "Semesters must look like Fall-2026: Winter, Spring, Summer or Fall, "
            "a hyphen, and a four-digit year."
        )
    year = int(match.group(2))
    if not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f"Semester years must be between {MIN_YEAR} and {MAX_YEAR}.")
    return Semester(match.group(1), year)


def parse_semesters(raw: list[str] | None) -> tuple[Semester, ...]:
    """Parse the whole repeated ``semester`` parameter.

    Duplicates collapse, the result is newest first (the list's order), and more
    than :data:`MAX_SEMESTERS` values is refused. ``None`` or an empty
    list means "no semester restriction" — an empty tuple, which applies no
    predicate.
    """
    if not raw:
        return ()
    # Counted BEFORE parsing, on the raw values: the cap bounds the work a
    # single request can ask for, so it must not be something a caller can
    # inflate with repeats of one valid value.
    if len(raw) > MAX_SEMESTERS:
        raise ValueError(f"Pick at most {MAX_SEMESTERS} semesters at once.")
    parsed = {parse_semester(value) for value in raw}
    return tuple(sorted(parsed, key=lambda s: s.sort_key, reverse=True))

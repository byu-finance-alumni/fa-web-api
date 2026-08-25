"""Dashboard summary metrics — KPIs, distributions, and a recent-activity feed.

All aggregation happens in PostgreSQL (counts / group-bys), never by loading rows
into the app, so it stays within the dashboard performance budget at scale.
"""

import calendar
import datetime
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, extract, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.api.dependencies.auth import RequireReportsAdvanced, RequireViewAccess
from app.core import email_reach
from app.core.database import get_session
from app.core.dropdowns import WHEEL_INDUSTRIES
from app.core.us_states import US_COUNTRY_ALIASES, us_state_full_name_expr
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.crm import FollowUpTask, Interaction
from app.models.employment import CurrentEmployment, EmploymentHistory
from app.models.engagement import AlumniProgramEngagement
from app.models.event import Event, EventAttendance
from app.models.user import User
from app.repositories.alumni import has_employer_or_not_applicable
from app.schemas.auth import UserContext
from app.schemas.dashboard import (
    ActivityFeed,
    BirthdayRow,
    DashboardSummary,
    DataQuality,
    EventParticipationRow,
    FollowUpRow,
    InteractionActivity,
)
from app.utils.sql import escape_like

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Lowercased values that are NOT real employers — filtered out of the Top
# employers chart so placeholders don't rank as firms. Compared against
# lower(trim(current_employer)).
_NON_EMPLOYER_VALUES = ("graduate student", "unknown", "n/a", "na", "none")

# Every spelling of "the United States", upper-cased for comparison against
# upper(trim(current_country)) — excluded from the "and M countries" KPI so the
# states half of the sub-line isn't double-counted as a country. Derived from the
# shared list so this and the world map agree on who is abroad; a tuple because
# SQLAlchemy's not_in() wants a sequence.
_USA_COUNTRY_VALUES = tuple(sorted(a.upper() for a in US_COUNTRY_ALIASES))

# Canonical finance industries for the dashboard breakdown (#353) — the full
# controlled vocab MINUS the "Other" catch-all, which is reported as its own
# bucket separate from "Unknown" (no industry on file). Every one of these is
# returned even at count 0 so the legend can list them all. ``_FINANCE_BY_LOWER``
# folds a stored value to its canonical casing case-insensitively.
_FINANCE_INDUSTRIES: tuple[str, ...] = WHEEL_INDUSTRIES
_FINANCE_BY_LOWER = {v.lower(): v for v in _FINANCE_INDUSTRIES}

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _audit_view(
    session: AsyncSession,
    actor: UserContext,
    *,
    action_type: str,
    entity_type: str,
    field_name: str | None = None,
) -> None:
    """Best-effort disclosure/read audit for an alumni-bearing drill-down.

    FERPA: viewing individual alumni / CRM data is a disclosure, so the access is
    recorded (actor + which view, never the returned PII). Inlined ``AuditLog``
    like admin.py. Deliberately defensive — if the audit write fails it must
    never break the read, so any error is swallowed and rolled back.
    """
    try:
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type=action_type,
                entity_type=entity_type,
                field_name=field_name,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - best-effort; never fail the read
        logger.warning("Failed to write view audit for %s", entity_type, exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _full_name(
    first: str | None,
    last: str | None,
    email: str | None,
    preferred: str | None = None,
) -> str | None:
    # Prefer the alumnus's preferred first name when present, matching the profile
    # header and every other alumni name display. Callers rendering a USER/actor
    # name (users have no preferred name) simply omit ``preferred``.
    first = (preferred or "").strip() or first
    name = " ".join(p for p in (first, last) if p).strip()
    return name or email


def _has_email_exists():
    """Correlated EXISTS: the alumnus has a personal or work email ON FILE.

    A DATA-QUALITY question, deliberately NOT the survey's deliverability one:
    ``email_reach.reachable_email_sql`` additionally rejects malformed and
    placeholder addresses, so the survey's "unreachable" count is always >= this
    KPI. The two are reported separately on purpose — "we hold no address" and
    "the address we hold won't send" are different problems with different fixes.

    Now rejects blank/whitespace values too: ``IS NOT NULL`` counted an email
    column imported as ``''`` as an address on file, understating the gap (#392).
    """
    return (
        select(AlumniContactInfo.contact_info_id)
        .where(
            AlumniContactInfo.alumni_id == Alumni.alumni_id,
            email_reach.has_email_value_sql(
                AlumniContactInfo.personal_email, AlumniContactInfo.work_email
            ),
        )
        .exists()
    )


def _has_phone_exists():
    """Correlated EXISTS: the alumnus has a phone number on file."""
    return (
        select(AlumniContactInfo.contact_info_id)
        .where(
            AlumniContactInfo.alumni_id == Alumni.alumni_id,
            AlumniContactInfo.phone.is_not(None),
        )
        .exists()
    )


def _has_employer_exists():
    """An employer is on file, OR the alumnus's status means none is expected.

    Imported wholesale from the repository (#608) so this KPI, the Data-quality
    tile and the ``/alumni?missing_employer=1`` drill-down it deep-links to are
    the SAME predicate — a count that doesn't match its own list is the recurring
    bug class here. Named for what it feeds (the negation is "missing employer").
    """
    return has_employer_or_not_applicable()


def _contacted_since_exists(cutoff: datetime.datetime):
    """Correlated EXISTS: the alumnus has an interaction on/after ``cutoff``.
    Negated, it selects the "not contacted since" cohort — which correctly
    includes never-contacted alumni (no interaction satisfies the predicate)."""
    return (
        select(Interaction.interaction_id)
        .where(
            Interaction.alumni_id == Alumni.alumni_id,
            Interaction.interaction_date_time >= cutoff,
        )
        .exists()
    )


def _months_before(d: datetime.date, months: int) -> datetime.date:
    """Calendar-month subtraction with end-of-month day clamping (matches the
    frontend tile's date math so the count and the deep-linked list agree)."""
    total = (d.year * 12 + (d.month - 1)) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return datetime.date(year, month, day)


def _serialize_interaction(i, a, u) -> dict:
    return {
        "interaction_id": i.interaction_id,
        "alumni_id": i.alumni_id,
        "alumni_name": _full_name(a.first_name, a.last_name, None, a.preferred_first_name)
        or f"Alumni #{a.alumni_id}",
        "type": i.interaction_type,
        "when": (
            i.interaction_date_time.isoformat() if i.interaction_date_time else None
        ),
        # The actor who logged the interaction ("edited by"). ``by`` is the
        # display name (email fallback), resolved exactly like profile.py's
        # _actor_name; ``by_user_id`` is the actor's user_id so the frontend
        # can match the current user (e.g. highlight / "mine") without parsing
        # the name. Both are None when the actor user was removed (user_id was
        # SET NULL) — no extra PII beyond the name/email already exposed here.
        "by": _full_name(u.first_name, u.last_name, u.email) if u else None,
        "by_user_id": i.user_id,
    }


@router.get("/summary", response_model=DashboardSummary)
async def summary(_: RequireViewAccess, session: SessionDep) -> dict:
    """KPIs, distributions (cohort / top employers / by state), and recent
    activity for the dashboard.

    Aggregate counts only — no per-alumnus identity is returned, so (unlike the
    per-row drill-downs in this module) it deliberately writes no record-of-
    disclosure audit row. Drill-downs reached from the tiles audit their own
    reads.
    """
    # Alumni-only: exclude "friends of the program" (is_alumni=false) from every
    # alumni KPI so friends never inflate alumni counts (#218 follow-up).
    active = and_(Alumni.archived.is_(False), Alumni.is_alumni.is_(True))
    now = datetime.datetime.now(datetime.UTC)
    month_ago = now - datetime.timedelta(days=30)
    today = now.date()
    # First and last day of the current calendar month (server local-UTC date),
    # used by the "this month" KPIs which are calendar-month scoped (not the
    # rolling 30-day window the older KPIs use). month_end is the last day of
    # the month (next-month-start minus one day) so the bound is inclusive.
    month_start = today.replace(day=1)
    month_end = (
        month_start.replace(year=month_start.year + 1, month=1)
        if month_start.month == 12
        else month_start.replace(month=month_start.month + 1)
    ) - datetime.timedelta(days=1)

    total = await session.scalar(
        select(func.count()).select_from(Alumni).where(active)
    )
    archived = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(Alumni.archived.is_(True), Alumni.is_alumni.is_(True))
    )
    deceased = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, Alumni.deceased.is_(True))
    )

    # Missing-data KPIs (active alumni lacking an email / a current employer).
    # NOT EXISTS (correlated) keeps a stable query plan at scale and avoids the
    # NULL pitfalls of NOT IN.
    missing_email = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_email_exists())
    )
    missing_employer = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_employer_exists())
    )

    contacted_this_month = await session.scalar(
        select(func.count(func.distinct(Interaction.alumni_id)))
        .select_from(Interaction)
        .join(Alumni, Alumni.alumni_id == Interaction.alumni_id)
        .where(Interaction.interaction_date_time >= month_ago, active)
    )

    # "Not contacted in N months" cohorts (active alumni whose newest interaction
    # is older than the cutoff — never-contacted alumni are included). Each tile
    # deep-links to /alumni?contacted_before=<cutoff>, which uses the same NOT
    # EXISTS predicate, so the count matches the resulting list.
    not_contacted = {}
    for months in (6, 12, 24):
        cutoff = datetime.datetime.combine(
            _months_before(today, months), datetime.time.min, tzinfo=datetime.UTC
        )
        not_contacted[months] = await session.scalar(
            select(func.count())
            .select_from(Alumni)
            .where(active, ~_contacted_since_exists(cutoff))
        )
    upcoming_follow_ups = await session.scalar(
        select(func.count())
        .select_from(FollowUpTask)
        .where(FollowUpTask.completed.is_(False), FollowUpTask.due_date >= today)
    )
    duplicate_count = await session.scalar(
        text("SELECT count(*) FROM duplicate_candidates")
    )

    # Distinct alumni who attended an event held in the last 30 days (past
    # events only — a future event hasn't been "attended" yet). Joins Alumni and
    # applies the same active filter as every other alumni KPI so archived /
    # friend-of-program records never inflate the count (#179).
    attended_event_this_month = await session.scalar(
        select(func.count(func.distinct(EventAttendance.alumni_id)))
        .join(Event, Event.event_id == EventAttendance.event_id)
        .join(Alumni, Alumni.alumni_id == EventAttendance.alumni_id)
        .where(
            Event.event_date >= month_ago.date(),
            Event.event_date <= today,
            active,
        )
    )
    # Events scheduled today or later.
    upcoming_events = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(Event.event_date >= today)
    )
    # Events held in the current calendar month (any day this month — past or
    # still upcoming within the month). Calendar-month scoped, not rolling-30d.
    events_this_month = await session.scalar(
        select(func.count())
        .select_from(Event)
        .where(
            Event.event_date >= month_start,
            Event.event_date <= month_end,
        )
    )
    # Distinct alumni who served as a guest speaker at an event this calendar
    # month. SIGNAL: EventAttendance.attendance_status — it is the only
    # per-event, per-alumnus role field in the data model, so it is the only
    # signal that can answer "served as a speaker AT AN EVENT this month".
    # (The "Speaker" engagement tag and the guest_speaker_willing flag are
    # alumnus-level and carry no event/date, so they cannot be month-scoped.)
    # We match attendance_status case-insensitively against any value
    # containing "speaker" (e.g. "Speaker", "Guest Speaker") so the KPI is
    # robust to label variants. NOTE TO TEAM: confirm attendance_status is the
    # field event check-in records the speaker role in; today's mock data only
    # writes "Attended", so this KPI reads 0 until speaker statuses are entered.
    guest_speakers_this_month = await session.scalar(
        select(func.count(func.distinct(EventAttendance.alumni_id)))
        .join(Event, Event.event_id == EventAttendance.event_id)
        .join(Alumni, Alumni.alumni_id == EventAttendance.alumni_id)
        .where(
            Event.event_date >= month_start,
            Event.event_date <= month_end,
            EventAttendance.attendance_status.ilike("%speaker%"),
            active,
        )
    )
    # Active alumni flagged as PIFF donors.
    piff_donors = await session.scalar(
        select(func.count())
        .select_from(AlumniProgramEngagement)
        .join(Alumni, Alumni.alumni_id == AlumniProgramEngagement.alumni_id)
        .where(active, AlumniProgramEngagement.piff_donor.is_(True))
    )
    # Active alumni willing to mentor.
    willing_mentors = await session.scalar(
        select(func.count())
        .select_from(AlumniProgramEngagement)
        .join(Alumni, Alumni.alumni_id == AlumniProgramEngagement.alumni_id)
        .where(active, AlumniProgramEngagement.mentor_willing.is_(True))
    )
    # Alumni records edited this CALENDAR month (#606) and this CALENDAR YEAR
    # to date (#645) — the dashboard tile stacks "this month" over "this year"
    # as a running year total, so the two counts MUST come from the same signal
    # and the same population or the tile contradicts itself.
    #
    # Both are deliberately calendar scoped ("this month" = the 1st through now;
    # "this year" = 1 January through now), NOT the rolling 30-day / 12-month
    # windows the older contacted/attended KPIs use; the windows disagree by
    # design and the tile labels say "this month"/"this year" so staff can tell
    # them apart. CONSEQUENCE, and it is CORRECT, not a bug: the year count
    # collapses to near zero every 1 January. Do NOT "fix" that by switching to
    # a trailing 12 months — Amy asked for a year-to-date running total.
    #
    # SIGNAL: alumni.updated_at (TimestampMixin, auto-bumped on every write).
    # This COUNTS DISTINCT ALUMNI RECORDS, NOT CHANGES — ten edits to one person
    # is one record. That property is structural, not something we enforce here:
    # we count rows in the `alumni` table filtered on updated_at, and there is
    # exactly one such row per alumnus. Do NOT rebuild either count on top of
    # audit_logs to get "who changed what" — that table holds one row per
    # changed FIELD, and it also carries action_type='search'/'preview' rows with
    # entity_type='alumni', so a naive count there would be inflated twice over.
    # (Section-only edits — contact, employment, education — do bump
    # alumni.updated_at, because update_alumni touches the Alumni row whenever a
    # section was actually written, and a no-op save doesn't move it.)
    #
    # Bulk imports DO count, DELIBERATELY: the tile measures DATA FRESHNESS
    # ("how much of the record set has changed recently"), not staff effort, so a
    # large CSV import or an automated survey-response apply legitimately
    # dominates the month it lands in. An audit-trail-based "staff hand-edits
    # only" version was considered and explicitly declined.
    #
    # Single aggregate COUNT with a WHERE on updated_at — never fetch rows and
    # count in Python (8,000+ alumni). Same `active` predicate as every other
    # alumni KPI so archived / friend-of-program records can't inflate either.
    # The year bound is 1 Jan 00:00 UTC (all date filters in this app are UTC),
    # and since it is strictly earlier than the month bound over an otherwise
    # identical query, the year count is always >= the month count.
    month_start_ts = datetime.datetime.combine(
        month_start, datetime.time.min, tzinfo=datetime.UTC
    )
    year_start_ts = datetime.datetime.combine(
        today.replace(month=1, day=1), datetime.time.min, tzinfo=datetime.UTC
    )
    alumni_edited_this_month = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, Alumni.updated_at >= month_start_ts)
    )
    alumni_edited_this_year = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, Alumni.updated_at >= year_start_ts)
    )

    cohort = (
        await session.execute(
            select(Alumni.graduation_year, func.count())
            .where(active, Alumni.graduation_year.is_not(None))
            .group_by(Alumni.graduation_year)
            .order_by(Alumni.graduation_year)
        )
    ).all()

    # Top employers over the LAST 5 YEARS (#355): aggregate every employment
    # record that is ACTIVE in that window — the alumnus's current job (always
    # ongoing) UNION any prior role that either started within the last 5 years
    # OR is still current/ongoing (is_current, or no end year). COUNT(DISTINCT
    # alumni_id) over the union so an alum with the same employer in both tables
    # is only counted once. Employer names are trimmed and the same non-employer
    # placeholders excluded so the chart shows real firms.
    five_years_ago = today.year - 5
    current_jobs = (
        select(
            CurrentEmployment.alumni_id.label("alumni_id"),
            func.trim(CurrentEmployment.current_employer).label("employer"),
        )
        .join(Alumni, Alumni.alumni_id == CurrentEmployment.alumni_id)
        .where(
            active,
            CurrentEmployment.current_employer.is_not(None),
            func.trim(CurrentEmployment.current_employer) != "",
        )
    )
    recent_history = (
        select(
            EmploymentHistory.alumni_id.label("alumni_id"),
            func.trim(EmploymentHistory.employer_name).label("employer"),
        )
        .join(Alumni, Alumni.alumni_id == EmploymentHistory.alumni_id)
        .where(
            active,
            EmploymentHistory.employer_name.is_not(None),
            func.trim(EmploymentHistory.employer_name) != "",
            or_(
                EmploymentHistory.start_year >= five_years_ago,
                EmploymentHistory.is_current.is_(True),
                EmploymentHistory.end_year.is_(None),
            ),
        )
    )
    active_employment = current_jobs.union_all(recent_history).subquery()
    _emp_count = func.count(func.distinct(active_employment.c.alumni_id))
    top_employers = (
        await session.execute(
            select(active_employment.c.employer, _emp_count)
            .where(
                func.lower(active_employment.c.employer).not_in(
                    _NON_EMPLOYER_VALUES
                )
            )
            .group_by(active_employment.c.employer)
            .order_by(_emp_count.desc())
            .limit(8)
        )
    ).all()

    # Industry breakdown (#351/#352/#353): count active alumni by their current
    # industry, then reconcile to the CANONICAL finance-industry vocab so every
    # industry is represented (incl. zero-count ones). Two buckets are kept
    # SEPARATE and distinct from each other: "Other" (the catch-all vocab value
    # plus any stored value outside the finance vocab) and "Unknown" (active
    # alumni with NO industry on file). current_employment is unique per alum, so
    # the group counts sum to the alumni-with-industry population and Unknown is
    # simply the remainder of the active total.
    industry_rows = (
        await session.execute(
            select(
                CurrentEmployment.current_industry,
                func.count(func.distinct(Alumni.alumni_id)),
            )
            .join(Alumni, Alumni.alumni_id == CurrentEmployment.alumni_id)
            .where(
                active,
                CurrentEmployment.current_industry.is_not(None),
                func.trim(CurrentEmployment.current_industry) != "",
            )
            .group_by(CurrentEmployment.current_industry)
        )
    ).all()
    industry_counts = {name: 0 for name in _FINANCE_INDUSTRIES}
    other_count = 0
    graduate_student_count = 0
    unknown_explicit = 0
    known_total = 0
    for value, n in industry_rows:
        n = int(n)
        known_total += n
        lowered = (value or "").strip().lower()
        if lowered == "graduate student":
            # Graduate Student (#294) is its own dashboard bar, split out of the
            # "Other" catch-all so it can be counted and drilled into separately.
            graduate_student_count += n
            continue
        if lowered == "unknown":
            # Explicit "Unknown" (#295) is merged INTO the "Unknown" data-gap bar
            # below, alongside alumni with no industry on file — so the dashboard
            # shows a single "Unknown". Not counted under "Other".
            unknown_explicit += n
            continue
        canonical = _FINANCE_BY_LOWER.get(lowered)
        if canonical is not None:
            industry_counts[canonical] += n
        else:
            # Literal "Other", "Military" (#608 — deliberately NOT its own bar;
            # the chart stays about finance sectors) or any value outside the
            # finance vocab.
            other_count += n
    # Unknown = active alumni with no (non-blank) industry on file, PLUS those
    # explicitly marked "Unknown" (#295). ``known_total`` already counts the
    # explicit-unknown rows, so the blank remainder excludes them; add them back.
    unknown_count = max(int(total or 0) - known_total, 0) + unknown_explicit

    # Geographic distribution: alumni by the state they WORK in
    # (current_employment.current_state) — the employer's address is the only
    # address this system holds, and it is what the geography map plots (#287).
    # current_employment is unique per alum, so a plain count is a per-alumnus
    # count and matches app/services/geography.py's by-state aggregation.
    #
    # ⚠️ GROUPED ON THE FOLDED NAME, NOT THE RAW COLUMN (#754). This used to
    # `GROUP BY current_state` verbatim, which had two consequences, both wrong
    # and both invisible in the output:
    #
    #   * "UT" and "Utah" were two different bars competing for the same top-8
    #     slots, so a real state could be pushed off the list by its own
    #     alternate spelling;
    #   * non-US regions ("Ontario", "London") ranked as states.
    #
    # `us_state_full_name_expr` folds both spellings to "Utah" and yields NULL
    # for anything that is not one of the 50 states + DC — the SAME expression
    # the "Across N states" KPI below counts, so the tile and this drill-down
    # can never disagree about what a state is. The LIMIT is applied AFTER the
    # fold (it is a GROUP BY key, not a post-filter), which is the whole point:
    # ranking raw spellings and then truncating to 8 is what produced the wrong
    # list. Non-US alumni are represented by the country KPI, not here.
    #
    # ONE expression object, built once and reused by all three geography
    # queries in this handler (this breakdown, the "N states" count, and the
    # "M countries" count's abroad test). Not a style choice: the tile, its
    # drill-down and its complement have to share a single definition of "a
    # state", and two copies of a call is exactly how they would drift apart.
    _state_name = us_state_full_name_expr(CurrentEmployment.current_state)
    by_state = (
        await session.execute(
            select(_state_name.label("state"), func.count())
            # Explicit: the leading column is now a CASE, so the FROM can no
            # longer be inferred from it the way a bare column did.
            .select_from(CurrentEmployment)
            .join(Alumni, Alumni.alumni_id == CurrentEmployment.alumni_id)
            .where(active, _state_name.is_not(None))
            .group_by(_state_name)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    # ⚠️ LAST OF THE `session.scalar` CALLS ON PURPOSE. The dashboard tests stub
    # `scalar` with a POSITIONAL list and index into `scalar_args`, so inserting
    # a query anywhere above shifts every later value and fails eleven unrelated
    # assertions. Ordering is irrelevant to the result — these counts are
    # independent — so the new one goes on the end where it costs nothing. If
    # that stub is ever made order-independent, this can move next to the other
    # employer counts where it reads better.
    # DISTINCT COMPANIES (#dashboard KPI, 2026-08-20). How many different firms
    # the active alumni population currently works for.
    #
    # ⚠️ COUNTED ON THE SAME TERMS AS THE TOP-EMPLOYERS CHART, deliberately.
    # `current_employer` is free text with no write validation, so:
    #
    #   * names are folded with lower(trim(...)) before DISTINCT — otherwise
    #     "Goldman Sachs", "goldman sachs" and a trailing space are three
    #     companies, and the number is quietly inflated by data entry;
    #   * `_NON_EMPLOYER_VALUES` ("unknown", "n/a", "none", "graduate student")
    #     are excluded, because the chart already refuses to rank them as firms
    #     and a KPI that counted them would disagree with the panel directly
    #     underneath it. A count that does not match its own drill-down is the
    #     bug class this file keeps getting bitten by.
    #
    # CURRENT employment only, not history: the tile answers "where are our
    # alumni now", which is also what makes it comparable with the employer
    # numbers elsewhere on this page.
    _employer_norm = func.lower(func.trim(CurrentEmployment.current_employer))
    distinct_employers = await session.scalar(
        select(func.count(func.distinct(_employer_norm)))
        .select_from(CurrentEmployment)
        .join(Alumni, Alumni.alumni_id == CurrentEmployment.alumni_id)
        .where(
            active,
            CurrentEmployment.current_employer.is_not(None),
            func.trim(CurrentEmployment.current_employer) != "",
            _employer_norm.not_in(_NON_EMPLOYER_VALUES),
        )
    )

    # ...and how many STATES those companies are in — the sub-line under the
    # Companies tile ("Across N states and M countries"), mirroring the
    # industries line under Total alumni.
    #
    # Same address as the geography map and the `by_state` breakdown above: the
    # employer's, which is the only address this system holds (#287).
    #
    # ⚠️ #754 — THIS TILE READ "Across 70 states". There are 51 possible values.
    # The old query was `COUNT(DISTINCT lower(trim(current_state)))` with no US
    # restriction, which is two bugs wearing one query:
    #
    #   * lower(trim(...)) folds casing and whitespace and NOTHING ELSE, so "UT"
    #     and "Utah" — both of which the free-text column holds — counted twice;
    #   * nothing said "US state", so "Ontario" and "London" counted as states.
    #
    # `us_state_full_name_expr` fixes both at once: it folds a code OR a full
    # name in any casing to the canonical full name, and yields NULL for
    # anything that is not one of the 50 states + DC. COUNT(DISTINCT ...) skips
    # NULLs, so the number is now structurally incapable of exceeding 51 — no
    # amount of bad data entry can inflate it again. It is the SAME expression
    # `by_state` groups on (the `_state_name` built there), so the count and the
    # list beneath it agree by construction rather than by two authors
    # remembering to match.
    employer_states = await session.scalar(
        select(func.count(func.distinct(_state_name)))
        .select_from(CurrentEmployment)
        .join(Alumni, Alumni.alumni_id == CurrentEmployment.alumni_id)
        .where(active)
    )

    # ...and the other half of that sub-line: how many COUNTRIES the alumni
    # working outside the US are in (#754, Jake's call — "Across N states and M
    # countries"). Excluding international alumni entirely was the alternative
    # and it hides real reach, so both numbers ship.
    #
    # WHO COUNTS AS ABROAD: an alumnus whose work state does not resolve to a US
    # state — the same NULL from the same expression the state count uses. That
    # is the discriminator, NOT the country column, because state is the field
    # that is actually populated. So the two numbers partition the population
    # instead of overlapping: nobody is both a state and a country.
    #
    # WHAT IS COUNTED: DISTINCT upper(trim(current_country)). US spellings are
    # excluded (`US_COUNTRY_ALIASES`, shared with the world map) so a domestic
    # record with a junk state can't make "United States" the 1 in "and 1
    # country" — the states half already speaks for the US.
    #
    # ⚠️ DELIBERATE UNDERCOUNT, AND THE HONEST BEHAVIOUR: an alum abroad whose
    # `current_country` is blank contributes NOTHING to this number. There is no
    # way to name a country we were never told, and inventing an "unknown"
    # bucket would put a number on the tile that is not a country. `current_country`
    # is free text with no vocab and no default, filled only when the intake
    # sheet's "Current country" column or the survey's "Employment country"
    # answer was, so this reads LOW rather than wrong. It also does not fold
    # spellings ("UK" vs "United Kingdom" are two countries) — there is no
    # country crosswalk in this codebase to fold them with, only the US aliases.
    _country_norm = func.upper(func.trim(CurrentEmployment.current_country))
    employer_countries = await session.scalar(
        select(func.count(func.distinct(_country_norm)))
        .select_from(CurrentEmployment)
        .join(Alumni, Alumni.alumni_id == CurrentEmployment.alumni_id)
        .where(
            active,
            _state_name.is_(None),
            CurrentEmployment.current_country.is_not(None),
            func.trim(CurrentEmployment.current_country) != "",
            _country_norm.not_in(_USA_COUNTRY_VALUES),
        )
    )

    return {
        "total_alumni": int(total or 0),
        "archived": int(archived or 0),
        "deceased": int(deceased or 0),
        "missing_email": int(missing_email or 0),
        "missing_employer": int(missing_employer or 0),
        "distinct_employers": int(distinct_employers or 0),
        "employer_states": int(employer_states or 0),
        "employer_countries": int(employer_countries or 0),
        "contacted_this_month": int(contacted_this_month or 0),
        "alumni_edited_this_month": int(alumni_edited_this_month or 0),
        "alumni_edited_this_year": int(alumni_edited_this_year or 0),
        "not_contacted_6mo": int(not_contacted[6] or 0),
        "not_contacted_12mo": int(not_contacted[12] or 0),
        "not_contacted_24mo": int(not_contacted[24] or 0),
        "upcoming_follow_ups": int(upcoming_follow_ups or 0),
        "duplicate_count": int(duplicate_count or 0),
        "attended_event_this_month": int(attended_event_this_month or 0),
        "upcoming_events": int(upcoming_events or 0),
        "events_this_month": int(events_this_month or 0),
        "guest_speakers_this_month": int(guest_speakers_this_month or 0),
        "piff_donors": int(piff_donors or 0),
        "willing_mentors": int(willing_mentors or 0),
        "by_graduation_year": [{"year": r[0], "count": int(r[1])} for r in cohort],
        "top_employers": [
            {"employer": r[0], "count": int(r[1])} for r in top_employers
        ],
        "by_state": [{"state": r[0], "count": int(r[1])} for r in by_state],
        "industry_breakdown": {
            "industries": [
                {"industry": name, "count": industry_counts[name]}
                for name in _FINANCE_INDUSTRIES
            ],
            "other": other_count,
            "unknown": unknown_count,
            "graduate_student": graduate_student_count,
        },
    }


@router.get("/birthdays", response_model=list[BirthdayRow])
async def birthdays(
    actor: RequireViewAccess, session: SessionDep
) -> list[dict]:
    """Active alumni whose birthday falls in the current calendar month, ordered
    by day-of-month ascending (earliest in the month first). Each row carries
    the alumnus's current/most-recent employer via the same correlated scalar
    subquery the alumni list uses, so the value matches that view exactly.

    Filters on the month component of ``birth_date`` (the year is irrelevant for
    a recurring birthday). Aggregation/derivation happens in PostgreSQL.

    FERPA: the full date of birth (incl. year) is sensitive PII and is NOT needed
    to wish someone a happy birthday — so this view-only endpoint returns only the
    recurring month+day (``birth_month`` / ``birth_day``), never the birth year,
    so view_only users can't harvest full DOBs. The disclosure is audited.

    Returns, e.g.
    [{"id": 7, "first_name": "Jane", "last_name": "Doe",
      "current_employer": "Goldman Sachs", "graduation_year": 2019,
      "birth_month": 6, "birth_day": 3}, ...]."""
    # Alumni-only: exclude "friends of the program" (is_alumni=false) from every
    # alumni KPI so friends never inflate alumni counts (#218 follow-up).
    active = and_(Alumni.archived.is_(False), Alumni.is_alumni.is_(True))
    current_month = datetime.datetime.now(datetime.UTC).date().month

    # Same correlated scalar subquery the alumni list uses to surface the
    # current/most-recent employer (current_employment.current_employer).
    current_employer = (
        select(CurrentEmployment.current_employer)
        .where(CurrentEmployment.alumni_id == Alumni.alumni_id)
        .limit(1)
        .scalar_subquery()
    )

    rows = (
        await session.execute(
            select(Alumni, current_employer)
            .where(
                active,
                Alumni.birth_date.is_not(None),
                extract("month", Alumni.birth_date) == current_month,
            )
            .order_by(
                extract("day", Alumni.birth_date).asc(),
                Alumni.last_name.asc(),
                Alumni.alumni_id.asc(),
            )
        )
    ).all()
    await _audit_view(
        session, actor, action_type="view", entity_type="dashboard:birthdays"
    )
    return [
        {
            "id": a.alumni_id,
            "first_name": a.first_name,
            "last_name": a.last_name,
            "current_employer": employer,
            "graduation_year": a.graduation_year,
            # Recurring month+day only — never the birth year (FERPA: full DOB).
            "birth_month": a.birth_date.month if a.birth_date else None,
            "birth_day": a.birth_date.day if a.birth_date else None,
        }
        for a, employer in rows
    ]


@router.get("/event-participation", response_model=list[EventParticipationRow])
async def event_participation(
    _: RequireViewAccess, session: SessionDep
) -> list[dict]:
    """Per-event participation for the ~last 12 months (past/current events —
    these are what have "participation"). One row per event with its attendee
    count, aggregated in PostgreSQL (LEFT JOIN so an event with 0 attendees
    still appears), ordered chronologically and capped at 10 so it fits the
    dashboard panel.

    Returns oldest→newest, e.g.
    [{"event_id": 12, "event_name": "Spring Mixer", "event_type": "Networking",
      "event_date": "2026-05-29", "participant_count": 34}, ...]."""
    today = datetime.datetime.now(datetime.UTC).date()
    # First day of the current month, then step back 11 months → the oldest
    # month in the 12-month window (current month inclusive).
    current_month_start = today.replace(day=1)
    year = current_month_start.year
    month = current_month_start.month - 11
    # Normalize the month/year after subtracting 11 (handles year rollover).
    year += (month - 1) // 12
    month = (month - 1) % 12 + 1
    window_start = datetime.date(year, month, 1)

    # One row per event with its attendee count. LEFT JOIN keeps events with
    # zero recorded attendees. Grouping by event_id sidesteps any date_trunc
    # GROUP BY pitfalls. Take the 10 most recent events in the window, then
    # re-sort oldest→newest so the panel reads left-to-right in time.
    rows = (
        await session.execute(
            select(
                Event.event_id,
                Event.event_name,
                Event.event_type,
                Event.event_date,
                func.count(EventAttendance.event_attendance_id).label("participant_count"),
            )
            .select_from(Event)
            .outerjoin(
                EventAttendance, EventAttendance.event_id == Event.event_id
            )
            .where(Event.event_date >= window_start, Event.event_date <= today)
            .group_by(
                Event.event_id,
                Event.event_name,
                Event.event_type,
                Event.event_date,
            )
            .order_by(Event.event_date.desc().nullslast(), Event.event_id.desc())
            .limit(10)
        )
    ).all()

    # rows are newest-first (for the LIMIT); reverse to oldest→newest for the UI.
    return [
        {
            "event_id": r.event_id,
            "event_name": r.event_name,
            "event_type": r.event_type,
            "event_date": r.event_date.isoformat() if r.event_date else None,
            "participant_count": int(r.participant_count),
        }
        for r in reversed(rows)
    ]


@router.get("/activity", response_model=ActivityFeed)
async def activity_feed(
    actor: RequireReportsAdvanced,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(
            description=(
                "Case-insensitive substring matched against the alumnus's "
                "first / last / preferred name OR the interaction type."
            )
        ),
    ] = None,
    type: Annotated[
        str | None,
        Query(description="Interaction type (case-insensitive exact)."),
    ] = None,
    date_from: Annotated[
        datetime.date | None,
        Query(description="Only interactions on/after this date (inclusive)."),
    ] = None,
    date_to: Annotated[
        datetime.date | None,
        Query(description="Only interactions on/before this date (inclusive)."),
    ] = None,
    sort: Annotated[
        str,
        Query(description="Sort order: recent (newest first) | oldest."),
    ] = "recent",
    mine: Annotated[
        bool,
        Query(
            description=(
                "When true, restrict to interactions logged by the current "
                "authenticated user (the actor / 'interacted by me')."
            )
        ),
    ] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """Paginated all-time interaction feed (newest first) — the full version
    of the dashboard's old recent-activity panel, now on its own page. Supports
    optional server-side filtering by free-text search, interaction type, and an
    inclusive date range; all filtering happens in PostgreSQL.

    FERPA: this is a searchable feed of individual-alumni CRM interactions, so it
    is gated to full_access (view_only gets 403) and the search/disclosure is
    audited (actor + that a search happened, never the returned rows)."""
    # Build the shared filter predicates once so the count and the page agree.
    conditions = []
    if q and q.strip():
        like = f"%{escape_like(q.strip())}%"
        conditions.append(
            or_(
                Alumni.first_name.ilike(like, escape="\\"),
                Alumni.last_name.ilike(like, escape="\\"),
                Alumni.preferred_first_name.ilike(like, escape="\\"),
                Interaction.interaction_type.ilike(like, escape="\\"),
            )
        )
    if type and type.strip():
        conditions.append(
            Interaction.interaction_type.ilike(
                escape_like(type.strip()), escape="\\"
            )
        )
    # A bare date covers the whole day: expand to full-day UTC bounds so
    # same-day interactions are included regardless of their time.
    if date_from is not None:
        conditions.append(
            Interaction.interaction_date_time
            >= datetime.datetime.combine(
                date_from, datetime.time.min, tzinfo=datetime.UTC
            )
        )
    if date_to is not None:
        conditions.append(
            Interaction.interaction_date_time
            <= datetime.datetime.combine(
                date_to, datetime.time.max, tzinfo=datetime.UTC
            )
        )
    # "Interacted by me": only rows whose actor is the current user. Applied as
    # just another predicate so it composes with q / type / date range / sort,
    # and is reflected in both the count and the page (shared ``conditions``).
    if mine:
        conditions.append(Interaction.user_id == actor.user_id)

    total = await session.scalar(
        select(func.count())
        .select_from(Interaction)
        .join(Alumni, Alumni.alumni_id == Interaction.alumni_id)
        .where(*conditions)
    )
    rows = (
        await session.execute(
            select(Interaction, Alumni, User)
            .join(Alumni, Alumni.alumni_id == Interaction.alumni_id)
            .outerjoin(User, User.user_id == Interaction.user_id)
            .where(*conditions)
            .order_by(
                Interaction.interaction_date_time.asc().nullslast()
                if sort == "oldest"
                else Interaction.interaction_date_time.desc().nullslast(),
                Interaction.interaction_id.asc()
                if sort == "oldest"
                else Interaction.interaction_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
    ).all()
    # Distinct non-null interaction types (sorted) seed the toolbar's type
    # dropdown — independent of the active filters so every option stays
    # reachable.
    type_rows = (
        await session.execute(
            select(Interaction.interaction_type)
            .where(Interaction.interaction_type.is_not(None))
            .distinct()
            .order_by(Interaction.interaction_type)
        )
    ).all()
    await _audit_view(
        session,
        actor,
        action_type="search",
        entity_type="dashboard:activity",
        field_name="interaction_feed",
    )
    return {
        "items": [_serialize_interaction(i, a, u) for i, a, u in rows],
        "types": [r[0] for r in type_rows],
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/data-quality", response_model=DataQuality)
async def data_quality(_: RequireReportsAdvanced, session: SessionDep) -> dict:
    """The data-quality alert counts (same predicates as the summary KPIs),
    for the dedicated data-quality page.

    Full-access only (matches the sidebar gate): view_only users get 403, like
    the cross-alumni Tasks list."""
    # Alumni-only: exclude "friends of the program" (is_alumni=false) from every
    # alumni KPI so friends never inflate alumni counts (#218 follow-up).
    active = and_(Alumni.archived.is_(False), Alumni.is_alumni.is_(True))
    total = await session.scalar(
        select(func.count()).select_from(Alumni).where(active)
    )
    missing_email = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_email_exists())
    )
    missing_employer = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_employer_exists())
    )
    missing_phone = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(active, ~_has_phone_exists())
    )
    # "Complete" = an active alumnus with all three tracked contact/career fields
    # on file (email AND phone AND current employer).
    complete_alumni = await session.scalar(
        select(func.count())
        .select_from(Alumni)
        .where(
            active,
            _has_email_exists(),
            _has_phone_exists(),
            _has_employer_exists(),
        )
    )
    duplicate_count = await session.scalar(
        text("SELECT count(*) FROM duplicate_candidates")
    )
    return {
        "total_alumni": int(total or 0),
        "complete_alumni": int(complete_alumni or 0),
        "missing_email": int(missing_email or 0),
        "missing_employer": int(missing_employer or 0),
        "missing_phone": int(missing_phone or 0),
        "duplicate_count": int(duplicate_count or 0),
    }


@router.get("/contacted-this-month", response_model=list[InteractionActivity])
async def contacted_this_month_list(
    actor: RequireReportsAdvanced, session: SessionDep
) -> list[dict]:
    """The alumni behind the "Contacted this month" KPI — one row per distinct
    alumnus contacted in the last 30 days, carrying their most recent
    interaction in the window.

    Applies the same active-alumni filter as the KPI count
    (archived=false AND is_alumni=true) so archived / friend-of-program records
    never leak into the list and the row count reconciles with the tile (#179).

    FERPA: exposes individual alumni + CRM interaction data, so it is gated to
    full_access (view_only gets 403) and the disclosure is audited; only the
    aggregate count KPI on /summary stays view-accessible."""
    # Alumni-only: mirror the /summary KPI's predicate so the drill-down list
    # reconciles with the "Contacted this month" tile count (#179).
    active = and_(Alumni.archived.is_(False), Alumni.is_alumni.is_(True))
    now = datetime.datetime.now(datetime.UTC)
    month_ago = now - datetime.timedelta(days=30)
    latest = (
        select(Interaction)
        .where(Interaction.interaction_date_time >= month_ago)
        .distinct(Interaction.alumni_id)
        .order_by(
            Interaction.alumni_id, Interaction.interaction_date_time.desc()
        )
        .subquery()
    )
    li = aliased(Interaction, latest)
    rows = (
        await session.execute(
            select(li, Alumni, User)
            .join(Alumni, Alumni.alumni_id == li.alumni_id)
            .outerjoin(User, User.user_id == li.user_id)
            .where(active)
            .order_by(li.interaction_date_time.desc())
            .limit(200)
        )
    ).all()
    await _audit_view(
        session,
        actor,
        action_type="view",
        entity_type="dashboard:contacted-this-month",
    )
    # Same interaction-row shape (incl. actor "by"/"by_user_id") as the activity
    # feed — reuse the shared serializer so the two stay in lockstep.
    return [_serialize_interaction(i, a, u) for i, a, u in rows]


@router.get("/follow-ups", response_model=list[FollowUpRow])
async def upcoming_follow_ups_list(
    actor: RequireReportsAdvanced, session: SessionDep
) -> list[dict]:
    """The open tasks behind the "Upcoming follow-ups" KPI (incomplete, due
    today or later), soonest due first — same predicate as the KPI count.

    FERPA: exposes individual alumni + their assigned follow-up tasks, so it is
    gated to full_access (view_only gets 403) and the disclosure is audited; only
    the aggregate count KPI on /summary stays view-accessible."""
    today = datetime.datetime.now(datetime.UTC).date()
    rows = (
        await session.execute(
            select(FollowUpTask, Alumni, User)
            .join(Alumni, Alumni.alumni_id == FollowUpTask.alumni_id)
            .outerjoin(User, User.user_id == FollowUpTask.assigned_to_user_id)
            .where(
                FollowUpTask.completed.is_(False),
                FollowUpTask.due_date >= today,
            )
            .order_by(FollowUpTask.due_date.asc(), FollowUpTask.follow_up_task_id)
            .limit(200)
        )
    ).all()
    await _audit_view(
        session, actor, action_type="view", entity_type="dashboard:follow-ups"
    )
    return [
        {
            "task_id": t.follow_up_task_id,
            "alumni_id": t.alumni_id,
            "alumni_name": _full_name(a.first_name, a.last_name, None, a.preferred_first_name)
            or f"Alumni #{a.alumni_id}",
            "title": t.task_title,
            "due_date": t.due_date.isoformat() if t.due_date else None,
            "assigned_to": (
                _full_name(u.first_name, u.last_name, u.email) if u else None
            ),
        }
        for t, a, u in rows
    ]

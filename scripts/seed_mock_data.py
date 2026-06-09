"""Seed (or remove) easily-removable MOCK alumni data for development.

Every mock row is tagged with a single ``data_sources`` row named ``MOCK_DATA``,
so cleanup is one cascade delete — no risk of touching real records once the
department's data lands.

Run from the repo root with the project venv:

    .venv/Scripts/python -m scripts.seed_mock_data            # (re)seed mock data
    .venv/Scripts/python -m scripts.seed_mock_data --remove   # delete all mock data

``seed`` is idempotent: it removes any existing MOCK_DATA first, then re-inserts,
so re-running never creates duplicates.

NOTE: dev and prod share one Supabase database. This data is clearly tagged and
trivially removable, but it IS visible to both deployments until removed.
"""

import argparse
import asyncio
import datetime

from sqlalchemy import delete, select

from app.core.database import SessionLocal
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.crm import Attachment, FollowUpTask, Interaction, Survey
from app.models.data_source import DataSource
from app.models.employment import (
    CurrentEmployment,
    EducationHistory,
    EmploymentHistory,
)
from app.models.engagement import (
    AlumniEngagement,
    AlumniProgramEngagement,
    FinanceSocietyLeadership,
)
from app.models.event import Event, EventAttendance
from app.models.tags import AlumniStatusLabel, AlumniTag, StatusLabel, Tag
from app.models.user import User

# Event dates are RELATIVE to "today" (set in seed_mock) so the dashboard's
# time-windowed KPIs always have data no matter when this is run:
#   * two events in the last 30 days  -> "Attended an event this month"
#   * two events today-or-later       -> "Upcoming events"
#   * one older event                 -> history
MOCK_EVENTS = [
    {"name": "Spring NetTrek", "type": "Net Trek", "days": -10, "location": "New York, NY"},
    {"name": "Women in Finance Mixer", "type": "Club Event", "days": -24, "location": "Provo, UT"},
    {"name": "Alumni Reunion BBQ", "type": "BBQ", "days": -120, "location": "Provo, UT"},
    {"name": "Recruiting Night — Goldman Sachs", "type": "Recruiting Event", "days": 18, "location": "Virtual"},
    {"name": "Finance Conference", "type": "Finance Conference", "days": 45, "location": "Salt Lake City, UT"},
]

MOCK_SOURCE_NAME = "MOCK_DATA"

# Synthetic BYU Finance alumni. Obviously-fake, varied for exercising filters
# (grad-year range, deceased, archived, search). Employer/location live in
# related tables (not modeled yet), so these cover the alumni core only.
MOCK_ALUMNI: list[dict] = [
    {"byu_id": "00-100-001", "net_id": "jdoe1", "first_name": "James", "last_name": "Doe", "gender": "M", "graduation_year": 2012, "finance_program_year": 2011, "linkedin_url": "https://linkedin.com/in/mock-jdoe", "notes": "[MOCK] Investment banking track."},
    {"byu_id": "00-100-002", "net_id": "alee2", "first_name": "Ava", "last_name": "Lee", "preferred_first_name": "Avy", "gender": "F", "graduation_year": 2015, "finance_program_year": 2014, "notes": "[MOCK] PE associate."},
    {"byu_id": "00-100-003", "net_id": "mchen3", "first_name": "Marcus", "middle_name": "T", "last_name": "Chen", "gender": "M", "graduation_year": 2018, "finance_program_year": 2017},
    {"byu_id": "00-100-004", "net_id": "srivera4", "first_name": "Sofia", "last_name": "Rivera", "gender": "F", "graduation_year": 2020, "finance_program_year": 2019, "linkedin_url": "https://linkedin.com/in/mock-srivera"},
    {"byu_id": "00-100-005", "net_id": "bpatel5", "first_name": "Benjamin", "last_name": "Patel", "gender": "M", "graduation_year": 2010, "finance_program_year": 2009, "graduate_degree": "MBA"},
    {"byu_id": "00-100-006", "net_id": "enguyen6", "first_name": "Emily", "last_name": "Nguyen", "gender": "F", "graduation_year": 2022, "finance_program_year": 2021, "notes": "[MOCK] Corporate finance."},
    {"byu_id": "00-100-007", "net_id": "dkim7", "first_name": "Daniel", "last_name": "Kim", "gender": "M", "graduation_year": 2016, "finance_program_year": 2015},
    {"byu_id": "00-100-008", "net_id": "ojohnson8", "first_name": "Olivia", "last_name": "Johnson", "gender": "F", "graduation_year": 2019, "finance_program_year": 2018, "linkedin_url": "https://linkedin.com/in/mock-ojohnson"},
    {"byu_id": "00-100-009", "net_id": "lgarcia9", "first_name": "Lucas", "last_name": "Garcia", "gender": "M", "graduation_year": 2013, "finance_program_year": 2012},
    {"byu_id": "00-100-010", "net_id": "hwhite10", "first_name": "Hannah", "last_name": "White", "gender": "F", "graduation_year": 2021, "finance_program_year": 2020, "notes": "[MOCK] Wealth management."},
    {"byu_id": "00-100-011", "net_id": "nwright11", "first_name": "Nathan", "last_name": "Wright", "gender": "M", "graduation_year": 2009, "finance_program_year": 2008, "deceased": True, "notes": "[MOCK] Deceased — for filter testing."},
    {"byu_id": "00-100-012", "net_id": "gmartin12", "first_name": "Grace", "last_name": "Martin", "gender": "F", "graduation_year": 2017, "finance_program_year": 2016},
    {"byu_id": "00-100-013", "net_id": "ethomas13", "first_name": "Ethan", "last_name": "Thomas", "gender": "M", "graduation_year": 2023, "finance_program_year": 2022, "linkedin_url": "https://linkedin.com/in/mock-ethomas"},
    {"byu_id": "00-100-014", "net_id": "iclark14", "first_name": "Isabella", "last_name": "Clark", "gender": "F", "graduation_year": 2014, "finance_program_year": 2013, "archived": True, "notes": "[MOCK] Archived — for soft-delete testing."},
    {"byu_id": "00-100-015", "net_id": "alewis15", "first_name": "Andrew", "last_name": "Lewis", "gender": "M", "graduation_year": 2011, "finance_program_year": 2010, "graduate_degree": "MAcc"},
    {"byu_id": "00-100-016", "net_id": "shall16", "first_name": "Sophia", "last_name": "Hall", "gender": "F", "graduation_year": 2020, "finance_program_year": 2019, "notes": "[MOCK] Equity research."},
]


# Canonical reference labels used by the chips/filters (from Features.md §4b).
# These live in shared lookup tables (no source_id), so they're get-or-created
# idempotently and intentionally left in place by --remove.
MOCK_TAGS = ["Mentor", "Highly Engaged", "Speaker", "Recruiter", "Donor"]
MOCK_STATUS_LABELS = ["Inactive", "Deceased", "Lost Contact", "Do Not Contact"]

# Rich per-alumni detail keyed by net_id. James Doe (jdoe1) is fully populated to
# mirror the Figma profile demo; a few others get lighter data so every tab and
# the alumni list show realistic variety. Anything omitted renders as an
# "awaiting data" empty state in the UI.
MOCK_DETAIL: dict[str, dict] = {
    "jdoe1": {
        "contact": {
            "personal_email": "james.doe@example.com",
            "work_email": "jdoe@goldmansachs.com",
            "phone": "+1 (212) 555-0142",
            "address_line_1": "200 West St",
            "city": "New York",
            "state": "NY",
            "zip": "10282",
            "country": "USA",
            "region": "Northeast",
        },
        "career": {
            "current_employer": "Goldman Sachs",
            "current_title": "Vice President",
            "current_industry": "Investment Banking",
            "current_industry_secondary": "Financial Services",
            "current_city": "New York",
            "current_state": "NY",
            "current_country": "USA",
            "seniority_level": "Vice President",
            "last_verified_at": datetime.datetime(2026, 4, 1, tzinfo=datetime.UTC),
        },
        "employment_history": [
            {"employer_name": "Goldman Sachs", "employment_title": "Vice President", "employment_industry": "Investment Banking", "city": "New York", "state": "NY", "start_year": 2018, "end_year": None, "is_current": True},
            {"employer_name": "J.P. Morgan", "employment_title": "Analyst", "employment_industry": "Investment Banking", "city": "New York", "state": "NY", "start_year": 2012, "end_year": 2018, "is_current": False},
            {"employer_name": "Deloitte", "employment_title": "Summer Analyst", "employment_industry": "Consulting", "city": "New York", "state": "NY", "start_year": 2011, "end_year": 2011, "is_current": False},
        ],
        "education": [
            {"university": "Brigham Young University", "college": "Marriott School of Business", "department": "Finance", "degree": "BS", "major": "Finance", "degree_status": "Completed", "degree_year": 2012},
        ],
        "leadership": [
            {"leadership_role": "Finance Society President", "role_year": 2011},
            {"leadership_role": "Investment Banking Club VP", "role_year": 2010},
        ],
        "program": {
            "nettrek_host_willing": True,
            "finance_conference_willing": True,
            "mentor_willing": True,
            "guest_speaker_willing": True,
            "hired_finance_intern": True,
            "cfa_designation": True,
            "piff_donor": True,
            "piff_donor_amount": 5000,
            "engagement_notes": "Hosts NetTrek in NYC; active IB-track mentor.",
        },
        "engagement_notes": [
            {"engagement_interest_type": "Mentorship", "engagement_notes": "Willing to mentor students pursuing investment banking."},
        ],
        "tags": ["Mentor", "Highly Engaged"],
        "status_labels": [],
        "surveys": [
            {"survey_year": 2025, "completed": True, "survey_status": "Completed", "completed_at": datetime.datetime(2025, 10, 3, tzinfo=datetime.UTC)},
            {"survey_year": 2026, "completed": False, "survey_status": "Sent", "survey_due_date": datetime.date(2026, 9, 30)},
        ],
        "interactions": [
            {"interaction_type": "Meeting", "days_ago": 14, "interaction_notes": "Coffee in Manhattan. Open to hosting two students for NetTrek."},
            {"interaction_type": "Phone Call", "days_ago": 45, "interaction_notes": "Caught up on recruiting pipeline; will refer a candidate."},
            {"interaction_type": "Networking", "days_ago": 120, "interaction_notes": "Reconnected at the Finance Conference."},
        ],
        "tasks": [
            {"task_title": "Schedule Q3 mentorship call", "due_in_days": 21, "completed": False, "task_notes": "Pair with two IB-track juniors."},
            {"task_title": "Confirm spring recruiting visit", "due_in_days": 7, "completed": False, "task_notes": "NYC office, two-day trip."},
            {"task_title": "Review mentee resumes", "due_in_days": 14, "completed": False, "task_notes": "Three students in the IB track."},
            {"task_title": "Send thank-you note", "due_in_days": -3, "completed": False, "task_notes": "After the Finance Conference panel."},
            {"task_title": "Plan alumni panel session", "due_in_days": 30, "completed": False},
            {"task_title": "Send NetTrek host packet", "due_in_days": -5, "completed": True, "task_notes": "Logistics for spring trek."},
        ],
        "attachments": [
            {"file_name": "James_Doe_Resume.pdf", "file_type": "application/pdf", "attachment_notes": "2025 resume"},
            {"file_name": "James_Doe_Headshot.jpg", "file_type": "image/jpeg", "attachment_notes": None},
        ],
    },
    "alee2": {
        "contact": {"personal_email": "ava.lee@example.com", "phone": "+1 (415) 555-0199", "city": "San Francisco", "state": "CA", "country": "USA", "region": "West"},
        "career": {"current_employer": "Bain Capital", "current_title": "Associate", "current_industry": "Private Equity", "current_city": "San Francisco", "current_state": "CA", "seniority_level": "Associate"},
        "program": {"piff_donor": True, "piff_donor_amount": 1500, "mentor_willing": True},
        "tags": ["Highly Engaged", "Donor"],
        "interactions": [
            {"interaction_type": "Event Follow-Up", "days_ago": 30, "interaction_notes": "Followed up after Women in Finance mixer."},
        ],
    },
    "srivera4": {
        "contact": {"personal_email": "sofia.rivera@example.com", "city": "Chicago", "state": "IL", "country": "USA", "region": "Midwest"},
        "career": {"current_employer": "Northern Trust", "current_title": "Senior Analyst", "current_industry": "Asset Management", "current_city": "Chicago", "current_state": "IL", "seniority_level": "Senior Analyst"},
        "program": {"mentor_willing": True},
        "tags": ["Recruiter", "Mentor"],
    },
    "enguyen6": {
        "contact": {"city": "San Jose", "state": "CA", "country": "USA", "region": "West"},
        "career": {"current_employer": "Adobe", "current_title": "Corporate Finance Analyst", "current_industry": "Technology", "current_city": "San Jose", "current_state": "CA", "seniority_level": "Analyst"},
    },
    # BYU-heavy Utah hub (Provo / Salt Lake City / Lehi) plus financial centers,
    # so the geography dashboard has a realistic, populated distribution.
    "mchen3": {
        "contact": {"city": "Provo", "state": "UT", "country": "USA", "region": "Mountain West"},
        "career": {"current_employer": "Qualtrics", "current_title": "Finance Manager", "current_industry": "Technology", "current_city": "Provo", "current_state": "UT", "seniority_level": "Manager"},
        "program": {"mentor_willing": True},
        "tags": ["Mentor"],
    },
    "bpatel5": {
        "contact": {"city": "Salt Lake City", "state": "UT", "country": "USA", "region": "Mountain West"},
        "career": {"current_employer": "Goldman Sachs", "current_title": "Associate", "current_industry": "Investment Banking", "current_city": "Salt Lake City", "current_state": "UT", "seniority_level": "Associate"},
    },
    "dkim7": {
        "contact": {"city": "Lehi", "state": "UT", "country": "USA", "region": "Mountain West"},
        "career": {"current_employer": "Adobe", "current_title": "FP&A Analyst", "current_industry": "Technology", "current_city": "Lehi", "current_state": "UT", "seniority_level": "Analyst"},
        "program": {"mentor_willing": True},
        "tags": ["Mentor"],
    },
    "ojohnson8": {
        "contact": {"city": "Provo", "state": "UT", "country": "USA", "region": "Mountain West"},
        "career": {"current_employer": "Fidelity", "current_title": "Wealth Advisor", "current_industry": "Asset Management", "current_city": "Provo", "current_state": "UT", "seniority_level": "Advisor"},
        "program": {"piff_donor": True, "piff_donor_amount": 2500, "mentor_willing": True},
        "tags": ["Donor", "Mentor"],
    },
    "lgarcia9": {
        "contact": {"city": "Dallas", "state": "TX", "country": "USA", "region": "South"},
        "career": {"current_employer": "JPMorgan", "current_title": "Vice President", "current_industry": "Investment Banking", "current_city": "Dallas", "current_state": "TX", "seniority_level": "Vice President"},
    },
    "hwhite10": {
        "contact": {"city": "New York", "state": "NY", "country": "USA", "region": "Northeast"},
        "career": {"current_employer": "Morgan Stanley", "current_title": "Wealth Manager", "current_industry": "Wealth Management", "current_city": "New York", "current_state": "NY", "seniority_level": "Vice President"},
        "program": {"piff_donor": True, "piff_donor_amount": 1000, "mentor_willing": True},
        "tags": ["Donor", "Mentor"],
    },
    "gmartin12": {
        "contact": {"city": "Salt Lake City", "state": "UT", "country": "USA", "region": "Mountain West"},
        "career": {"current_employer": "Goldman Sachs", "current_title": "Analyst", "current_industry": "Investment Banking", "current_city": "Salt Lake City", "current_state": "UT", "seniority_level": "Analyst"},
    },
    "ethomas13": {
        "contact": {"city": "Austin", "state": "TX", "country": "USA", "region": "South"},
        "career": {"current_employer": "Dell", "current_title": "Treasury Analyst", "current_industry": "Technology", "current_city": "Austin", "current_state": "TX", "seniority_level": "Analyst"},
    },
    "alewis15": {
        "contact": {"city": "Boston", "state": "MA", "country": "USA", "region": "Northeast"},
        "career": {"current_employer": "Fidelity", "current_title": "Portfolio Analyst", "current_industry": "Asset Management", "current_city": "Boston", "current_state": "MA", "seniority_level": "Analyst"},
    },
    "shall16": {
        "contact": {"city": "San Francisco", "state": "CA", "country": "USA", "region": "West"},
        "career": {"current_employer": "BlackRock", "current_title": "Equity Research Associate", "current_industry": "Asset Management", "current_city": "San Francisco", "current_state": "CA", "seniority_level": "Associate"},
    },
    "nwright11": {"status_labels": ["Deceased"]},
    "iclark14": {"status_labels": ["Lost Contact"]},
}


async def _get_source(session) -> DataSource | None:
    return await session.scalar(
        select(DataSource).where(DataSource.source_name == MOCK_SOURCE_NAME)
    )


async def _get_or_create_lookup(session, model, name_attr: str, names: list[str]) -> dict[str, int]:
    """Idempotently ensure the named lookup rows exist; return name -> id."""
    column = getattr(model, name_attr)
    existing = {
        getattr(row, name_attr): row
        for row in (await session.scalars(select(model).where(column.in_(names)))).all()
    }
    for name in names:
        if name not in existing:
            row = model(**{name_attr: name})
            session.add(row)
            existing[name] = row
    await session.flush()
    return {name: getattr(row, name_attr.replace("_name", "_id")) for name, row in existing.items()}


async def _seed_details(session, alumni_by_net_id: dict[str, int], actor_user_id: int | None) -> None:
    """Insert the rich per-alumni related rows defined in MOCK_DETAIL."""
    tag_ids = await _get_or_create_lookup(session, Tag, "tag_name", MOCK_TAGS)
    status_ids = await _get_or_create_lookup(
        session, StatusLabel, "status_label_name", MOCK_STATUS_LABELS
    )
    now = datetime.datetime.now(datetime.UTC)

    for net_id, detail in MOCK_DETAIL.items():
        aid = alumni_by_net_id.get(net_id)
        if aid is None:
            continue
        if c := detail.get("contact"):
            session.add(AlumniContactInfo(alumni_id=aid, **c))
        if career := detail.get("career"):
            session.add(CurrentEmployment(alumni_id=aid, **career))
        for eh in detail.get("employment_history", []):
            session.add(EmploymentHistory(alumni_id=aid, **eh))
        for ed in detail.get("education", []):
            session.add(EducationHistory(alumni_id=aid, **ed))
        for le in detail.get("leadership", []):
            session.add(FinanceSocietyLeadership(alumni_id=aid, **le))
        if program := detail.get("program"):
            session.add(AlumniProgramEngagement(alumni_id=aid, **program))
        for en in detail.get("engagement_notes", []):
            session.add(AlumniEngagement(alumni_id=aid, **en))
        for tag_name in detail.get("tags", []):
            session.add(AlumniTag(alumni_id=aid, tag_id=tag_ids[tag_name]))
        for label in detail.get("status_labels", []):
            session.add(AlumniStatusLabel(alumni_id=aid, status_label_id=status_ids[label]))
        for sv in detail.get("surveys", []):
            session.add(Survey(alumni_id=aid, **sv))
        for ix in detail.get("interactions", []):
            data = dict(ix)
            days = data.pop("days_ago", None)
            if days is not None:
                data["interaction_date_time"] = now - datetime.timedelta(days=days)
            session.add(Interaction(alumni_id=aid, user_id=actor_user_id, **data))
        for tk in detail.get("tasks", []):
            data = dict(tk)
            due = data.pop("due_in_days", None)
            if due is not None:
                data["due_date"] = (now + datetime.timedelta(days=due)).date()
            if data.get("completed"):
                data.setdefault("completed_at", now)
            session.add(FollowUpTask(alumni_id=aid, assigned_to_user_id=actor_user_id, **data))
        for at in detail.get("attachments", []):
            session.add(
                Attachment(
                    alumni_id=aid,
                    uploaded_by_user_id=actor_user_id,
                    storage_key=f"mock/{net_id}/{at['file_name']}",
                    **at,
                )
            )


async def remove_mock(session) -> int:
    """Delete all mock alumni/events and the MOCK_DATA source. Returns rows removed."""
    # Mock events are tagged in event_notes; FK cascade removes their attendance.
    await session.execute(delete(Event).where(Event.event_notes.like("%[MOCK]%")))
    source = await _get_source(session)
    if source is None:
        await session.commit()
        return 0
    result = await session.execute(
        delete(Alumni).where(Alumni.source_id == source.source_id)
    )
    await session.delete(source)
    await session.commit()
    return result.rowcount or 0


async def seed_mock(session) -> int:
    """Remove any existing mock data, then insert a fresh set. Idempotent."""
    await remove_mock(session)
    source = DataSource(
        source_name=MOCK_SOURCE_NAME,
        source_type="mock",
        source_description=(
            "Synthetic mock alumni for development. Safe to delete: "
            "`python -m scripts.seed_mock_data --remove`."
        ),
    )
    session.add(source)
    await session.flush()  # assign source_id
    alumni = [Alumni(source_id=source.source_id, **row) for row in MOCK_ALUMNI]
    session.add_all(alumni)
    await session.flush()  # assign alumni_ids
    alumni_ids = [a.alumni_id for a in alumni]
    alumni_by_net_id = {
        row["net_id"]: a.alumni_id
        for row, a in zip(MOCK_ALUMNI, alumni, strict=True)
        if row.get("net_id")
    }

    # Attribute seeded interactions/tasks/attachments to a real provisioned user
    # so the "logged by / assigned to" names render (falls back to None).
    actor_user_id = await session.scalar(
        select(User.user_id).order_by(User.user_id).limit(1)
    )
    await _seed_details(session, alumni_by_net_id, actor_user_id)

    # Guarantee every alumnus has a current employer + industry and a LinkedIn
    # so the alumni list never renders blank cells. Fallbacks are deterministic
    # (indexed) and use the canonical industry vocabulary.
    fallback_careers = [
        ("Deloitte", "Consulting"),
        ("Wells Fargo", "Commercial Banking"),
        ("Charles Schwab", "Wealth Management"),
        ("KPMG", "Valuation & Advisory"),
        ("PIMCO", "Asset Management"),
    ]
    for idx, (row, a) in enumerate(zip(MOCK_ALUMNI, alumni, strict=True)):
        nid = row.get("net_id")
        if not MOCK_DETAIL.get(nid or "", {}).get("career"):
            employer, industry = fallback_careers[idx % len(fallback_careers)]
            session.add(
                CurrentEmployment(
                    alumni_id=a.alumni_id,
                    current_employer=employer,
                    current_industry=industry,
                )
            )
        if not a.linkedin_url:
            a.linkedin_url = f"https://www.linkedin.com/in/mock-{nid}"

    today = datetime.date.today()
    events = [
        Event(
            event_name=ev["name"],
            event_type=ev["type"],
            event_date=today + datetime.timedelta(days=ev["days"]),
            event_location=ev["location"],
            event_notes="[MOCK] seeded event",
        )
        for ev in MOCK_EVENTS
    ]
    session.add_all(events)
    await session.flush()  # assign event_ids
    # Deterministically attach a few alumni to each event (deduped via a set so
    # explicit additions below can't violate the (event, alumni) unique key).
    attendance: set[tuple[int, int]] = set()
    for i, e in enumerate(events):
        for aid in alumni_ids[i : i + 4]:
            attendance.add((e.event_id, aid))
    # Ensure the demo-rich profile (James Doe) attends three recent events so the
    # profile's Recent events panel is well populated.
    james_id = alumni_by_net_id.get("jdoe1")
    if james_id is not None:
        for e in events[:3]:
            attendance.add((e.event_id, james_id))
    for event_id, aid in attendance:
        session.add(
            EventAttendance(
                event_id=event_id, alumni_id=aid, attendance_status="Attended"
            )
        )
    await session.commit()
    return len(MOCK_ALUMNI)


async def main(remove: bool) -> None:
    if SessionLocal is None:
        raise SystemExit("DATABASE_URL is not configured — set it in .env.")
    async with SessionLocal() as session:
        if remove:
            removed = await remove_mock(session)
            print(f"Removed mock data: {removed} alumni (+ MOCK_DATA source).")
        else:
            seeded = await seed_mock(session)
            print(f"Seeded {seeded} mock alumni (tagged source={MOCK_SOURCE_NAME}).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remove", action="store_true", help="Delete all mock data and exit."
    )
    args = parser.parse_args()
    asyncio.run(main(args.remove))

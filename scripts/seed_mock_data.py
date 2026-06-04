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
from app.models.data_source import DataSource
from app.models.event import Event, EventAttendance

MOCK_EVENTS = [
    {"name": "Spring NetTrek 2026", "type": "Net Trek", "date": datetime.date(2026, 3, 14), "location": "New York, NY"},
    {"name": "Alumni Reunion BBQ", "type": "BBQ", "date": datetime.date(2025, 8, 22), "location": "Provo, UT"},
    {"name": "Finance Conference 2025", "type": "Finance Conference", "date": datetime.date(2025, 11, 5), "location": "Salt Lake City, UT"},
    {"name": "Women in Finance Mixer", "type": "Club Event", "date": datetime.date(2026, 2, 10), "location": "Provo, UT"},
    {"name": "Recruiting Night — Goldman Sachs", "type": "Recruiting Event", "date": datetime.date(2026, 1, 20), "location": "Virtual"},
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


async def _get_source(session) -> DataSource | None:
    return await session.scalar(
        select(DataSource).where(DataSource.source_name == MOCK_SOURCE_NAME)
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

    events = [
        Event(
            event_name=ev["name"],
            event_type=ev["type"],
            event_date=ev["date"],
            event_location=ev["location"],
            event_notes="[MOCK] seeded event",
        )
        for ev in MOCK_EVENTS
    ]
    session.add_all(events)
    await session.flush()  # assign event_ids
    # Deterministically attach a few alumni to each event.
    for i, e in enumerate(events):
        for aid in alumni_ids[i : i + 4]:
            session.add(
                EventAttendance(
                    event_id=e.event_id, alumni_id=aid, attendance_status="Attended"
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

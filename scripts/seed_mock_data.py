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

from app.core.database import SessionLocal, dispose_engine
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
# Every alumnus has a full birth_date (mock). A few have a free-text spouse
# (non-alumni). James Doe (jdoe1) and Ava Lee (alee2) are married AND both
# alumni — that reciprocal link is wired post-flush via MOCK_SPOUSE_LINKS (their
# alumni_ids aren't known until the rows are inserted), so it demonstrates the
# clickable spouse → profile link.
MOCK_ALUMNI: list[dict] = [
    {"byu_id": "001000001", "net_id": "jdoe1", "first_name": "James", "last_name": "Doe", "gender": "M", "birth_date": datetime.date(1990, 3, 15), "graduation_year": 2012, "finance_program_year": 2011, "linkedin_url": "https://linkedin.com/in/mock-jdoe", "notes": "[MOCK] Investment banking track."},
    {"byu_id": "001000002", "net_id": "alee2", "first_name": "Ava", "last_name": "Lee", "preferred_first_name": "Avy", "gender": "F", "birth_date": datetime.date(1993, 7, 22), "graduation_year": 2015, "finance_program_year": 2014, "notes": "[MOCK] PE associate."},
    {"byu_id": "001000003", "net_id": "mchen3", "first_name": "Marcus", "middle_name": "T", "last_name": "Chen", "gender": "M", "birth_date": datetime.date(1996, 1, 10), "graduation_year": 2018, "finance_program_year": 2017},
    {"byu_id": "001000004", "net_id": "srivera4", "first_name": "Sofia", "last_name": "Rivera", "gender": "F", "birth_date": datetime.date(1998, 11, 5), "graduation_year": 2020, "finance_program_year": 2019, "linkedin_url": "https://linkedin.com/in/mock-srivera"},
    {"byu_id": "001000005", "net_id": "bpatel5", "first_name": "Benjamin", "last_name": "Patel", "gender": "M", "birth_date": datetime.date(1987, 5, 30), "graduation_year": 2010, "finance_program_year": 2009, "graduate_degree": "MBA", "spouse_first_name": "Priya", "spouse_last_name": "Patel", "spouse_birth_date": datetime.date(1988, 9, 9)},
    {"byu_id": "001000006", "net_id": "enguyen6", "first_name": "Emily", "last_name": "Nguyen", "gender": "F", "birth_date": datetime.date(2000, 2, 18), "graduation_year": 2022, "finance_program_year": 2021, "notes": "[MOCK] Corporate finance."},
    {"byu_id": "001000007", "net_id": "dkim7", "first_name": "Daniel", "last_name": "Kim", "gender": "M", "birth_date": datetime.date(1994, 9, 12), "graduation_year": 2016, "finance_program_year": 2015},
    {"byu_id": "001000008", "net_id": "ojohnson8", "first_name": "Olivia", "last_name": "Johnson", "gender": "F", "birth_date": datetime.date(1997, 6, 25), "graduation_year": 2019, "finance_program_year": 2018, "linkedin_url": "https://linkedin.com/in/mock-ojohnson", "spouse_first_name": "Mark", "spouse_last_name": "Johnson", "spouse_birth_date": datetime.date(1996, 2, 2)},
    {"byu_id": "001000009", "net_id": "lgarcia9", "first_name": "Lucas", "last_name": "Garcia", "gender": "M", "birth_date": datetime.date(1991, 4, 8), "graduation_year": 2013, "finance_program_year": 2012},
    {"byu_id": "001000010", "net_id": "hwhite10", "first_name": "Hannah", "last_name": "White", "gender": "F", "birth_date": datetime.date(1999, 12, 1), "graduation_year": 2021, "finance_program_year": 2020, "notes": "[MOCK] Wealth management.", "spouse_first_name": "Caleb", "spouse_last_name": "White", "spouse_birth_date": datetime.date(1998, 8, 8)},
    {"byu_id": "001000011", "net_id": "nwright11", "first_name": "Nathan", "last_name": "Wright", "gender": "M", "birth_date": datetime.date(1986, 8, 19), "graduation_year": 2009, "finance_program_year": 2008, "deceased": True, "notes": "[MOCK] Deceased — for filter testing."},
    {"byu_id": "001000012", "net_id": "gmartin12", "first_name": "Grace", "last_name": "Martin", "gender": "F", "birth_date": datetime.date(1995, 3, 27), "graduation_year": 2017, "finance_program_year": 2016},
    {"byu_id": "001000013", "net_id": "ethomas13", "first_name": "Ethan", "last_name": "Thomas", "gender": "M", "birth_date": datetime.date(2001, 10, 14), "graduation_year": 2023, "finance_program_year": 2022, "linkedin_url": "https://linkedin.com/in/mock-ethomas"},
    {"byu_id": "001000014", "net_id": "iclark14", "first_name": "Isabella", "last_name": "Clark", "gender": "F", "birth_date": datetime.date(1992, 7, 3), "graduation_year": 2014, "finance_program_year": 2013, "archived": True, "notes": "[MOCK] Archived — for soft-delete testing."},
    {"byu_id": "001000015", "net_id": "alewis15", "first_name": "Andrew", "last_name": "Lewis", "gender": "M", "birth_date": datetime.date(1988, 1, 22), "graduation_year": 2011, "finance_program_year": 2010, "graduate_degree": "MAcc"},
    {"byu_id": "001000016", "net_id": "shall16", "first_name": "Sophia", "last_name": "Hall", "gender": "F", "birth_date": datetime.date(1998, 5, 16), "graduation_year": 2020, "finance_program_year": 2019, "notes": "[MOCK] Equity research."},
]

# Reciprocal spouse links between alumni records (net_id -> spouse net_id).
# Wired after the alumni rows are flushed (so alumni_ids exist). Each side's
# spouse name + birthday is copied from the partner's row.
MOCK_SPOUSE_LINKS: dict[str, str] = {
    "jdoe1": "alee2",
    "alee2": "jdoe1",
}


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
            "current_industry_secondary": "Private Equity",
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
        "program": {"piff_donor": True, "mentor_willing": True},
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
        "program": {"piff_donor": True, "mentor_willing": True},
        "tags": ["Donor", "Mentor"],
    },
    "lgarcia9": {
        "contact": {"city": "Dallas", "state": "TX", "country": "USA", "region": "South"},
        "career": {"current_employer": "JPMorgan", "current_title": "Vice President", "current_industry": "Investment Banking", "current_city": "Dallas", "current_state": "TX", "seniority_level": "Vice President"},
    },
    "hwhite10": {
        "contact": {"city": "New York", "state": "NY", "country": "USA", "region": "Northeast"},
        "career": {"current_employer": "Morgan Stanley", "current_title": "Wealth Manager", "current_industry": "Wealth Management", "current_city": "New York", "current_state": "NY", "seniority_level": "Vice President"},
        "program": {"piff_donor": True, "mentor_willing": True},
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


# --- Bulk generation (#152) --------------------------------------------------
#
# The 16 hand-crafted records above mirror the Figma demo. To make the alumni
# list, geography map shading, and profiles look truly populated, we ALSO
# generate a large batch of unique, complete synthetic records. Generation is
# deterministic (seeded RNG) so re-running produces the SAME 250 rows — combined
# with seed_mock()'s remove-then-insert, the result is fully idempotent.
#
# Every generated row is COMPLETE: name + unique byu_id/net_id/mst_id, gender,
# plausible birthday, grad year, a contact row (city/state/region for the map),
# a current-employment row (employer/title/industry/city/state), one education
# row, and most carry a tag or two. A realistic minority are deceased/archived
# or marked as a "friend" (is_alumni=false) so the new friends split has data.
# Everything is tagged via the same MOCK_DATA source, so --remove wipes it all.

GENERATED_COUNT = 250

_FIRST_NAMES_M = [
    "Liam", "Noah", "Oliver", "Elijah", "William", "Henry", "Lucas", "Mason",
    "Logan", "Jackson", "Aiden", "Carter", "Jack", "Owen", "Wyatt", "Caleb",
    "Hunter", "Connor", "Spencer", "Tyler", "Bryson", "Porter", "Easton",
    "Tanner", "Cole", "Brigham", "Parker", "Dallin", "Kyle", "Trevor",
]
_FIRST_NAMES_F = [
    "Olivia", "Emma", "Charlotte", "Amelia", "Sophia", "Isabella", "Mia",
    "Evelyn", "Harper", "Abigail", "Emily", "Ella", "Scarlett", "Grace",
    "Chloe", "Lily", "Aria", "Brooke", "Hailey", "Paige", "Sydney", "McKenna",
    "Brynn", "Whitney", "Kennedy", "Savannah", "Taylor", "Morgan", "Reagan",
    "Eliza",
]
_LAST_NAMES = [
    "Anderson", "Bennett", "Brooks", "Bryant", "Caldwell", "Carlson", "Coleman",
    "Crawford", "Davies", "Erickson", "Fletcher", "Foster", "Gallagher",
    "Hansen", "Hawkins", "Higgins", "Holloway", "Ingram", "Jennings", "Knight",
    "Larsen", "Lawson", "Maxwell", "Mercer", "Nielsen", "Osborne", "Pearson",
    "Quincy", "Ramsey", "Reeves", "Sanders", "Schwartz", "Sullivan", "Tucker",
    "Underwood", "Vaughn", "Wallace", "Whitaker", "Young", "Zimmerman",
    "Abbott", "Barrett", "Castillo", "Donovan", "Everett", "Franklin",
    "Griffith", "Holt", "Jacobsen", "Kendrick",
]
_BIRTH_NAMES = [
    "Stevenson", "Marsh", "Pope", "Wren", "Holland", "Frost", "Barker",
    "Dalton", "Mead", "Crane",
]

# (city, state, region) tuples. Utah-heavy (BYU hub) plus the major financial
# centers, so the geography map has a realistic, well-shaded distribution.
_LOCATIONS = [
    ("Provo", "UT", "Mountain West"),
    ("Salt Lake City", "UT", "Mountain West"),
    ("Lehi", "UT", "Mountain West"),
    ("Orem", "UT", "Mountain West"),
    ("Draper", "UT", "Mountain West"),
    ("American Fork", "UT", "Mountain West"),
    ("New York", "NY", "Northeast"),
    ("Boston", "MA", "Northeast"),
    ("Stamford", "CT", "Northeast"),
    ("Chicago", "IL", "Midwest"),
    ("Dallas", "TX", "South"),
    ("Austin", "TX", "South"),
    ("Houston", "TX", "South"),
    ("Atlanta", "GA", "South"),
    ("Charlotte", "NC", "South"),
    ("San Francisco", "CA", "West"),
    ("San Jose", "CA", "West"),
    ("Los Angeles", "CA", "West"),
    ("Seattle", "WA", "West"),
    ("Denver", "CO", "Mountain West"),
    ("Phoenix", "AZ", "West"),
    ("Las Vegas", "NV", "West"),
    ("Washington", "DC", "Northeast"),
    ("Miami", "FL", "South"),
]

# (employer, industry-from-canonical-INDUSTRIES). Industries here are the
# canonical vocabulary used by the filters so the advanced-search facets and the
# real records line up.
_EMPLOYERS = [
    ("Goldman Sachs", "Investment Banking"),
    ("J.P. Morgan", "Investment Banking"),
    ("Morgan Stanley", "Investment Banking"),
    ("Bank of America", "Commercial Banking"),
    ("Wells Fargo", "Commercial Banking"),
    ("Citi", "Commercial Banking"),
    ("Bain Capital", "Private Equity"),
    ("KKR", "Private Equity"),
    ("Blackstone", "Private Equity"),
    ("BlackRock", "Asset Management"),
    ("Fidelity", "Asset Management"),
    ("Vanguard", "Asset Management"),
    ("Northern Trust", "Asset Management"),
    ("PIMCO", "Asset Management"),
    ("Charles Schwab", "Wealth Management"),
    ("Edward Jones", "Wealth Management"),
    ("UBS", "Wealth Management"),
    ("McKinsey & Company", "Consulting"),
    ("Bain & Company", "Consulting"),
    ("Deloitte", "Consulting"),
    ("KPMG", "Valuation & Advisory"),
    ("EY", "Valuation & Advisory"),
    ("Qualtrics", "Corporate Finance"),
    ("Adobe", "Corporate Finance"),
    ("Microsoft", "Corporate Finance"),
    ("Sequoia Capital", "Venture Capital"),
    ("Andreessen Horowitz", "Venture Capital"),
    ("CBRE", "Real Estate"),
    ("Jefferies", "Equity Research"),
    ("Ares Management", "Private Credit"),
]

_TITLES_BY_SENIORITY = [
    ("Analyst", "Analyst"),
    ("Senior Analyst", "Senior Analyst"),
    ("Associate", "Associate"),
    ("Senior Associate", "Senior Associate"),
    ("Manager", "Manager"),
    ("Vice President", "Vice President"),
    ("Director", "Director"),
    ("Principal", "Principal"),
    ("Partner", "Partner"),
]

_UNIVERSITIES = [
    ("Brigham Young University", "Marriott School of Business", "Finance"),
]
_DEGREES = ["BS", "BA"]
_MAJORS = ["Finance", "Accounting", "Economics", "Entrepreneurship"]
_GRADUATE_DEGREES = [None, None, None, "MBA", "MAcc"]
_GENDERS = ["M", "F"]


def _generate_records(start_index: int) -> tuple[list[dict], dict[str, dict]]:
    """Build GENERATED_COUNT unique alumni dicts + their MOCK_DETAIL entries.

    ``start_index`` offsets the byu_id / net_id sequence so generated rows never
    collide with the hand-crafted block. Deterministic: a fixed RNG seed makes
    re-runs produce identical data (idempotent with seed_mock's wipe+insert).
    """
    import random

    rng = random.Random(20260630)
    today = datetime.date.today()
    rows: list[dict] = []
    detail: dict[str, dict] = {}

    for i in range(GENERATED_COUNT):
        n = start_index + i  # global sequence number, unique across the batch
        gender = _GENDERS[i % 2]
        first = rng.choice(_FIRST_NAMES_M if gender == "M" else _FIRST_NAMES_F)
        last = _LAST_NAMES[n % len(_LAST_NAMES)]
        # net_id: lowercase letters + the sequence number -> globally unique and
        # matches the ^[a-z0-9]{2,12}$ shape. byu_id: 9 digits, unique. mst_id:
        # a distinct MST-prefixed token.
        net_id = f"mk{n:05d}"
        byu_id = f"{100000000 + n:09d}"
        mst_id = f"MST-{n:06d}"

        grad_year = 2005 + (n % 20)  # 2005..2024
        finance_year = grad_year - 1
        # Birthday ~22 years before graduation, jittered so dates vary.
        birth_year = grad_year - 22 - (i % 3)
        birth_month = (i % 12) + 1
        birth_day = (i % 27) + 1
        birth_date = datetime.date(birth_year, birth_month, birth_day)

        city, state, region = rng.choice(_LOCATIONS)
        employer, industry = rng.choice(_EMPLOYERS)
        title, seniority = rng.choice(_TITLES_BY_SENIORITY)
        degree = rng.choice(_DEGREES)
        major = rng.choice(_MAJORS)
        grad_degree = rng.choice(_GRADUATE_DEGREES)
        university, college, department = _UNIVERSITIES[0]

        # A small, realistic minority of special-case rows.
        deceased = i % 50 == 7        # ~2% deceased
        archived = i % 40 == 13       # ~2.5% archived (soft-deleted)
        is_alumni = not (i % 12 == 5)  # ~8% are "friends" (non-alumni contacts)

        row: dict = {
            "byu_id": byu_id,
            "mst_id": mst_id,
            "net_id": net_id,
            "first_name": first,
            "last_name": last,
            "gender": gender,
            "birth_date": birth_date,
            "graduation_year": grad_year,
            "finance_program_year": finance_year,
            "is_alumni": is_alumni,
            "linkedin_url": f"https://www.linkedin.com/in/mock-{net_id}",
            "notes": "[MOCK] Generated alumni record.",
        }
        if grad_degree:
            row["graduate_degree"] = grad_degree
        # Some women carry a maiden / birth name (exercises #216 search).
        if gender == "F" and i % 5 == 0:
            row["birth_name"] = rng.choice(_BIRTH_NAMES)
        # ~30% preferred name.
        if i % 10 < 3:
            row["preferred_first_name"] = first
        # A subset married to a free-text (non-alumni) spouse.
        if i % 4 == 0:
            sp_gender_pool = _FIRST_NAMES_F if gender == "M" else _FIRST_NAMES_M
            row["spouse_first_name"] = rng.choice(sp_gender_pool)
            row["spouse_last_name"] = last
            row["spouse_birth_date"] = datetime.date(
                birth_year + (i % 3) - 1, ((i + 4) % 12) + 1, ((i + 7) % 27) + 1
            )
        if deceased:
            row["deceased"] = True
        if archived:
            row["archived"] = True

        rows.append(row)

        # Matching COMPLETE detail so the list/map/profile render populated.
        d: dict = {
            "contact": {
                "personal_email": f"{net_id}@example.com",
                "phone": f"+1 (801) 555-{(1000 + n) % 10000:04d}",
                "city": city,
                "state": state,
                "country": "USA",
                "region": region,
            },
            "career": {
                "current_employer": employer,
                "current_title": title,
                "current_industry": industry,
                "current_city": city,
                "current_state": state,
                "current_country": "USA",
                "seniority_level": seniority,
            },
            "education": [
                {
                    "university": university,
                    "college": college,
                    "department": department,
                    "degree": degree,
                    "major": major,
                    "degree_status": "Completed",
                    "degree_year": grad_year,
                }
            ],
        }
        # Roughly half carry one or two engagement tags so the chips/filters have
        # variety without every row looking identical.
        tag_pool = MOCK_TAGS
        picks = []
        if i % 2 == 0:
            picks.append(tag_pool[i % len(tag_pool)])
        if i % 6 == 0:
            picks.append(tag_pool[(i + 2) % len(tag_pool)])
        if picks:
            d["tags"] = list(dict.fromkeys(picks))  # de-dupe, keep order
        # A donor/mentor engagement profile for a subset (drives those filters).
        if i % 7 == 0:
            d["program"] = {"piff_donor": True, "mentor_willing": True}
        elif i % 7 == 3:
            d["program"] = {"mentor_willing": True}
        # Status labels for the deceased / archived special cases.
        if deceased:
            d["status_labels"] = ["Deceased"]
        elif archived:
            d["status_labels"] = ["Inactive"]
        detail[net_id] = d

    return rows, detail


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


async def _seed_details(
    session,
    alumni_by_net_id: dict[str, int],
    actor_user_id: int | None,
    detail_map: dict[str, dict] | None = None,
) -> None:
    """Insert the rich per-alumni related rows for ``detail_map`` (the
    hand-crafted MOCK_DETAIL by default, or the combined hand-crafted + generated
    map passed by seed_mock)."""
    if detail_map is None:
        detail_map = MOCK_DETAIL
    tag_ids = await _get_or_create_lookup(session, Tag, "tag_name", MOCK_TAGS)
    status_ids = await _get_or_create_lookup(
        session, StatusLabel, "status_label_name", MOCK_STATUS_LABELS
    )
    now = datetime.datetime.now(datetime.UTC)

    for net_id, detail in detail_map.items():
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

    # Combine the 16 hand-crafted demo records with the 250 generated ones. The
    # generated batch starts at sequence 1000 so its byu_id / net_id / mst_id can
    # never collide with the hand-crafted block (001000001..001000016). Both
    # share the MOCK_DATA source, so --remove wipes everything.
    generated_rows, generated_detail = _generate_records(start_index=1000)
    all_rows = MOCK_ALUMNI + generated_rows
    combined_detail = {**MOCK_DETAIL, **generated_detail}

    alumni = [Alumni(source_id=source.source_id, **row) for row in all_rows]
    session.add_all(alumni)
    await session.flush()  # assign alumni_ids
    alumni_ids = [a.alumni_id for a in alumni]
    alumni_by_net_id = {
        row["net_id"]: a.alumni_id
        for row, a in zip(all_rows, alumni, strict=True)
        if row.get("net_id")
    }

    # Wire reciprocal spouse links now that alumni_ids exist. Copy each spouse's
    # name + birthday from the partner's row so the linked records stay in sync.
    alumni_obj_by_net_id = {
        row["net_id"]: a
        for row, a in zip(all_rows, alumni, strict=True)
        if row.get("net_id")
    }
    row_by_net_id = {r["net_id"]: r for r in all_rows if r.get("net_id")}
    for net_id, spouse_net_id in MOCK_SPOUSE_LINKS.items():
        a = alumni_obj_by_net_id.get(net_id)
        spouse_obj = alumni_obj_by_net_id.get(spouse_net_id)
        spouse_row = row_by_net_id.get(spouse_net_id)
        if a is None or spouse_obj is None or spouse_row is None:
            continue
        a.spouse_alumni_id = spouse_obj.alumni_id
        a.spouse_first_name = (
            spouse_row.get("preferred_first_name") or spouse_row.get("first_name")
        )
        a.spouse_last_name = spouse_row.get("last_name")
        a.spouse_birth_date = spouse_row.get("birth_date")

    # Attribute seeded interactions/tasks/attachments to a real provisioned user
    # so the "logged by / assigned to" names render (falls back to None).
    actor_user_id = await session.scalar(
        select(User.user_id).order_by(User.user_id).limit(1)
    )
    await _seed_details(session, alumni_by_net_id, actor_user_id, combined_detail)

    # Guarantee every alumnus has a current employer + industry and a LinkedIn
    # so the alumni list never renders blank cells. Fallbacks are deterministic
    # (indexed) and use the canonical industry vocabulary. (Generated rows always
    # carry a career section, so this only backfills the hand-crafted block.)
    fallback_careers = [
        ("Deloitte", "Consulting"),
        ("Wells Fargo", "Commercial Banking"),
        ("Charles Schwab", "Wealth Management"),
        ("KPMG", "Valuation & Advisory"),
        ("PIMCO", "Asset Management"),
    ]
    for idx, (row, a) in enumerate(zip(all_rows, alumni, strict=True)):
        nid = row.get("net_id")
        if not combined_detail.get(nid or "", {}).get("career"):
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
        # Pull a spread of attendees from across the (now large) population so
        # each event has a realistic, non-trivial guest list.
        for aid in alumni_ids[i :: max(1, len(alumni_ids) // 25)][:25]:
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
    return len(all_rows)


async def main(remove: bool) -> None:
    if SessionLocal is None:
        raise SystemExit("DATABASE_URL is not configured — set it in .env.")
    # Dispose the engine in a finally so this one-off script never leaks
    # connections against Supabase's 15-client session-pooler cap.
    try:
        async with SessionLocal() as session:
            if remove:
                removed = await remove_mock(session)
                print(f"Removed mock data: {removed} alumni (+ MOCK_DATA source).")
            else:
                seeded = await seed_mock(session)
                print(f"Seeded {seeded} mock alumni (tagged source={MOCK_SOURCE_NAME}).")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--remove", action="store_true", help="Delete all mock data and exit."
    )
    args = parser.parse_args()
    asyncio.run(main(args.remove))

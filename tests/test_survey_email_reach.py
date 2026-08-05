"""Who a survey can reach, and who it cannot (#392).

Jake: *"it says they need a personal email to receive the email. It also needs to
be, if they have no personal email send it to work email; if not, have an alert
who it can't send to."*

The old rule was ``personal_email IS NOT NULL``, so an alumnus holding only a
work address was excluded from every survey ever sent — no send, no error, no
trace. These tests pin the replacement: personal preferred, work as the fallback,
and everyone left over surfaced by name instead of vanishing.

Like ``test_survey_followup``, these run the queries FOR REAL against in-memory
SQLite rather than asserting on a canned fake, because the entire risk lives in
the SQL. A fake session cannot tell you whether ``reachable_email_sql`` actually
matches ``resolve_email`` — and that agreement IS the feature: the count the
console shows and the population the sender iterates have to be the same people.

The predicates use Postgres string functions (``btrim`` / ``strpos`` /
``split_part``), which SQLite lacks, so they are registered as UDFs on the test
connection with Postgres semantics. That keeps the production expression tree
under test end to end — the alternative, asserting on compiled SQL text, would
pass happily on a predicate that selects the wrong rows.
"""

import asyncio
import datetime

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.core import email_reach
from app.core.database import Base
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.services import survey_email

_YEAR = 2000
# Comfortably inside the 365-day re-survey window relative to the seeded reply.
_NOW = datetime.datetime(2026, 8, 1, 12, 0, tzinfo=datetime.UTC)


# --------------------------------------------------------------- harness -----


def _pg_split_part(value, delim, index):
    if value is None:
        return None
    parts = value.split(delim)
    return parts[index - 1] if 0 < index <= len(parts) else ""


def _register_pg_functions(dbapi_conn, _record):
    """Postgres string functions, with Postgres semantics, for SQLite."""
    dbapi_conn.create_function(
        "btrim", 1, lambda v: None if v is None else v.strip()
    )
    # Postgres strpos is 1-based and returns 0 when absent — exactly SQLite instr.
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
                # `_load_recipients` bulk-loads employment for the email's
                # "here's what we have on file" block.
                CurrentEmployment.__table__,
            ],
        )
        # survey_responses has a JSONB column SQLite cannot render; only the
        # columns the eligibility predicate reads are needed.
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
        conn.commit()
        yield conn
    engine.dispose()


class _World:
    """A tiny cohort to ask reachability questions of."""

    def __init__(self, conn):
        self.conn = conn
        self.session = _Session(Session(bind=conn))
        self._next_id = 0
        self._next_response_id = 0

    def alum(
        self,
        name,
        *,
        personal=None,
        work=None,
        year=_YEAR,
        deceased=False,
        archived=False,
        status_label=None,
        contact_row=True,
    ):
        self._next_id += 1
        alumni_id = self._next_id
        self.conn.execute(
            Alumni.__table__.insert().values(
                alumni_id=alumni_id,
                first_name=name,
                last_name="Test",
                graduation_year=year,
                is_alumni=True,
                archived=archived,
                deceased=deceased,
            )
        )
        if contact_row:
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

    # -- the three views under test ------------------------------------------

    def recipients(self):
        loaded = asyncio.run(survey_email._load_recipients(self.session, _YEAR))
        kept, _dropped = survey_email.dedupe_by_email(loaded)
        return kept

    def unreachable(self):
        return asyncio.run(survey_email.list_unreachable(self.session, _YEAR))

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


def _emails(recipients):
    return sorted(r.email for r in recipients)


def _names(items):
    return sorted(i.name for i in items)


# ------------------------------------------------ the recipient rule ---------


def test_personal_email_is_preferred_when_both_exist(world):
    """Personal stays the preferred address — the fallback must not change who
    already worked."""
    world.alum("Both", personal="both.personal@byu.edu", work="both.work@firm.com")
    (r,) = world.recipients()
    assert r.email == "both.personal@byu.edu"
    assert r.email_source == email_reach.SOURCE_PERSONAL


def test_work_email_is_used_when_there_is_no_personal_one(world):
    """THE bug: this alumnus was previously excluded from every survey, silently."""
    world.alum("WorkOnly", personal=None, work="workonly@firm.com")
    (r,) = world.recipients()
    assert r.email == "workonly@firm.com"
    assert r.email_source == email_reach.SOURCE_WORK


def test_one_person_gets_exactly_one_email_even_with_two_addresses(world):
    """No double-send. `Recipient` holds a single resolved address, so having
    both columns populated yields one recipient and one message — not one per
    address."""
    world.alum("Both", personal="dual.personal@byu.edu", work="dual.work@firm.com")
    recipients = world.recipients()
    assert len(recipients) == 1
    assert len({r.alumni_id for r in recipients}) == 1
    assert world.breakdown().recipients == 1


@pytest.mark.parametrize(
    "personal,work",
    [
        ("", "usable@firm.com"),          # empty string is not an address
        ("   ", "usable@firm.com"),       # nor is whitespace
        ("not-an-email", "usable@firm.com"),
        ("someone@example.com", "usable@firm.com"),  # reserved placeholder domain
    ],
)
def test_an_unusable_personal_email_falls_through_to_work(world, personal, work):
    """`IS NOT NULL` passed all of these, so a blank or junk personal address
    shadowed a perfectly good work one and the alum was "reachable" at nothing."""
    world.alum("Fallthrough", personal=personal, work=work)
    (r,) = world.recipients()
    assert r.email == "usable@firm.com"
    assert r.email_source == email_reach.SOURCE_WORK


@pytest.mark.parametrize(
    "work", ["", "   ", "no-at-sign", "someone@example.com", None]
)
def test_an_unusable_work_email_does_not_make_someone_reachable(world, work):
    """A non-NULL work column is not the same as a usable address — the whole
    point of validating the fallback rather than trusting the column."""
    world.alum("Unusable", personal=None, work=work)
    assert world.recipients() == []
    assert [i.name for i in world.unreachable()] == ["Unusable Test"]


# ------------------------------------------------ the unreachable surface ----


def test_alumni_with_neither_address_are_not_sent_to_and_are_listed(world):
    """The two halves of Jake's ask, together: they must not be emailed, AND
    they must stop being invisible."""
    world.alum("Reachable", personal="reachable@byu.edu")
    world.alum("NoEmail", personal=None, work=None)
    world.alum("NoContactRow", contact_row=False)

    assert _emails(world.recipients()) == ["reachable@byu.edu"]
    assert _names(world.unreachable()) == ["NoContactRow Test", "NoEmail Test"]


def test_the_unreachable_list_says_why(world):
    """"We never had an address" and "the address we hold is junk" are different
    jobs — the second is fixable on sight, straight from this list."""
    world.alum("Nothing", personal=None, work=None)
    world.alum("Typo", personal="jane@@", work=None)

    by_name = {i.name: i for i in world.unreachable()}
    assert by_name["Nothing Test"].reason == email_reach.REASON_NO_EMAIL
    assert by_name["Typo Test"].reason == email_reach.REASON_UNUSABLE
    # The offending value is shown so it can be corrected.
    assert by_name["Typo Test"].personal_email == "jane@@"
    assert by_name["Nothing Test"].personal_email is None


# ------------------------------------------------ suppression is untouched ---


@pytest.mark.parametrize("label", ["Deceased", "Do Not Contact"])
def test_a_suppressed_alumnus_is_in_neither_bucket(world, label):
    """Suppression is a decision to honour, not a gap to close. A Do Not Contact
    alum must never be emailed AND must never appear on a chase list — putting
    them in the unreachable list would be a quiet instruction to go find their
    address."""
    world.alum("Reachable", personal="reachable@byu.edu")
    world.alum("Suppressed", personal=None, work=None, status_label=label)

    assert _emails(world.recipients()) == ["reachable@byu.edu"]
    assert _names(world.unreachable()) == []

    b = world.breakdown()
    assert b.suppressed == 1
    assert b.unreachable == 0


def test_a_suppressed_alumnus_with_a_good_address_is_still_never_emailed(world):
    """The fallback widens WHO HAS AN ADDRESS; it must not widen who may be
    emailed."""
    world.alum("Suppressed", personal=None, work="ok@firm.com",
               status_label="Do Not Contact")
    assert world.recipients() == []
    assert world.unreachable() == []


def test_the_deceased_flag_alone_also_suppresses(world):
    """The flag and the label are separate columns set independently; both are
    checked, and neither turns someone into an "unreachable" to chase."""
    world.alum("Dead", personal=None, work=None, deceased=True)
    assert world.recipients() == []
    assert world.unreachable() == []
    assert world.breakdown().suppressed == 1


# ------------------------------------------------ the populations agree ------


def test_eligibility_the_count_and_the_send_path_agree(world):
    """The failure mode this whole change exists to avoid: the console reporting
    a number that is not what went out.

    Every figure here is asserted against the SAME cohort — the SQL eligibility
    query, the reported breakdown, and the recipients the sender would iterate.
    """
    world.alum("Personal", personal="p@byu.edu")
    world.alum("WorkFallback", personal=None, work="w@firm.com")
    world.alum("BlankPersonal", personal="", work="w2@firm.com")
    world.alum("Unreachable", personal=None, work=None)
    world.alum("Suppressed", personal="s@byu.edu", status_label="Do Not Contact")
    replied = world.alum("Replied", personal="r@byu.edu")
    world.replied(replied)

    eligible_rows = (
        world.session._session.execute(survey_email.eligible_alumni_query(_YEAR))
        .scalars()
        .all()
    )
    recipients = world.recipients()
    b = world.breakdown()

    # The SQL population and the loaded population are the same people.
    assert {a.alumni_id for a in eligible_rows} == {r.alumni_id for r in recipients}
    assert b.recipients == len(recipients) == 3
    assert b.eligible == len(eligible_rows) == 3
    assert _emails(recipients) == ["p@byu.edu", "w2@firm.com", "w@firm.com"]

    # Reported separately, never merged.
    assert b.suppressed == 1
    assert b.already_responded == 1
    assert b.unreachable == 1
    assert b.work_email_fallback == 2

    # And the buckets partition the cohort — nobody is counted twice or lost.
    assert b.cohort_total == 6
    assert (
        b.suppressed + b.already_responded + b.unreachable + b.eligible
        == b.cohort_total
    )


def test_someone_who_already_replied_is_not_reported_as_unreachable(world):
    """The misdiagnosis behind this issue: a cohort that had simply all replied
    within the 365-day window was reported as having no email addresses. A recent
    responder is neither a recipient nor a gap."""
    replied = world.alum("Replied", personal="r@byu.edu")
    world.replied(replied)

    assert world.recipients() == []
    assert world.unreachable() == []

    b = world.breakdown()
    assert b.already_responded == 1
    assert b.unreachable == 0
    assert b.recipients == 0


def test_a_rejected_reply_leaves_them_surveyable(world):
    """Staff threw that submission away, so the alum is due again — the shipped
    365-day rule, unchanged by the fallback."""
    alum = world.alum("Rejected", personal=None, work="w@firm.com")
    world.replied(alum, status="rejected")
    assert _emails(world.recipients()) == ["w@firm.com"]


def test_shared_addresses_still_collapse_to_one_recipient(world):
    """Two alumni reachable at the same address — now possibly via DIFFERENT
    columns — must still yield one email, because it carries a live edit token."""
    world.alum("SpouseA", personal="shared@byu.edu")
    world.alum("SpouseB", personal=None, work="shared@byu.edu")

    b = world.breakdown()
    assert b.eligible == 2
    assert b.duplicate_emails == 1
    assert b.recipients == 1


def test_an_archived_alumnus_is_neither_a_recipient_nor_unreachable(world):
    """Archived records are out of the cohort entirely — they are not a
    contact-data gap for staff to work."""
    world.alum("Archived", personal=None, work=None, archived=True)
    assert world.recipients() == []
    assert world.unreachable() == []
    assert world.breakdown().cohort_total == 0


# ------------------------------------------------ the pure rule --------------


@pytest.mark.parametrize(
    "personal,work,expected_email,expected_source",
    [
        ("p@byu.edu", "w@firm.com", "p@byu.edu", email_reach.SOURCE_PERSONAL),
        (None, "w@firm.com", "w@firm.com", email_reach.SOURCE_WORK),
        ("", "w@firm.com", "w@firm.com", email_reach.SOURCE_WORK),
        ("p@byu.edu", None, "p@byu.edu", email_reach.SOURCE_PERSONAL),
        (None, None, None, None),
        ("", "", None, None),
        ("bad", "worse", None, None),
        ("a@example.com", "b@example.com", None, None),
    ],
)
def test_resolve_email_rule(personal, work, expected_email, expected_source):
    """The recipient rule on its own, including the cases the SQL twin must
    match. If this table and `reachable_email_sql` ever disagree, the count and
    the send drift apart — which is the bug class this module guards."""
    assert email_reach.resolve_email(personal, work) == (
        expected_email,
        expected_source,
    )

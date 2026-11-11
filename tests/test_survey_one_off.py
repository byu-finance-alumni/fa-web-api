"""Send the survey to ONE alum from their profile (api#669).

The rules this pins, from the issue and Jake's decisions (2026-11-11):

* Same email, same 7-day link, same day-7 and day-14 reminders as a class send.
* A one-off must NEVER count as the class's campaign: it writes nothing to
  ``survey_send_log``, creates no campaign, and the later class send STILL
  emails the person.
* Re-sending is allowed; the preview reports "last sent".
* Never to deceased / Do Not Contact / no-email people -- refused with a reason.
* It counts toward the daily/monthly cap, takes the send lock, and is stopped
  while the alum's class campaign is paused or cancelled.

Runs against in-memory SQLite with the real models and UNIQUE constraints (same
harness as ``test_survey_manual_send_campaign``). Resend is never called: the
batch sender is replaced and only records what it was asked to send.
"""

import asyncio
import contextlib
import datetime

import pytest
from sqlalchemy import BigInteger, create_engine, event, func, select, text
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.dependencies import auth as auth_deps
from app.core.errors import ConflictError, NotFoundError, ServiceError
from app.main import app
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.survey_one_off import SurveyOneOff, SurveyOneOffSendLog
from app.models.survey_retirement import SurveyCampaignRetirement
from app.models.survey_schedule import SurveySchedule, SurveySendLog
from app.models.survey_send_config import SurveySendConfig
from app.models.tags import AlumniStatusLabel, StatusLabel
from app.services import profile, survey_email, survey_one_off, survey_schedule

_YEAR = 2019
_ANN = 1
_BEN = 2


class _Settings:
    survey_token_secret = "one-off-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"
    survey_usage_baseline_at = None
    survey_usage_baseline_today = 0
    survey_usage_baseline_month = 0
    cron_secret = "cron"


@pytest.fixture(autouse=True)
def fake_settings(monkeypatch):
    monkeypatch.setattr(survey_email, "get_settings", lambda: _Settings())


# --------------------------------------------------------------- harness -----


@compiles(BigInteger, "sqlite")
def _bigint_is_integer_on_sqlite(type_, compiler, **kw) -> str:
    return "INTEGER"


def _pg_split_part(value, delim, index):
    if value is None:
        return None
    parts = value.split(delim)
    return parts[index - 1] if 0 < index <= len(parts) else ""


def _register_pg_functions(dbapi_conn, _record):
    dbapi_conn.create_function("btrim", 1, lambda v: None if v is None else v.strip())
    dbapi_conn.create_function("strpos", 2, lambda h, n: 0 if h is None else h.find(n) + 1)
    dbapi_conn.create_function("split_part", 3, _pg_split_part)


class _Session:
    """The async-session surface the services use, over a synchronous ORM one."""

    def __init__(self, session):
        self._session = session
        self.added = []

    async def execute(self, stmt, params=None):
        if params is None:
            return self._session.execute(stmt)
        return self._session.execute(stmt, params)

    async def scalars(self, stmt):
        return self._session.scalars(stmt)

    async def scalar(self, stmt):
        return self._session.scalar(stmt)

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        # A failed SELECT leaves a SQLite session usable, so a plain block is
        # enough to model the SAVEPOINT the services wrap these reads in.
        yield

    def add(self, obj):
        self.added.append(obj)
        if not isinstance(obj, AuditLog):
            self._session.add(obj)

    async def delete(self, obj):
        self._session.delete(obj)

    async def commit(self):
        self._session.commit()

    async def rollback(self):
        self._session.rollback()


def _ddl(conn):
    from app.core.database import Base

    Base.metadata.create_all(
        conn,
        tables=[
            Alumni.__table__,
            AlumniContactInfo.__table__,
            AlumniStatusLabel.__table__,
            StatusLabel.__table__,
            CurrentEmployment.__table__,
            SurveySchedule.__table__,
            SurveySendLog.__table__,
            SurveyCampaignRetirement.__table__,
            SurveySendConfig.__table__,
            # From the models, so the real UNIQUE (one_off, stage) is in force.
            SurveyOneOff.__table__,
            SurveyOneOffSendLog.__table__,
        ],
    )
    conn.execute(
        text(
            "CREATE TABLE survey_responses ("
            " survey_response_id INTEGER PRIMARY KEY,"
            " alumni_id INTEGER NOT NULL,"
            " graduation_year INTEGER,"
            " payload TEXT NOT NULL DEFAULT '{}',"
            " status VARCHAR(20) NOT NULL,"
            " staged_photo_path VARCHAR(255),"
            " cycle_seq INTEGER,"
            " stage INTEGER,"
            " fill_seconds INTEGER,"
            " submitted_at TIMESTAMP NOT NULL,"
            " reviewed_by_user_id INTEGER,"
            " reviewed_at TIMESTAMP)"
        )
    )
    conn.execute(
        text(
            "CREATE TABLE users ("
            " user_id INTEGER PRIMARY KEY,"
            " first_name VARCHAR(100),"
            " last_name VARCHAR(100),"
            " email VARCHAR(255))"
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


def _now():
    return datetime.datetime.now(datetime.UTC)


def _today():
    return _now().date()


class _World:
    def __init__(self, conn, monkeypatch):
        self.conn = conn
        self.session = _Session(conn)
        self.monkeypatch = monkeypatch
        # Every email Resend was asked to deliver, in order.
        self.emails: list[dict] = []
        self.fail_with: Exception | None = None
        self._resp_id = 0

        async def _batch(emails):
            if self.fail_with is not None:
                raise self.fail_with
            self.emails.extend(emails)
            return (None, None, [f"re_{len(self.emails)}_{i}" for i in range(len(emails))])

        monkeypatch.setattr(survey_email, "_send_batch", _batch)

        async def _no_sleep(_seconds):
            return None

        monkeypatch.setattr(survey_one_off.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(survey_email.asyncio, "sleep", _no_sleep)

    # -- seeding -------------------------------------------------------------

    def alum(
        self,
        alumni_id,
        first="Ann",
        *,
        personal="__default__",
        work=None,
        year=_YEAR,
        is_alumni=True,
        archived=False,
        deceased=False,
    ):
        self.conn.execute(
            Alumni.__table__.insert(),
            [
                {
                    "alumni_id": alumni_id,
                    "first_name": first,
                    "last_name": "Lovelace",
                    "graduation_year": year,
                    "is_alumni": is_alumni,
                    "archived": archived,
                    "deceased": deceased,
                }
            ],
        )
        self.conn.execute(
            AlumniContactInfo.__table__.insert(),
            [
                {
                    "contact_info_id": alumni_id,
                    "alumni_id": alumni_id,
                    "personal_email": (
                        f"alum{alumni_id}@byu.edu" if personal == "__default__" else personal
                    ),
                    "work_email": work,
                }
            ],
        )
        self.conn.commit()

    def label(self, alumni_id, name):
        label_id = self.conn.scalar(
            select(StatusLabel.status_label_id).where(StatusLabel.status_label_name == name)
        )
        if label_id is None:
            self.conn.execute(StatusLabel.__table__.insert(), [{"status_label_name": name}])
            label_id = self.conn.scalar(
                select(StatusLabel.status_label_id).where(StatusLabel.status_label_name == name)
            )
        self.conn.execute(
            AlumniStatusLabel.__table__.insert(),
            [{"alumni_id": alumni_id, "status_label_id": label_id}],
        )
        self.conn.commit()

    def schedule(self, *, status="active", start_date=None, cycle=1):
        self.conn.execute(
            SurveySchedule.__table__.insert(),
            [
                {
                    "survey_schedule_id": _YEAR * 10 + cycle,
                    "graduation_year": _YEAR,
                    "start_date": start_date or _today(),
                    "status": status,
                    "cycle_seq": cycle,
                }
            ],
        )
        self.conn.commit()

    def cap(self, daily, monthly=10_000, enabled=True):
        self.conn.execute(
            SurveySendConfig.__table__.insert(),
            [{"id": 1, "enabled": enabled, "daily_limit": daily, "monthly_limit": monthly}],
        )
        self.conn.commit()

    def replied(self, alumni_id, *, status="pending", days_ago=0, at=None):
        self._resp_id += 1
        self.conn.execute(
            text(
                "INSERT INTO survey_responses (survey_response_id, alumni_id,"
                " graduation_year, payload, status, submitted_at)"
                " VALUES (:i, :a, :y, '{}', :s, :t)"
            ),
            {
                "i": self._resp_id,
                "a": alumni_id,
                "y": _YEAR,
                "s": status,
                "t": at or (_now() - datetime.timedelta(days=days_ago)),
            },
        )
        self.conn.commit()

    def age_one_offs(self, days):
        """Move every one-off (and its emails) `days` into the past."""
        delta = datetime.timedelta(days=days)
        for row in self.conn.scalars(select(SurveyOneOff)).all():
            row.started_at = row.started_at - delta
        for row in self.conn.scalars(select(SurveyOneOffSendLog)).all():
            row.sent_at = row.sent_at - delta
        self.conn.commit()

    # -- actions -------------------------------------------------------------

    def preview(self, alumni_id=_ANN):
        return asyncio.run(survey_one_off.get_preview(self.session, alumni_id))

    def send(self, alumni_id=_ANN, actor_user_id=7):
        return asyncio.run(
            survey_one_off.send_one_off(self.session, alumni_id, actor_user_id=actor_user_id)
        )

    def class_send(self):
        return asyncio.run(
            survey_email.send_campaign(
                self.session, graduation_year=_YEAR, actor_user_id=7, dry_run=False
            )
        )

    def run_cron(self):
        return asyncio.run(survey_schedule.run_due_schedules(self.session, actor_user_id=None))

    # -- reads ---------------------------------------------------------------

    def to(self):
        return [e["to"][0] for e in self.emails]

    def one_off_rows(self):
        return sorted(
            (r.survey_one_off_id, r.stage)
            for r in self.conn.scalars(select(SurveyOneOffSendLog)).all()
        )

    def class_rows(self):
        return sorted(
            (r.alumni_id, r.stage, r.cycle_seq)
            for r in self.conn.scalars(select(SurveySendLog)).all()
        )

    def campaigns(self):
        return self.conn.scalars(select(SurveySchedule)).all()

    def audits(self):
        return [a for a in self.session.added if isinstance(a, AuditLog)]


@pytest.fixture
def world(monkeypatch):
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", _register_pg_functions)
    with engine.begin() as conn:
        _ddl(conn)
    with Session(engine) as session:
        yield _World(session, monkeypatch)
    engine.dispose()


# ================================================================ preview ======


def test_preview_names_the_person_and_the_one_address(world):
    world.alum(_ANN, work="ann@work.com")
    p = world.preview()
    assert p.can_send is True
    assert p.reason_code is None and p.reason is None
    assert p.name == "Ann Lovelace"
    assert p.email == "alum1@byu.edu"
    assert p.email_source == "personal"
    assert p.graduation_year == _YEAR
    assert p.last_sent_at is None


def test_preview_falls_back_to_the_work_email(world):
    world.alum(_ANN, personal=None, work="ann@work.com")
    p = world.preview()
    assert p.can_send is True
    assert (p.email, p.email_source) == ("ann@work.com", "work")


@pytest.mark.parametrize(
    ("kwargs", "label", "code"),
    [
        ({"deceased": True}, None, "suppressed"),
        ({}, "Do Not Contact", "suppressed"),
        ({}, "Deceased", "suppressed"),
        ({"personal": None, "work": None}, None, "unreachable"),
        ({"personal": "someone@example.com"}, None, "unreachable"),
        ({"year": None}, None, "no_graduation_year"),
        ({"is_alumni": False}, None, "not_alumni"),
        ({"archived": True}, None, "archived"),
    ],
)
def test_people_who_must_not_be_emailed_are_refused_with_a_reason(world, kwargs, label, code):
    world.alum(_ANN, **kwargs)
    if label:
        world.label(_ANN, label)
    p = world.preview()
    assert p.can_send is False
    assert p.reason_code == code
    assert p.reason  # plain words for the disabled button
    with pytest.raises(ConflictError):
        world.send()
    assert world.emails == []
    assert world.one_off_rows() == []


@pytest.mark.parametrize(
    ("status", "code"), [("paused", "campaign_paused"), ("cancelled", "campaign_cancelled")]
)
def test_a_paused_or_stopped_class_campaign_blocks_a_one_off(world, status, code):
    world.alum(_ANN)
    world.schedule(status=status)
    p = world.preview()
    assert (p.can_send, p.reason_code) == (False, code)
    with pytest.raises(ConflictError):
        world.send()
    assert world.emails == []


@pytest.mark.parametrize("status", ["scheduled", "active", "completed"])
def test_a_running_or_finished_class_campaign_does_not_block(world, status):
    world.alum(_ANN)
    world.schedule(status=status)
    assert world.preview().can_send is True


def test_unknown_alum_is_404(world):
    with pytest.raises(NotFoundError):
        world.preview(999)
    with pytest.raises(NotFoundError):
        world.send(999)


# ================================================================== send ======


def test_send_emails_exactly_that_person_once(world):
    world.alum(_ANN)
    world.alum(_BEN, first="Ben")
    result = world.send()
    assert world.to() == ["alum1@byu.edu"]
    assert result.email == "alum1@byu.edu"
    assert result.email_source == "personal"
    assert result.reminders_scheduled is True
    # Logged as a one-off: one ask, its stage-0 email.
    assert len(world.one_off_rows()) == 1 and world.one_off_rows()[0][1] == 0


def test_it_is_the_same_email_and_link_as_the_class_send(world):
    world.alum(_ANN)
    world.send()
    (email,) = world.emails
    link_prefix = "https://finance.alumni.byu.edu/survey/"
    assert link_prefix in email["text"]
    token = email["text"].split(link_prefix, 1)[1].split()[0]
    assert survey_email.verify_survey_token(token) == _ANN
    # The initial: no reminder note on top.
    subject, _html, text_body = survey_email.render_survey_email(
        survey_email.Recipient(alumni_id=_ANN, first_name="Ann", email="x", on_file=()),
        "L",
    )
    assert email["subject"] == subject
    assert text_body.splitlines()[0] == email["text"].splitlines()[0]
    tags = {t["name"]: t["value"] for t in email["tags"]}
    assert tags == {"alumni_id": "1", "graduation_year": str(_YEAR), "stage": "0", "one_off": "1"}


def test_no_unsubscribe_link(world):
    world.alum(_ANN)
    world.send()
    (email,) = world.emails
    for part in (email["html"], email["text"]):
        assert "unsubscribe" not in part.lower()
    assert "headers" not in email


def test_send_never_touches_the_class_campaign(world):
    world.alum(_ANN)
    world.send()
    assert world.class_rows() == []
    assert world.campaigns() == []


def test_the_later_class_send_still_emails_them(world):
    """Jake: the grad-year send does NOT skip someone who got a one-off."""
    world.alum(_ANN)
    world.alum(_BEN, first="Ben")
    world.send(_ANN)
    result = world.class_send()
    assert result.sent == 2
    assert world.to() == ["alum1@byu.edu", "alum1@byu.edu", "alum2@byu.edu"]
    assert world.class_rows() == [(_ANN, 0, 1), (_BEN, 0, 1)]


def test_a_one_off_during_a_running_campaign_does_not_change_it(world):
    world.alum(_ANN)
    world.alum(_BEN, first="Ben")
    world.schedule(status="active")
    world.send(_ANN)
    # The class cron still owes BOTH people their initial.
    world.run_cron()
    assert world.class_rows() == [(_ANN, 0, 1), (_BEN, 0, 1)]
    (campaign,) = world.campaigns()
    assert campaign.status == "active"


def test_re_send_is_allowed_and_preview_reports_last_sent(world):
    world.alum(_ANN)
    assert world.preview().last_sent_at is None
    world.send()
    first = world.preview().last_sent_at
    assert first is not None
    world.send()
    assert world.to() == ["alum1@byu.edu", "alum1@byu.edu"]
    assert len({one_off for one_off, _stage in world.one_off_rows()}) == 2


def test_last_sent_also_counts_class_emails(world):
    world.alum(_ANN)
    world.class_send()
    assert world.preview().last_sent_at is not None


def test_preview_reports_a_recent_reply_but_still_allows_a_send(world):
    world.alum(_ANN)
    world.replied(_ANN, status="applied", days_ago=10)
    p = world.preview()
    assert p.last_replied_at is not None
    assert p.can_send is True


def test_send_is_audited_on_the_alum_without_the_address(world):
    world.alum(_ANN)
    world.send(actor_user_id=42)
    (audit,) = world.audits()
    assert audit.action_type == "send_survey_one_off"
    assert (audit.entity_type, audit.entity_id, audit.user_id) == ("alumni", _ANN, 42)
    assert "stage=0" in audit.new_value and "scheduled=False" in audit.new_value
    assert "@" not in audit.new_value


def test_resend_message_id_is_recorded(world):
    world.alum(_ANN)
    world.send()
    (row,) = world.conn.scalars(select(SurveyOneOffSendLog)).all()
    assert row.resend_email_id
    assert row.sent_to == "alum1@byu.edu"


# ============================================================ cap and lock ====


def test_one_off_emails_count_toward_the_send_usage(world):
    world.alum(_ANN)
    world.send()
    world.send()
    usage = asyncio.run(survey_email.get_send_usage(world.session))
    assert (usage.sent_today, usage.sent_this_month) == (2, 2)


def test_a_spent_budget_refuses_the_one_off(world):
    world.alum(_ANN)
    world.alum(_BEN, first="Ben")
    world.cap(daily=1)
    world.send(_BEN)
    with pytest.raises(ConflictError, match="limit"):
        world.send(_ANN)
    assert world.to() == ["alum2@byu.edu"]


def test_a_one_off_shrinks_the_class_send_budget(world):
    world.alum(_ANN)
    world.alum(_BEN, first="Ben")
    world.cap(daily=2)
    world.send(_ANN)
    result = world.class_send()
    # Two allowed today; the one-off used one.
    assert result.sent == 1


def test_a_busy_send_lock_refuses_the_one_off(world, monkeypatch):
    world.alum(_ANN)

    @contextlib.asynccontextmanager
    async def _busy():
        yield False

    monkeypatch.setattr(survey_email, "send_lock", _busy)
    with pytest.raises(ConflictError, match="running"):
        world.send()
    assert world.emails == [] and world.one_off_rows() == []


def test_a_rate_limit_releases_the_claim(world):
    world.alum(_ANN)
    world.fail_with = survey_email.ResendRateLimited(30)
    with pytest.raises(ConflictError):
        world.send()
    assert world.one_off_rows() == []
    assert world.conn.scalar(select(func.count()).select_from(SurveyOneOff)) == 0


def test_a_refused_send_releases_the_claim(world):
    world.alum(_ANN)
    world.fail_with = ServiceError("Resend rejected the send (HTTP 403).")
    with pytest.raises(ServiceError):
        world.send()
    assert world.one_off_rows() == []


def test_an_unknown_outcome_keeps_the_claim(world):
    """Possibly delivered: kept, so it counts and is not silently re-sent."""
    world.alum(_ANN)
    world.fail_with = survey_email.ResendDeliveryUnknown("Could not reach the email service.")
    with pytest.raises(ServiceError):
        world.send()
    assert len(world.one_off_rows()) == 1
    assert "failed=True" in world.audits()[0].new_value


# ============================================================== reminders =====


@pytest.mark.parametrize(
    ("days", "sent", "owed"),
    [
        (0, {0}, None),
        (6, {0}, None),
        (7, {0}, 1),
        (13, {0, 1}, None),
        (14, {0, 1}, 2),
        (14, {0}, 1),  # a missed day-7 run catches up first
        (20, {0, 1, 2}, None),
        (28, {0, 1}, 2),
        (29, {0, 1}, None),  # gave up
        (7, set(), None),  # never sent
    ],
)
def test_owed_stage(days, sent, owed):
    today = datetime.date(2026, 11, 30)
    started = datetime.datetime.combine(
        today - datetime.timedelta(days=days), datetime.time(12), tzinfo=datetime.UTC
    )
    assert survey_one_off.owed_stage(started, sent, today) == owed


def test_day_seven_and_day_fourteen_reminders_go_out_from_the_cron(world):
    world.alum(_ANN)
    world.send()
    world.run_cron()
    assert len(world.emails) == 1  # nothing due on day 0

    world.age_one_offs(7)
    summary = world.run_cron()
    assert summary.one_off_reminders_sent == 1
    reminder = world.emails[-1]
    assert reminder["to"] == ["alum1@byu.edu"]
    assert {t["name"]: t["value"] for t in reminder["tags"]}["stage"] == "1"
    # The reminder note goes on top, like a class reminder.
    note = survey_email.survey_message.DEFAULT_MESSAGE.reminder_note.strip()
    if note:
        assert survey_email.copy_to_text(note).splitlines()[0] in reminder["text"]

    # Same day again: claimed already, nothing more.
    assert world.run_cron().one_off_reminders_sent == 0

    world.age_one_offs(7)
    assert world.run_cron().one_off_reminders_sent == 1
    assert {t["name"]: t["value"] for t in world.emails[-1]["tags"]}["stage"] == "2"
    assert sorted(stage for _o, stage in world.one_off_rows()) == [0, 1, 2]

    world.age_one_offs(7)
    assert world.run_cron().one_off_reminders_sent == 0
    # And none of it ever touched the class log.
    assert world.class_rows() == []
    reminder_audits = [a for a in world.audits() if "scheduled=True" in a.new_value]
    assert len(reminder_audits) == 2


def test_no_reminder_after_they_reply(world):
    world.alum(_ANN)
    world.send()
    world.age_one_offs(7)
    world.replied(_ANN, status="confirmed", days_ago=1)
    assert world.run_cron().one_off_reminders_sent == 0
    assert len(world.emails) == 1


def test_a_reply_before_the_one_off_does_not_stop_its_reminders(world):
    world.alum(_ANN)
    world.replied(_ANN, status="applied", days_ago=60)
    world.send()
    world.age_one_offs(7)
    assert world.run_cron().one_off_reminders_sent == 1


def test_reminders_are_held_while_the_class_campaign_is_paused(world):
    world.alum(_ANN)
    world.send()
    world.schedule(status="paused")
    world.age_one_offs(7)
    assert world.run_cron().one_off_reminders_sent == 0


def test_no_reminder_to_someone_marked_do_not_contact_since(world):
    world.alum(_ANN)
    world.send()
    world.label(_ANN, "Do Not Contact")
    world.age_one_offs(7)
    assert world.run_cron().one_off_reminders_sent == 0


def test_a_re_send_restarts_the_reminders(world):
    """Only the newest one-off per alum is reminded: no double chains."""
    world.alum(_ANN)
    world.send()
    world.age_one_offs(7)
    world.send()  # lost the email; sent again on day 7
    assert world.run_cron().one_off_reminders_sent == 0
    world.age_one_offs(7)
    # Newest is now 7 days old (stage 1 owed); the older chain is ignored.
    assert world.run_cron().one_off_reminders_sent == 1


def test_reminders_respect_the_send_budget(world):
    world.alum(_ANN)
    world.send()
    world.age_one_offs(7)
    world.cap(daily=0)
    assert world.run_cron().one_off_reminders_sent == 0


def test_a_rate_limited_reminder_is_released_and_retried(world):
    world.alum(_ANN)
    world.send()
    world.age_one_offs(7)
    world.fail_with = survey_email.ResendRateLimited(5)
    summary = world.run_cron()
    assert summary.one_off_reminders_sent == 0
    assert sorted(stage for _o, stage in world.one_off_rows()) == [0]
    world.fail_with = None
    assert world.run_cron().one_off_reminders_sent == 1


def test_one_off_reminders_and_a_class_campaign_both_run(world):
    world.alum(_ANN)
    world.alum(_BEN, first="Ben")
    world.send(_ANN)
    world.age_one_offs(7)
    world.schedule(status="scheduled")
    summary = world.run_cron()
    assert summary.one_off_reminders_sent == 1
    assert [item.sent for item in summary.ran] == [2]
    assert world.class_rows() == [(_ANN, 0, 1), (_BEN, 0, 1)]


# ======================================================== profile history =====


def test_profile_history_shows_the_one_off_as_an_open_ask(world):
    world.alum(_ANN)
    world.send()
    rows = asyncio.run(profile._derive_survey_history(world.session, _ANN, _YEAR))
    (row,) = rows
    assert row.completed is False
    assert row.survey_notes.startswith("Survey sent (one-off) ")
    assert row.survey_notes.endswith("no reply yet")
    assert row.survey_due_date == _today() + datetime.timedelta(days=21)


def test_profile_history_closes_the_ask_once_they_reply(world):
    world.alum(_ANN)
    world.send()
    world.replied(_ANN, status="pending", at=_now() + datetime.timedelta(seconds=5))
    rows = asyncio.run(profile._derive_survey_history(world.session, _ANN, _YEAR))
    assert [r.completed for r in rows] == [True]


# =================================================================== routes ===


def _guards(path, method):
    def _all_routes(router):
        for route in getattr(router, "routes", []):
            inner = getattr(route, "original_router", None)
            if inner is not None:
                yield from _all_routes(inner)
            elif hasattr(route, "routes"):
                yield from _all_routes(route)
            else:
                yield route

    route = next(
        r
        for r in _all_routes(app)
        if getattr(r, "path", None) == path and method in getattr(r, "methods", set())
    )
    found = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        found.add(dep.call)
        stack.extend(dep.dependencies)
    return found, route


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_routes_are_gated_like_the_profile_edit_button(method):
    guards, route = _guards("/survey/alumni/{alumni_id}/one-off", method)
    assert auth_deps.require_alumni_edit in guards
    assert route.response_model is not None


def test_migration_is_additive_and_locked_down():
    import pathlib
    import re

    repo = pathlib.Path(__file__).resolve().parent.parent
    sql = (repo / "database" / "migrations" / "2026-11-11_survey_one_off_sends.sql").read_text(
        encoding="utf-8"
    )
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    body = body.replace("ON DELETE CASCADE", "").replace("ON DELETE SET NULL", "")
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE|ALTER TABLE survey_send_log)\b", body)
    assert "CREATE TABLE IF NOT EXISTS survey_one_off " in body
    assert "CREATE TABLE IF NOT EXISTS survey_one_off_send_log" in body
    rls = (repo / "database" / "rls_lockdown.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE survey_one_off ENABLE ROW LEVEL SECURITY;" in rls
    assert "ALTER TABLE survey_one_off_send_log ENABLE ROW LEVEL SECURITY;" in rls

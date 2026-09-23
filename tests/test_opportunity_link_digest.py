"""The 6pm job-posting digest to staff (#567).

What is pinned here, in the order it would hurt:

  1. **The survey's email budget leaves room for it.** The digest spends from
     the same Resend account and the same UTC-day quota as the survey, and 6pm
     Mountain is already the NEXT UTC day. Every digest e-mail must shrink the
     survey's daily AND monthly allowance, at the cron's pacing AND at the send
     gate inside ``send_survey_stage`` -- otherwise the noon run meets a 429.
  2. **Exactly one path announces a posting.** Recipients set (and mail
     configured) -> the digest, and the submission path is silent. No
     recipients -> the per-posting alert, and the digest is a no-op. Never both,
     never neither.
  3. **The digest itself**: one e-mail per recipient, silent on a quiet day, a
     watermark that makes the window gap-free, no engineer e-mail copy, and
     nothing PII-shaped in what staff receive.
  4. **The console setting** mirrors ``alert_delivery``: validated, deduped and
     capped, engineer-only, audited through the rerouted AuditLog.
  5. **The cron** is on the schedule the route documents.

Offline: no database, no network, no DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.dependencies.auth import get_current_db_user
from app.core.database import get_session
from app.main import app
from app.models.audit import AuditLog
from app.models.opportunity_link import OpportunityLink
from app.models.opportunity_link_digest import OpportunityLinkDigestConfig
from app.schemas.auth import UserContext
from app.schemas.opportunity_link_digest import (
    MAX_RECIPIENTS,
    OpportunityLinkDigestUpdate,
    clean_recipients,
)
from app.schemas.survey import SurveySendConfigItem
from app.services import (
    failure_alert,
    mailer,
    opportunity_link_alert,
    opportunity_link_digest,
    survey_email,
    survey_schedule,
)
from tests.survey_fakes import SendLogSession

REPO = Path(__file__).resolve().parents[1]
GOOD_URL = "https://careers.acme-capital.example/jobs/analyst-2027"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_cache():
    opportunity_link_digest.reset_cache()
    yield
    opportunity_link_digest.reset_cache()


@pytest.fixture
def mail_ready(monkeypatch):
    """A Resend key and a From address, so the digest can be live."""
    settings = SimpleNamespace(
        resend_api_key="re_test",
        survey_from_email="byufinancealumni@mailing.byu.edu",
        survey_from_name="BYU Finance Alumni",
        alert_sender=None,
        survey_app_base_url="https://finance.alumni.byu.edu",
        environment="production",
    )
    monkeypatch.setattr(opportunity_link_digest, "get_settings", lambda: settings)
    monkeypatch.setattr(opportunity_link_alert, "get_settings", lambda: settings)
    return settings


# =============================================================================
# 1. ⚠️ The survey budget leaves room for the digest
# =============================================================================


class _UsageSession:
    """Answers the two usage aggregates: survey_send_log and the digest log."""

    def __init__(self, survey=(0, 0), digest=(0, 0), digest_breaks=False):
        self._survey = survey  # (month, today)
        self._digest = digest  # (month, today)
        self._breaks = digest_breaks
        self.savepoints = 0

    @contextlib.asynccontextmanager
    async def begin_nested(self):
        self.savepoints += 1
        yield

    async def execute(self, stmt):
        sql = str(stmt)
        if "opportunity_link_digest_send_log" in sql:
            if self._breaks:
                raise RuntimeError('relation "opportunity_link_digest_send_log" does not exist')
            row = self._digest
        else:
            row = self._survey
        return SimpleNamespace(first=lambda: row)


@pytest.fixture
def no_baseline(monkeypatch):
    class _S:
        survey_usage_baseline_at = None
        survey_usage_baseline_today = 0
        survey_usage_baseline_month = 0

    monkeypatch.setattr(survey_email, "get_settings", lambda: _S())


def test_usage_counts_the_digest_emails_in_both_the_day_and_the_month(no_baseline):
    session = _UsageSession(survey=(31, 7), digest=(5, 2))
    usage = _run(survey_email.get_send_usage(session))
    assert usage.sent_today == 9
    assert usage.sent_this_month == 36
    # Read inside a SAVEPOINT, so a failure cannot poison the send's transaction.
    assert session.savepoints == 1


def test_an_unreadable_digest_ledger_counts_as_zero_and_never_blocks_a_send(
    no_baseline,
):
    """The migration-not-yet-applied case: the survey budget is exactly what it
    was before the digest existed, and the survey send is NOT failed."""
    session = _UsageSession(survey=(31, 7), digest_breaks=True)
    usage = _run(survey_email.get_send_usage(session))
    assert (usage.sent_today, usage.sent_this_month) == (7, 31)


def test_the_digest_count_respects_the_manual_usage_baseline_anchor():
    """#544: the baseline covers everything up to its anchor, digest e-mails
    included, so only rows strictly after it may be added on top."""
    anchor = datetime.datetime(2026, 9, 23, 0, 0, tzinfo=datetime.UTC)
    seen = {}

    class _S(_UsageSession):
        async def execute(self, stmt):
            seen["sql"] = str(stmt.compile(compile_kwargs={"literal_binds": True}))
            return SimpleNamespace(first=lambda: (0, 0))

    _run(
        opportunity_link_digest.sent_counts(
            _S(),
            start_today=anchor,
            start_month=anchor.replace(day=1),
            after=anchor,
        )
    )
    assert "sent_at >" in seen["sql"]


def _cap(monkeypatch, *, daily, monthly):
    async def cfg(session):
        return SurveySendConfigItem(enabled=True, daily_limit=daily, monthly_limit=monthly)

    monkeypatch.setattr(survey_schedule, "get_send_config", cfg)


def test_the_daily_allowance_shrinks_by_the_digest_emails(no_baseline, monkeypatch):
    """6pm digest (2 e-mails, next UTC day) -> the noon run plans 98, not 100."""
    _cap(monkeypatch, daily=100, monthly=3000)
    session = _UsageSession(survey=(0, 0), digest=(2, 2))
    assert _run(survey_schedule._run_allowance(session)) == 98


def test_the_monthly_allowance_shrinks_by_the_digest_emails(no_baseline, monkeypatch):
    _cap(monkeypatch, daily=100, monthly=3000)
    # Nothing sent today, but the month is nearly spent -- partly by digests.
    session = _UsageSession(survey=(2990, 0), digest=(8, 0))
    assert _run(survey_schedule._run_allowance(session)) == 2


def test_the_send_gate_itself_enforces_the_digest_share(no_baseline, monkeypatch):
    """Not just the cron's pacing: the console's manual send, which passes no
    limit, is clamped by a budget that already includes the digest. 10 a day, 3
    spent by last night's digest, a cohort of 20 -> 7 emails reach Resend."""
    _cap(monkeypatch, daily=10, monthly=3000)
    monkeypatch.setattr(survey_schedule, "_today", lambda: datetime.date(2026, 9, 23))

    class _Send:
        survey_token_secret = "digest-budget-secret"
        survey_from_email = "test@jakegunnell.com"
        survey_from_name = "BYU Finance Alumni"
        survey_app_base_url = "https://finance.alumni.byu.edu"
        resend_api_key = "re_test_key"
        survey_usage_baseline_at = None
        survey_usage_baseline_today = 0
        survey_usage_baseline_month = 0

    monkeypatch.setattr(survey_email, "get_settings", lambda: _Send())

    async def digest_counts(session, **kw):
        return (3, 3)

    monkeypatch.setattr(opportunity_link_digest, "sent_counts", digest_counts)

    emailed: list[str] = []

    async def batch(emails):
        emailed.extend(e["to"][0] for e in emails)
        return (None, None)

    async def load(session, year):
        return [
            survey_email.Recipient(i, f"Alum{i}", f"a{i}@example.com", (("Company", "X"),))
            for i in range(1, 21)
        ]

    monkeypatch.setattr(survey_email, "_send_batch", batch)
    monkeypatch.setattr(survey_email, "_load_recipients", load)

    result = _run(
        survey_email.send_campaign(
            SendLogSession(), graduation_year=1900, actor_user_id=1, dry_run=False
        )
    )
    assert len(emailed) == 7
    assert result.sent == 7
    assert result.budget_limited is True


def test_the_console_meter_reads_the_same_usage():
    """GET /survey/send-usage returns get_send_usage itself, so the meter includes
    the digest with no second implementation of "usage"."""
    from app.api.routes import survey as survey_routes

    source = Path(survey_routes.__file__).read_text(encoding="utf-8")
    assert "return await survey_email.get_send_usage(session)" in source


# =============================================================================
# 2. Exactly one path announces a posting
# =============================================================================


def _recipients(monkeypatch, value):
    async def _read():
        return list(value)

    monkeypatch.setattr(opportunity_link_digest, "read_recipients", _read)


def test_no_recipients_means_per_posting(monkeypatch, mail_ready):
    _recipients(monkeypatch, [])
    assert _run(opportunity_link_alert.notify_mode()) == opportunity_link_alert.MODE_PER_POSTING


def test_recipients_and_mail_mean_the_digest(monkeypatch, mail_ready):
    _recipients(monkeypatch, ["amy@byu.edu"])
    assert _run(opportunity_link_alert.notify_mode()) == opportunity_link_alert.MODE_DAILY_DIGEST


def test_recipients_without_a_resend_key_stay_per_posting(monkeypatch, mail_ready):
    """A list alone must not be able to switch the notification off: a digest
    that cannot be sent is silence, so per-posting keeps firing."""
    mail_ready.resend_api_key = None
    _recipients(monkeypatch, ["amy@byu.edu"])
    assert _run(opportunity_link_alert.notify_mode()) == opportunity_link_alert.MODE_PER_POSTING


def test_an_unreadable_setting_is_per_posting_never_off(monkeypatch, mail_ready):
    monkeypatch.setattr(opportunity_link_digest.database, "SessionLocal", None)
    assert _run(opportunity_link_digest.read_recipients()) == []
    assert _run(opportunity_link_alert.notify_mode()) == opportunity_link_alert.MODE_PER_POSTING


def test_a_failed_read_keeps_the_last_known_list(monkeypatch):
    opportunity_link_digest._remember(["amy@byu.edu"])
    opportunity_link_digest._cached = (-1e9, ["amy@byu.edu"])  # expired
    monkeypatch.setattr(opportunity_link_digest.database, "SessionLocal", None)
    assert _run(opportunity_link_digest.read_recipients()) == ["amy@byu.edu"]


def test_the_cache_sentinel_is_none_and_not_a_timestamp():
    assert opportunity_link_digest._cached is None


def test_in_per_posting_mode_the_digest_sends_nothing(monkeypatch, mail_ready):
    """The other half of "never both": with no recipients the cron is a no-op
    even when postings are sitting in the window."""
    session = _DigestSession(recipients=[], links=[_link(1)])
    outbox = _outbox(monkeypatch)
    assert _run(opportunity_link_alert.send_digest(session)) is False
    assert outbox["mail"] == [] and outbox["slack"] == []
    assert session.claims == []


def test_switching_the_digest_on_starts_its_window_now(monkeypatch, mail_ready):
    """Postings announced one by one while the list was empty must not be
    reported again by the first digest."""
    session = _WriteSession(_config(recipients=[], reported_through=None))
    before = datetime.datetime.now(datetime.UTC)
    _run(
        opportunity_link_digest.set_recipients(
            session, recipients=["Amy@BYU.edu"], actor_user_id=7
        )
    )
    assert session.row.reported_through is not None
    assert session.row.reported_through >= before


def test_editing_a_live_list_keeps_the_window(monkeypatch, mail_ready):
    mark = datetime.datetime(2026, 9, 22, 0, 30, tzinfo=datetime.UTC)
    session = _WriteSession(_config(recipients=["amy@byu.edu"], reported_through=mark))
    _run(
        opportunity_link_digest.set_recipients(
            session, recipients=["amy@byu.edu", "tanya@byu.edu"], actor_user_id=7
        )
    )
    assert session.row.reported_through == mark


# =============================================================================
# 3. The digest itself
# =============================================================================


def _config(*, recipients, reported_through=None):
    row = OpportunityLinkDigestConfig(id=1, recipients=list(recipients))
    row.reported_through = reported_through
    row.updated_by_user_id = None
    row.updated_at = None
    return row


def _link(link_id: int, role_type: str = "internship") -> OpportunityLink:
    return OpportunityLink(
        opportunity_link_id=link_id,
        alumni_id=1,
        is_own_company=False,
        company_name="Acme Capital",
        url=GOOD_URL,
        details="Call Dana Whitcomb on 555 0100",
        role_type=role_type,
        status="pending",
        source="survey",
        submitted_at=datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=3),
    )


class _DigestSession:
    """The config row, the survey postings in the window, the pending count, and
    a real-enough digest ledger (claims and releases)."""

    def __init__(self, *, recipients, links, pending_total=0, reported_through=None):
        self.row = _config(recipients=recipients, reported_through=reported_through)
        self.links = links
        self.pending = pending_total
        self.claims: list[int] = []
        self.commits = 0
        self.window = None

    async def scalar(self, stmt):
        sql = str(stmt)
        if "INSERT INTO opportunity_link_digest_send_log" in sql:
            claim_id = len(self.claims) + 1
            self.claims.append(claim_id)
            return claim_id
        if "FROM opportunity_link_digest_config" in sql:
            return self.row
        return self.pending

    async def execute(self, stmt):
        sql = str(stmt)
        if sql.startswith("DELETE FROM opportunity_link_digest_send_log"):
            claim_id = dict(stmt.compile().params)["digest_send_id_1"]
            self.claims.remove(claim_id)
            return None
        self.window = dict(stmt.compile().params)
        links = self.links
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(links)))

    async def commit(self):
        self.commits += 1


def _outbox(monkeypatch, *, status=200, transport_error=False):
    box: dict[str, list] = {"mail": [], "slack": [], "engineer_mail": []}

    async def _post(url, *, api_key, payload, timeout):
        if transport_error:
            raise TimeoutError("resend never answered")
        box["mail"].append(payload)
        return SimpleNamespace(is_success=200 <= status < 300, status_code=status)

    async def _slack(subject, intro, rows, *, purpose=None, summary=None):
        box["slack"].append({"purpose": purpose, "summary": summary})
        return True

    async def _engineer_mail(*a, **kw):
        box["engineer_mail"].append(a)
        return True

    monkeypatch.setattr(mailer, "post_json", _post)
    monkeypatch.setattr(failure_alert, "_send_slack", _slack)
    monkeypatch.setattr(failure_alert, "_send_email", _engineer_mail)
    return box


def test_one_email_per_recipient_each_counted_once(monkeypatch, mail_ready):
    session = _DigestSession(
        recipients=["amy@byu.edu", "tanya@byu.edu"],
        links=[_link(1), _link(2, "full_time")],
        pending_total=5,
    )
    outbox = _outbox(monkeypatch)
    assert _run(opportunity_link_alert.send_digest(session)) is True

    assert [m["to"] for m in outbox["mail"]] == [["amy@byu.edu"], ["tanya@byu.edu"]]
    # One ledger row per e-mail that went out: exactly the survey budget's share.
    assert session.claims == [1, 2]
    # The engineer hears about it on Slack (free), NOT in the alert mailbox,
    # which would cost one more e-mail out of the survey's quota.
    assert len(outbox["slack"]) == 1
    assert outbox["slack"][0]["purpose"] == failure_alert.SUBMISSION
    assert outbox["engineer_mail"] == []
    assert session.row.reported_through is not None


def test_the_digest_is_silent_on_a_quiet_day(monkeypatch, mail_ready):
    """No e-mail, no Slack line, no quota spent -- and the watermark still moves,
    so tomorrow's window is one day wide."""
    session = _DigestSession(recipients=["amy@byu.edu"], links=[])
    outbox = _outbox(monkeypatch)
    assert _run(opportunity_link_alert.send_digest(session)) is False
    assert outbox["mail"] == [] and outbox["slack"] == []
    assert session.claims == []
    assert session.row.reported_through is not None


def test_the_window_runs_from_the_watermark_not_a_fixed_lookback(monkeypatch, mail_ready):
    """Gap-free and repeat-free whatever minute of its hour the cron fires."""
    mark = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=30)
    session = _DigestSession(
        recipients=["amy@byu.edu"], links=[_link(1)], reported_through=mark
    )
    _outbox(monkeypatch)
    before = datetime.datetime.now(datetime.UTC)
    _run(opportunity_link_alert.send_digest(session))

    lower, upper = session.window["submitted_at_1"], session.window["submitted_at_2"]
    assert lower == mark, "starts exactly where the last digest stopped"
    assert upper <= before - opportunity_link_alert.DIGEST_SETTLE + datetime.timedelta(seconds=5)
    assert session.row.reported_through == upper, "and the next one starts here"


def test_the_first_digest_covers_a_full_day_plus_the_cron_jitter(monkeypatch, mail_ready):
    session = _DigestSession(recipients=["amy@byu.edu"], links=[_link(1)])
    _outbox(monkeypatch)
    _run(opportunity_link_alert.send_digest(session))
    lower, upper = session.window["submitted_at_1"], session.window["submitted_at_2"]
    # Hobby fires anywhere in the hour: consecutive runs can be 24h59m apart.
    assert upper - lower >= datetime.timedelta(hours=25)


def test_a_refused_email_is_not_counted_and_the_postings_carry_over(monkeypatch, mail_ready):
    mark = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)
    session = _DigestSession(
        recipients=["amy@byu.edu"], links=[_link(1)], reported_through=mark
    )
    _outbox(monkeypatch, status=422)
    assert _run(opportunity_link_alert.send_digest(session)) is False
    assert session.claims == [], "Resend said no, so no quota was spent"
    assert session.row.reported_through == mark, "tomorrow's digest reports it"


def test_an_unknown_outcome_stays_counted(monkeypatch, mail_ready):
    """A transport failure may still have sent the e-mail; an uncounted e-mail is
    the 429 this exists to prevent, so the claim stays."""
    session = _DigestSession(recipients=["amy@byu.edu"], links=[_link(1)])
    _outbox(monkeypatch, transport_error=True)
    assert _run(opportunity_link_alert.send_digest(session)) is False
    assert session.claims == [1]


def test_the_digest_waits_for_a_survey_send_and_never_overlaps_one(monkeypatch, mail_ready):
    """While the survey holds its send lock the digest does not send; if the lock
    never frees it gives up without moving the watermark."""

    @contextlib.asynccontextmanager
    async def _held():
        yield False

    monkeypatch.setattr(survey_email, "send_lock", _held)
    monkeypatch.setattr(opportunity_link_alert, "_LOCK_WAIT_SECONDS", 0.0)
    session = _DigestSession(recipients=["amy@byu.edu"], links=[_link(1)])
    outbox = _outbox(monkeypatch)
    assert _run(opportunity_link_alert.send_digest(session)) is False
    assert outbox["mail"] == []
    assert session.row.reported_through is None


def test_a_broken_digest_does_not_500_the_cron(monkeypatch, mail_ready):
    class _Broken:
        async def scalar(self, stmt):
            raise RuntimeError("the database is unreachable")

    assert _run(opportunity_link_alert.send_digest(_Broken())) is False


def test_the_staff_email_is_plain_friendly_and_pii_free(monkeypatch, mail_ready):
    session = _DigestSession(
        recipients=["amy@byu.edu"],
        links=[_link(1), _link(2), _link(3, "full_time")],
        pending_total=5,
    )
    outbox = _outbox(monkeypatch)
    _run(opportunity_link_alert.send_digest(session))
    mail = outbox["mail"][0]
    blob = " ".join([mail["subject"], mail["html"], mail["text"]])

    assert mail["subject"] == "3 new job links from alumni to review"
    assert "2 internships and 1 full-time job" in mail["text"]
    assert "5 links are waiting for review" in mail["text"]
    assert "https://finance.alumni.byu.edu/links?status=pending" in mail["text"]
    # Not engineer wording.
    for engineer_word in ("fa-web-api", "Environment", "Build", "production", "->"):
        assert engineer_word not in mail["text"], engineer_word
    # Nothing a member of the public typed, and no alum.
    for forbidden in ("Acme Capital", GOOD_URL, "Dana", "Whitcomb", "555 0100"):
        assert forbidden not in blob, forbidden
    # Staff identity, not the engineer alert sender.
    assert mail["from"] == "BYU Finance Alumni <byufinancealumni@mailing.byu.edu>"


def test_the_staff_email_reads_right_for_one_posting():
    subject, _html, text = opportunity_link_alert.render_staff_digest(
        link_ids=[4], role_types=["both"], pending_total=1
    )
    assert subject == "1 new job link from alumni to review"
    assert "(1 internship or full-time role)" in text
    assert "1 link is waiting for review in total." in text


# =============================================================================
# 4. The console setting
# =============================================================================


def test_recipients_are_trimmed_lowercased_and_deduped_in_order():
    assert clean_recipients([" Amy@BYU.edu ", "tanya@byu.edu", "amy@byu.edu"]) == [
        "amy@byu.edu",
        "tanya@byu.edu",
    ]


@pytest.mark.parametrize(
    "bad",
    [
        ["not-an-email"],
        ["amy@byu.edu, tanya@byu.edu"],  # one box, one mailbox
        ["Amy <amy@byu.edu>"],
        ["amy@byu.edu\nBcc: x@evil.example"],
        [""],
        [42],
        "amy@byu.edu",  # not a list
    ],
)
def test_a_bad_recipient_is_refused(bad):
    with pytest.raises(ValueError):
        clean_recipients(bad)


def test_the_list_is_capped():
    ok = [f"staff{i}@byu.edu" for i in range(MAX_RECIPIENTS)]
    assert len(clean_recipients(ok)) == MAX_RECIPIENTS
    with pytest.raises(ValueError):
        clean_recipients([*ok, "one.more@byu.edu"])


def test_the_update_body_forbids_unknown_fields():
    with pytest.raises(ValidationError):
        OpportunityLinkDigestUpdate(recipients=[], enabled=True)


def test_a_hand_edited_bad_row_drops_only_the_bad_entry():
    assert opportunity_link_digest.normalize(["amy@byu.edu", "garbage", 7]) == [
        "amy@byu.edu"
    ]
    assert opportunity_link_digest.normalize(None) == []


def test_the_migration_cap_matches_the_schema_cap():
    sql = (REPO / "database" / "migrations" / "2026-09-23_opportunity_link_digest.sql").read_text(
        encoding="utf-8"
    )
    assert f"cardinality(recipients) <= {MAX_RECIPIENTS}" in sql


def test_both_new_tables_are_in_the_rls_lockdown_sweep():
    rls = (REPO / "database" / "rls_lockdown.sql").read_text(encoding="utf-8")
    for table in ("opportunity_link_digest_config", "opportunity_link_digest_send_log"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in rls


class _WriteSession:
    """Enough of a session for ``set_recipients`` / ``get_state``."""

    def __init__(self, row=None, actor_email="engineer@byu.edu"):
        self.row = row
        self.actor_email = actor_email
        self.added: list = []
        self.commits = 0

    async def scalar(self, stmt):
        if "opportunity_link_digest_config" in str(stmt):
            return self.row
        return self.actor_email

    def add(self, obj):
        self.added.append(obj)
        if isinstance(obj, OpportunityLinkDigestConfig):
            self.row = obj

    async def commit(self):
        self.commits += 1


def _ctx(*roles: str, user_id: int = 7) -> UserContext:
    return UserContext(
        user_id=user_id,
        auth_user_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        roles=list(roles),
    )


@pytest.fixture
def console(monkeypatch, mail_ready):
    session = _WriteSession(_config(recipients=[]))

    async def _session():
        yield session

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_db_user] = lambda: _ctx("engineer")
    with TestClient(app) as client:
        yield client, session
    app.dependency_overrides.clear()


def test_an_engineer_can_read_the_recipients(console):
    client, _session = console
    body = client.get("/admin/opportunity-link-digest").json()
    assert body["recipients"] == []
    assert body["email_configured"] is True


def test_an_engineer_can_set_the_recipients_and_it_is_audited(console):
    client, session = console
    response = client.put(
        "/admin/opportunity-link-digest",
        json={"recipients": ["Amy@BYU.edu", "tanya@byu.edu", "amy@byu.edu"]},
    )
    assert response.status_code == 200
    assert response.json()["recipients"] == ["amy@byu.edu", "tanya@byu.edu"]
    assert session.row.recipients == ["amy@byu.edu", "tanya@byu.edu"]
    audit = [o for o in session.added if isinstance(o, AuditLog)]
    assert [a.action_type for a in audit] == ["set_opportunity_link_digest_recipients"]
    assert audit[0].old_value == "" and audit[0].new_value == "amy@byu.edu, tanya@byu.edu"
    assert audit[0].user_id == 7


def test_the_service_never_writes_the_engineer_log_itself():
    """Written by the before_flush guard (#199), exactly as for alert_delivery."""
    source = Path(opportunity_link_digest.__file__).read_text(encoding="utf-8")
    assert "EngineerActionLog" not in source


def test_a_bad_list_is_a_422_before_anything_is_written(console):
    client, session = console
    assert (
        client.put("/admin/opportunity-link-digest", json={"recipients": ["nope"]}).status_code
        == 422
    )
    assert session.commits == 0


@pytest.mark.parametrize("role", ["super_admin", "full_access", "student", "view_only"])
def test_only_an_engineer_may_read_or_change_it(monkeypatch, console, role):
    client, session = console
    app.dependency_overrides[get_current_db_user] = lambda: _ctx(role)
    assert client.get("/admin/opportunity-link-digest").status_code == 403
    assert (
        client.put(
            "/admin/opportunity-link-digest", json={"recipients": ["a@byu.edu"]}
        ).status_code
        == 403
    )
    assert session.commits == 0


# =============================================================================
# 5. The cron
# =============================================================================


def test_the_cron_is_registered_once_per_utc_offset():
    crons = json.loads((REPO / "vercel.json").read_text(encoding="utf-8"))["crons"]
    digest = sorted(
        c["schedule"] for c in crons if c["path"] == "/opportunity-links/cron/digest"
    )
    assert digest == ["0 0 * * *", "0 1 * * *"]


def _utc(y, mo, d, h, mi=0):
    return datetime.datetime(y, mo, d, h, mi, tzinfo=datetime.UTC)


def _firings(local_evening: datetime.date):
    """Every minute the two Hobby entries could fire in, for the UTC day that
    follows ``local_evening`` (00:00-01:59 UTC)."""
    nxt = local_evening + datetime.timedelta(days=1)
    for hour in (0, 1):
        for minute in range(60):
            yield hour, _utc(nxt.year, nxt.month, nxt.day, hour, minute)


@pytest.mark.parametrize(
    "evening",
    [
        datetime.date(2026, 7, 1),  # summer, MDT
        datetime.date(2026, 12, 1),  # winter, MST
        datetime.date(2026, 10, 31),  # the evening before fall-back (still MDT)
        datetime.date(2026, 11, 1),  # fall-back day: clocks went back at 2am
        datetime.date(2027, 3, 13),  # the evening before spring-forward (MST)
        datetime.date(2027, 3, 14),  # spring-forward day: clocks went on at 2am
    ],
)
def test_exactly_one_of_the_two_entries_is_the_6pm_run(evening):
    """Whatever minute of its hour each entry fires, exactly ONE entry's calls
    are all due and the other's are all no-ops -- so there is one digest per
    evening, at 6pm Mountain, including both DST changeover days."""
    due_by_entry = {0: set(), 1: set()}
    for hour, when in _firings(evening):
        due_by_entry[hour].add(opportunity_link_alert.digest_due(when))
        if opportunity_link_alert.digest_due(when):
            assert opportunity_link_alert.local_digest_date(when) == evening
    # One entry is due at every minute of its hour, the other at none.
    assert {frozenset(v) for v in due_by_entry.values()} == {
        frozenset({True}),
        frozenset({False}),
    }


def test_which_entry_is_live_follows_daylight_saving():
    assert opportunity_link_alert.digest_due(_utc(2026, 11, 1, 0, 30))  # Oct 31, 6:30pm MDT
    assert not opportunity_link_alert.digest_due(_utc(2026, 11, 1, 1, 30))  # 7:30pm MDT
    assert not opportunity_link_alert.digest_due(_utc(2026, 11, 2, 0, 30))  # Nov 1, 5:30pm MST
    assert opportunity_link_alert.digest_due(_utc(2026, 11, 2, 1, 30))  # 6:30pm MST
    assert not opportunity_link_alert.digest_due(_utc(2027, 3, 14, 0, 30))  # Mar 13, 5:30pm MST
    assert opportunity_link_alert.digest_due(_utc(2027, 3, 14, 1, 30))  # 6:30pm MST
    assert opportunity_link_alert.digest_due(_utc(2027, 3, 15, 0, 30))  # Mar 14, 6:30pm MDT
    assert not opportunity_link_alert.digest_due(_utc(2027, 3, 15, 1, 30))  # 7:30pm MDT


def test_the_first_run_lookback_covers_the_longest_gap_between_6pm_runs():
    """25h across fall-back, plus up to an hour of firing jitter."""
    assert opportunity_link_alert.DIGEST_LOOKBACK_HOURS >= 26


def test_a_second_call_the_same_evening_sends_nothing(monkeypatch, mail_ready):
    """A retried or duplicated cron delivery: no second e-mail, no second charge
    to the survey budget -- even when a new posting arrived in between."""
    session = _DigestSession(recipients=["amy@byu.edu"], links=[_link(1)])
    outbox = _outbox(monkeypatch)
    assert _run(opportunity_link_alert.send_digest(session)) is True
    assert session.row.last_digest_on == opportunity_link_alert.local_digest_date()

    session.links = [_link(2)]
    assert _run(opportunity_link_alert.send_digest(session)) is False
    assert len(outbox["mail"]) == 1
    assert session.claims == [1]


def test_a_quiet_evening_also_counts_as_the_days_run(monkeypatch, mail_ready):
    session = _DigestSession(recipients=["amy@byu.edu"], links=[])
    _outbox(monkeypatch)
    _run(opportunity_link_alert.send_digest(session))
    assert session.row.last_digest_on == opportunity_link_alert.local_digest_date()


def test_a_failed_evening_can_be_retried(monkeypatch, mail_ready):
    session = _DigestSession(recipients=["amy@byu.edu"], links=[_link(1)])
    _outbox(monkeypatch, status=500)
    _run(opportunity_link_alert.send_digest(session))
    assert session.row.last_digest_on is None


@pytest.fixture
def cron_client(monkeypatch):
    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_the_cron_takes_a_get_with_the_shared_secret(monkeypatch, cron_client):
    """Vercel Cron calls with GET and ``Authorization: Bearer $CRON_SECRET``."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    get_settings.cache_clear()
    calls: list = []

    async def _send(session):
        calls.append(1)
        return True

    monkeypatch.setattr(opportunity_link_alert, "send_digest", _send)
    monkeypatch.setattr(opportunity_link_alert, "digest_due", lambda: True)
    ok = cron_client.get(
        "/opportunity-links/cron/digest", headers={"Authorization": "Bearer s3cret"}
    )
    assert ok.status_code == 200 and ok.json() == {"sent": True}
    assert cron_client.get("/opportunity-links/cron/digest").status_code == 401
    assert (
        cron_client.get(
            "/opportunity-links/cron/digest", headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )
    assert calls == [1], "a refused call never reaches the digest"
    get_settings.cache_clear()


def test_the_off_hour_call_is_a_200_no_op(monkeypatch, cron_client):
    """The twin entry's call lands at 5pm or 7pm Mountain: answered, not failed,
    and never reaches the digest."""
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    get_settings.cache_clear()
    calls: list = []

    async def _send(session):
        calls.append(1)
        return True

    monkeypatch.setattr(opportunity_link_alert, "send_digest", _send)
    monkeypatch.setattr(opportunity_link_alert, "digest_due", lambda: False)
    response = cron_client.get(
        "/opportunity-links/cron/digest", headers={"Authorization": "Bearer s3cret"}
    )
    assert response.status_code == 200
    assert response.json() == {"sent": False}
    assert calls == []
    get_settings.cache_clear()

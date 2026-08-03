"""Correctness guards for the survey send system (post-incident, 2026-08-03).

Background. Alumni were sent the survey by hand from the console on Thu
2026-07-30. On Sun 2026-08-02 a second, identical email went out that nobody had
scheduled. `send_campaign` called the raw sender WITHOUT the callback that
writes `survey_send_log`, so the manual send emailed a whole cohort and recorded
nothing — and that table is the scheduler's only double-send guard, so the cron
concluded the cohort had never had its initial and sent it again.

The audit around it found more. Each section below pins one of the fixes. The
process lesson is in `test_every_send_is_recorded_in_the_send_log`: the suite
had 1330 passing tests and still shipped the bug, because only the CRON path
ever asserted on `survey_send_log`. That test is parametrized over BOTH call
sites on purpose — it is what stops the two senders diverging again.
"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from app.core.dropdowns import STATUS_LABELS, SUPPRESSED_CONTACT_STATUS_LABELS
from app.core.errors import ServiceError
from app.services import survey_email, survey_schedule
from tests.survey_fakes import CommitFailed, SendLogSession, audits

_TODAY = datetime.date(2026, 8, 3)
_YEAR = 2000


class _Settings:
    survey_token_secret = "correctness-secret"
    survey_from_email = "test@jakegunnell.com"
    survey_from_name = "BYU Finance Alumni"
    survey_app_base_url = "https://finance.alumni.byu.edu"
    resend_api_key = "re_test_key"
    survey_usage_baseline_at = None
    survey_usage_baseline_today = 0
    survey_usage_baseline_month = 0


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _Settings()
    monkeypatch.setattr(survey_email, "get_settings", lambda: settings)
    monkeypatch.setattr(survey_schedule, "_today", lambda: _TODAY)
    return settings


def _rcpts(ids, email=None):
    return [
        survey_email.Recipient(
            i, f"A{i}", email or f"a{i}@example.com", (("Company", "X"),)
        )
        for i in ids
    ]


def _sql(stmt):
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


# ============================================================ fix 1: suppress ==
#
# Never email a suppressed alum. `_load_recipients` used to call
# `build_alumni_query(graduation_year=...)` and nothing else, though the
# repository has supported `deceased` and `status_label` filters all along. On
# dev, 5 alumni with `deceased = true` AND the `Deceased` status label passed
# every eligibility predicate; only the `@example.com` placeholder filter
# happened to stop them. On prod, with real addresses, a "confirm your
# information" email listing a dead person's whole record reaches a live inbox —
# in practice their surviving spouse's.


def test_suppression_list_is_exactly_deceased_and_do_not_contact():
    assert SUPPRESSED_CONTACT_STATUS_LABELS == ("Deceased", "Do Not Contact")
    # Every suppressed label must be a REAL status label, or the predicate
    # silently matches nothing.
    for label in SUPPRESSED_CONTACT_STATUS_LABELS:
        assert label in STATUS_LABELS


@pytest.mark.parametrize("label", ["Lost Contact", "Retired", "Inactive"])
def test_reconnectable_labels_stay_eligible(label):
    """Jake's call, 2026-08-03: only Deceased and Do Not Contact suppress.

    "Lost Contact" means we WANT to reconnect and the survey is the tool for it;
    retired and inactive alumni are still ours to survey. Widening this list
    silently un-surveys whole populations, so it is pinned here."""
    assert label in STATUS_LABELS  # a real label...
    assert label not in SUPPRESSED_CONTACT_STATUS_LABELS  # ...that is NOT suppressed
    assert label not in _sql(survey_email.eligible_alumni_query(_YEAR))


def test_eligible_query_excludes_deceased_and_suppressed_labels():
    sql = _sql(survey_email.eligible_alumni_query(_YEAR))
    # The flag column.
    assert "deceased IS false" in sql
    # ...and the status LABELS, which are a separate join set on independently:
    # an alum can carry the Deceased label without the flag, and vice versa.
    assert "NOT (EXISTS" in sql
    assert "Deceased" in sql and "Do Not Contact" in sql
    assert "status_label" in sql


def test_suppression_is_a_sql_predicate_not_a_python_filter():
    """8,000+ alumni and a performance budget: the exclusion has to run in
    Postgres as a correlated NOT EXISTS over the status-label join, never as a
    post-query loop."""
    sql = " ".join(_sql(survey_email.eligible_alumni_query(_YEAR)).split())
    # The NOT EXISTS that carries the suppressed labels also has to be the one
    # correlated to the alumnus and joined to status_label.
    suppression = next(
        (
            fragment
            for fragment in sql.split("NOT (EXISTS (SELECT")[1:]
            if "Do Not Contact" in fragment
        ),
        None,
    )
    assert suppression is not None, sql
    assert "FROM alumni_status_labels JOIN status_labels" in suppression
    assert "status_labels.status_label_name" in suppression
    assert "alumni_status_labels.alumni_id = alumni.alumni_id" in suppression


def test_suppress_labels_is_opt_in_and_off_by_default():
    """The kwarg exists so a future "who would receive this?" preview uses the
    IDENTICAL predicate — but it must not change any existing list/export."""
    from app.repositories.alumni import build_alumni_query

    assert "Do Not Contact" not in _sql(build_alumni_query(graduation_year=_YEAR))
    assert "Do Not Contact" in _sql(
        build_alumni_query(
            graduation_year=_YEAR, suppress_labels=SUPPRESSED_CONTACT_STATUS_LABELS
        )
    )


# ============================================== fix 2: completion is evidence ==
#
# See tests/test_survey_scheduler.py for the run-level assertions
# (`test_past_last_window_still_sends_to_anyone_never_emailed` and friends).
# These pin the stage arithmetic those rest on.


def test_ceiling_is_total_where_the_window_runs_out():
    # Windows (what `stage_for` reports) run out at day 21...
    assert survey_email.stage_for(20) == 2
    assert survey_email.stage_for(21) is None
    # ...but the CEILING never does: past every window all three stages are
    # permitted, so stragglers from any stage can still be finished.
    assert survey_email.ceiling_stage_for(0) == 0
    assert survey_email.ceiling_stage_for(7) == 1
    assert survey_email.ceiling_stage_for(14) == 2
    assert survey_email.ceiling_stage_for(21) == 2
    assert survey_email.ceiling_stage_for(365) == 2
    # Before the start only the initial is permitted.
    assert survey_email.ceiling_stage_for(-3) == 0


def test_select_stage_targets_picks_the_lowest_stage_still_owed(fake_settings):
    session = SendLogSession()
    session.seed_sent(_YEAR, 0, [1, 2, 3])
    session.seed_sent(_YEAR, 1, [1, 2])
    recipients = _rcpts([1, 2, 3])
    stage, targets = asyncio.run(
        survey_email.select_stage_targets(
            session, graduation_year=_YEAR, recipients=recipients, max_stage=2
        )
    )
    # Stage 2's window is open, but stage 1 still owes alum 3 — finish that first.
    assert stage == 1
    assert [r.alumni_id for r in targets] == [3]


def test_select_stage_targets_returns_none_when_nothing_is_owed(fake_settings):
    session = SendLogSession()
    for stage in (0, 1, 2):
        session.seed_sent(_YEAR, stage, [1, 2])
    stage, targets = asyncio.run(
        survey_email.select_stage_targets(
            session, graduation_year=_YEAR, recipients=_rcpts([1, 2]), max_stage=2
        )
    )
    # (None, []) is the ONLY honest basis for completing a campaign.
    assert stage is None
    assert targets == []


def test_select_stage_targets_never_exceeds_the_ceiling(fake_settings):
    session = SendLogSession()
    session.seed_sent(_YEAR, 0, [1])
    stage, targets = asyncio.run(
        survey_email.select_stage_targets(
            session, graduation_year=_YEAR, recipients=_rcpts([1]), max_stage=0
        )
    )
    # Day 0: the reminders are genuinely not due, so nothing is owed YET — which
    # is not the same as "done" (the run keeps the campaign active).
    assert stage is None and targets == []


# ============================================ fix 3: one send-and-log path ====


def _patch_send(monkeypatch, *, recipients, batch, schedules=None, allowance=None):
    async def fake_load(session, year):
        return recipients

    async def fake_due(session, today):
        return schedules or []

    async def fake_allowance(session):
        return allowance

    monkeypatch.setattr(survey_email, "_load_recipients", fake_load)
    monkeypatch.setattr(survey_email, "_send_batch", batch)
    monkeypatch.setattr(survey_schedule, "_load_schedules_due", fake_due)
    monkeypatch.setattr(survey_schedule, "_run_allowance", fake_allowance)


def _sched(year, start, status="scheduled"):
    return SimpleNamespace(
        survey_schedule_id=year,
        graduation_year=year,
        start_date=start,
        status=status,
        last_run_at=None,
        created_at=None,
        created_by_user_id=None,
        paused_at=None,
        paused_from_status=None,
    )


def _run_manual(session):
    return asyncio.run(
        survey_email.send_campaign(
            session, graduation_year=_YEAR, actor_user_id=1, dry_run=False
        )
    )


def _run_cron(session):
    return asyncio.run(survey_schedule.run_due_schedules(session, actor_user_id=1))


@pytest.mark.parametrize("call_site", ["manual", "cron"])
def test_every_send_is_recorded_in_the_send_log(call_site, fake_settings, monkeypatch):
    """THE regression test for the incident, asserted at BOTH call sites.

    The old suite only ever checked `survey_send_log` on the cron path, so the
    manual path could email a whole cohort and record nothing while every test
    stayed green. Whatever else changes, both senders must land here."""
    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    _patch_send(
        monkeypatch,
        recipients=_rcpts([1, 2, 3]),
        batch=batch,
        schedules=[_sched(_YEAR, _TODAY)],
    )

    session = SendLogSession()
    (_run_manual if call_site == "manual" else _run_cron)(session)

    assert sorted(sent_to) == ["a1@example.com", "a2@example.com", "a3@example.com"]
    # One row per email, at a REAL stage (never a synthetic "manual" -1, which
    # would satisfy the unique constraint alongside a stage-0 cron row).
    assert sorted(session.send_log) == [
        (_YEAR, 1, 0),
        (_YEAR, 2, 0),
        (_YEAR, 3, 0),
    ]
    # The audit row is still written for the trail (it is just no longer the
    # ledger — see get_send_usage).
    assert [a.action_type for a in audits(session)] == ["send_survey"]


def test_manual_send_then_cron_does_not_re_email_the_cohort(
    fake_settings, monkeypatch
):
    """The incident itself, end to end.

    Manual send on Thursday, cron on Sunday. Before the fix the manual send left
    no log rows, so the cron saw a cohort with no initial and sent it again."""
    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    _patch_send(
        monkeypatch,
        recipients=_rcpts([1, 2, 3]),
        batch=batch,
        schedules=[_sched(_YEAR, _TODAY)],
    )

    session = SendLogSession()
    _run_manual(session)
    assert len(sent_to) == 3

    summary = _run_cron(session)
    assert len(sent_to) == 3  # not one more email
    assert summary.ran[0].sent == 0
    assert len(session.send_log) == 3


def test_no_stage_minus_one(fake_settings, monkeypatch):
    """A manual send must claim a REAL stage.

    `survey_send_log` is UNIQUE on (year, alumni, stage); a synthetic `stage=-1`
    would let a manual send AND a stage-0 cron send both succeed for the same
    alum, which is exactly the incident."""

    async def batch(emails):
        return (None, None)

    _patch_send(monkeypatch, recipients=_rcpts([1]), batch=batch)
    session = SendLogSession()
    _run_manual(session)
    assert {stage for _y, _a, stage in session.send_log} == {survey_email.STAGE_INITIAL}


def test_manual_send_stage_follows_the_schedule_when_there_is_one(
    fake_settings, monkeypatch
):
    """With a schedule mid-cadence, a manual send resolves its stage the way the
    cron does rather than blindly re-sending the initial."""

    async def batch(emails):
        return (None, None)

    _patch_send(monkeypatch, recipients=_rcpts([1, 2]), batch=batch)
    session = SendLogSession()
    session.seed_sent(_YEAR, 0, [1, 2])  # the initial already went out
    # A schedule that started 8 days ago -> the 1-week reminder window.
    session._queue.append(
        SimpleNamespace(
            scalar_one_or_none=lambda: _TODAY - datetime.timedelta(days=8)
        )
    )
    _run_manual(session)
    assert sorted(session.send_log) == [
        (_YEAR, 1, 0),
        (_YEAR, 1, 1),
        (_YEAR, 2, 0),
        (_YEAR, 2, 1),
    ]


def test_raw_sender_is_private():
    """Nothing outside the module may reach Resend without logging.

    Patching the one manual call site would have left the same trap for the next
    caller, so the sender itself is private and `send_survey_stage` is the only
    door."""
    assert not hasattr(survey_email, "send_recipients")
    assert hasattr(survey_email, "_send_batch")
    assert hasattr(survey_email, "send_survey_stage")


# ---- failure direction -------------------------------------------------------


def test_rate_limit_releases_its_claim_so_those_people_still_get_the_email(
    fake_settings, monkeypatch
):
    """A 429 is a DEFINITIVE rejection — Resend queued nothing — so the claim is
    released and the throttled recipients go out on the next run."""
    state = {"n": 0}

    async def batch(emails):
        state["n"] += 1
        if state["n"] == 1:
            return (None, None)
        raise survey_email.ResendRateLimited(retry_after=30)

    _patch_send(monkeypatch, recipients=_rcpts(range(1, 151)), batch=batch)
    session = SendLogSession()
    result = _run_manual(session)

    assert result.sent == 100
    assert result.retry_after_seconds == 30
    assert len(session.send_log) == 100  # NOT 150 — the second claim was undone
    assert session.logged(_YEAR, 0) == set(range(1, 101))


def test_transport_failure_keeps_its_claim(fake_settings, monkeypatch):
    """The one failure we cannot undo.

    A timeout / dropped connection can fire AFTER Resend accepted and queued the
    batch. We do not know, so the claim STAYS: those recipients are treated as
    possibly-delivered and are never emailed a second time. "Possibly missed" is
    the right way for an irreversible side effect to fail — a missed alum can be
    re-sent deliberately, a duplicate cannot be recalled."""
    state = {"n": 0}

    async def batch(emails):
        state["n"] += 1
        if state["n"] == 1:
            return (None, None)
        raise survey_email.ResendDeliveryUnknown("Could not reach the email service.")

    _patch_send(monkeypatch, recipients=_rcpts(range(1, 151)), batch=batch)
    session = SendLogSession()
    result = _run_manual(session)

    assert result.sent == 100  # only what we KNOW landed is counted
    assert len(session.send_log) == 150  # ...but all 150 are claimed
    assert result.retry_after_seconds is None


def test_definitive_rejection_releases_its_claim(fake_settings, monkeypatch):
    """A non-2xx answer means Resend replied, and replied no — nothing queued —
    so the claim is released and nobody is stranded."""
    state = {"n": 0}

    async def batch(emails):
        state["n"] += 1
        if state["n"] == 1:
            return (None, None)
        raise ServiceError("Resend rejected the send (HTTP 422).")

    _patch_send(monkeypatch, recipients=_rcpts(range(1, 151)), batch=batch)
    session = SendLogSession()
    result = _run_manual(session)

    assert result.sent == 100
    assert len(session.send_log) == 100


def test_a_delivered_batch_is_committed_even_though_a_later_one_failed(
    fake_settings, monkeypatch
):
    """The hole this closes: `send_recipients` caught ONLY `ResendRateLimited`,
    so any other ServiceError propagated and abandoned the run un-logged AFTER
    Resend had accepted the earlier batches — and the next run re-emailed those
    people."""
    state = {"n": 0}

    async def batch(emails):
        state["n"] += 1
        if state["n"] == 1:
            return (None, None)
        raise ServiceError("Resend rejected the send (HTTP 500).")

    _patch_send(monkeypatch, recipients=_rcpts(range(1, 151)), batch=batch)
    session = SendLogSession()
    result = _run_manual(session)

    # The delivered 100 are logged and counted, not lost with the exception.
    assert result.sent == 100
    assert session.logged(_YEAR, 0) == set(range(1, 101))
    assert "sent=100" in audits(session)[0].new_value


def test_a_total_failure_is_raised_not_reported_as_a_clean_zero(
    fake_settings, monkeypatch
):
    """Nothing went out and something is genuinely wrong (bad key, unverified
    domain). Reporting "sent 0" would read like an empty cohort."""

    async def batch(emails):
        raise ServiceError("Resend rejected the send (HTTP 403).")

    _patch_send(monkeypatch, recipients=_rcpts([1, 2]), batch=batch)
    session = SendLogSession()
    with pytest.raises(ServiceError):
        _run_manual(session)
    assert session.send_log == set()  # claim released — nobody is stranded
    assert "failed=True" in audits(session)[0].new_value


def test_a_failed_claim_commit_sends_nothing(fake_settings, monkeypatch):
    """A session that can actually raise.

    The suite's older fakes could not express a failed commit, which is part of
    why these bugs survived. If the claim cannot be committed, the Resend call
    must never happen — the alternative is emailing people we have no record of.
    """
    calls = []

    async def batch(emails):  # pragma: no cover - must never be reached
        calls.append(emails)
        return (None, None)

    _patch_send(monkeypatch, recipients=_rcpts([1, 2]), batch=batch)
    session = SendLogSession(fail_commit_after=0)  # the very first commit fails
    with pytest.raises(CommitFailed):
        _run_manual(session)
    assert calls == []


# ================================================= fix 4: usage from the log ==


def test_survey_daily_cap_is_gone_from_config():
    """It was read by nothing in `app/` and misled anyone auditing config — the
    real budget is the admin-editable `survey_send_config` row plus Resend's
    429."""
    from app.core.config import Settings

    assert "survey_daily_cap" not in Settings.model_fields


# ================================================== fix 5: one email, one send ==


def test_recipients_sharing_an_email_get_one_message(fake_settings, monkeypatch):
    """Three alumni rows share `gunnjake@byu.edu` on dev today.

    Each message carries ~19 fields of THAT alum's record (both emails, spouse
    names, residence, employer, title, LinkedIn) plus a live signed token that
    lets the holder EDIT that record. Un-deduped, one inbox received three
    people's profiles with write access to all three. Real triggers: spouses
    sharing a household address, a reassigned address, a data-entry slip, or a
    genuine duplicate record."""
    sent_to = []

    async def batch(emails):
        sent_to.extend(e["to"][0] for e in emails)
        return (None, None)

    shared = _rcpts([7, 3, 9], email="shared@byu.edu")
    cohort = sorted(shared + _rcpts([4]), key=lambda r: r.alumni_id)
    _patch_send(monkeypatch, recipients=cohort, batch=batch)

    session = SendLogSession()
    result = _run_manual(session)

    assert sorted(sent_to) == ["a4@example.com", "shared@byu.edu"]
    assert result.sent == 2
    # Deterministic keeper: the lowest alumni_id, so a re-run picks the SAME
    # person rather than rotating whose record leaks.
    assert session.logged(_YEAR, 0) == {3, 4}


def test_dedupe_is_case_and_whitespace_insensitive():
    kept, dropped = survey_email.dedupe_by_email(
        [
            survey_email.Recipient(1, "A", "Jake@BYU.edu", ()),
            survey_email.Recipient(2, "B", " jake@byu.edu ", ()),
            survey_email.Recipient(3, "C", "other@byu.edu", ()),
        ]
    )
    assert [r.alumni_id for r in kept] == [1, 3]
    assert [r.alumni_id for r in dropped] == [2]


def test_dropped_duplicates_are_surfaced_not_silent(fake_settings, monkeypatch, caplog):
    async def batch(emails):
        return (None, None)

    _patch_send(
        monkeypatch, recipients=_rcpts([1, 2], email="shared@byu.edu"), batch=batch
    )
    session = SendLogSession()
    with caplog.at_level("WARNING"):
        _run_manual(session)
    assert "share an email address" in caplog.text
    # ...and it is on the audit trail too.
    assert "duplicate_emails=1" in audits(session)[0].new_value


# ====================================== fix 6: rejected replies + ordering ====


def test_a_rejected_response_does_not_suppress_the_alum():
    """A rejected response is one staff THREW AWAY — nothing was written to the
    record — so the alum has effectively not replied and must stay surveyable.
    Counting it silenced them for 365 days."""
    assert survey_email.RESPONDED_STATUSES == ("pending", "applied")
    sql = _sql(survey_email.eligible_alumni_query(_YEAR))
    assert "'pending'" in sql and "'applied'" in sql
    assert "rejected" not in sql


class _CaptureSession:
    def __init__(self):
        self.stmts = []

    async def execute(self, stmt):
        self.stmts.append(stmt)
        return SimpleNamespace(all=lambda: [])


def test_responded_count_uses_the_identical_status_filter():
    """`list_graduation_years` renders its count as "N replied" in the console.
    With a status-blind filter a rejected submission was BOTH un-emailed and
    reported as complete. Its docstring already claimed the two matched; now
    they do."""
    session = _CaptureSession()
    asyncio.run(survey_email.list_graduation_years(session))
    responded_sql = _sql(session.stmts[1])
    assert "'pending'" in responded_sql and "'applied'" in responded_sql
    assert "rejected" not in responded_sql


def test_recipients_are_ordered_so_truncation_is_reproducible():
    """`build_alumni_query` has no ORDER BY, so any `limit` / budget slice took
    an arbitrary, non-reproducible subset — which made "run Send again to
    continue" untrue and truncation impossible to characterise afterwards."""
    assert "ORDER BY alumni.alumni_id" in _sql(
        survey_email.eligible_alumni_query(_YEAR)
    )

"""The account-wide send budget binds the MANUAL send too (#417).

THE BUG
-------
`survey_schedule._run_allowance` — the configured daily/monthly limit minus what
`survey_send_log` says has gone out — was read in exactly one place: the cron
body. The console's manual send never asked. `POST /survey/campaigns/{year}/send`
defaults its `limit` to None, the console sends none, and `send_campaign` handed
that straight to the sender. So "Send now" on a large cohort emailed the ENTIRE
eligible stage in one call: past the cap, past whatever the cron had already
spent that day, into real alumni inboxes, unrecallable — with the console's own
daily/monthly tally sitting on the same screen describing a limit that only one
of the two senders obeyed.

WHY THIS FILE EXISTS AT ALL
---------------------------
The absence of exactly these assertions is why the bug survived. The suite tested
the cap thoroughly — but only ever through `run_due_schedules`
(`test_survey_scheduler.py::test_cap_limits_how_many_go_out_this_run` and
friends), every one of them monkeypatching `_run_allowance` and asserting the
CRON respected it. Nothing ever pressed the console's button and asked how many
emails came out. So the tests below drive `send_campaign` — the real console
path, the one with `limit=None` — and count what reached Resend.

WHAT IS PINNED
--------------
* a manual send with NO limit cannot exceed the remaining allowance;
* an explicit limit BELOW the budget is honoured, and one ABOVE it is clamped;
* a zero budget sends nothing, and says so (409) instead of a clean "sent 0";
* a dry run is unaffected in the way that matters — it writes and sends nothing;
* the cron's budget is not spent twice by the gate now living further down.

The last one is the risk the fix itself introduced: the cron reads the allowance,
decrements it per year and passes the remainder as `limit`, and the sender now
re-reads the same budget. If those compounded, the cron would send half of what
it is configured to.

The session is `survey_fakes.SendLogSession`, which honours the real send-log
unique key, so "how many emails went out" is counted from the claims that
actually landed rather than from a return value.
"""

import asyncio
import datetime
from types import SimpleNamespace

import pytest

from app.core.errors import ConflictError
from app.schemas.survey import SurveySendConfigItem, SurveyUsage
from app.services import survey_email, survey_schedule
from tests.survey_fakes import SendLogSession

_YEAR = 1900
_TODAY = datetime.date(2026, 8, 7)


class _Settings:
    survey_token_secret = "send-budget-secret"
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


def _rcpts(n: int) -> list[survey_email.Recipient]:
    return [
        survey_email.Recipient(i, f"Alum{i}", f"a{i}@example.com", (("Company", "X"),))
        for i in range(1, n + 1)
    ]


def _sched(year=_YEAR, start=None, status="active", cycle_seq=1):
    return SimpleNamespace(
        survey_schedule_id=year,
        graduation_year=year,
        start_date=start or _TODAY,
        status=status,
        last_run_at=None,
        created_at=None,
        created_by_user_id=None,
        paused_at=None,
        paused_from_status=None,
        cycle_seq=cycle_seq,
    )


class _World:
    """The console's Send button, the cron, and a count of what Resend got."""

    def __init__(self, monkeypatch, *, cohort, allowance, schedules=None):
        self.emailed: list[str] = []
        self.session = SendLogSession()

        async def batch(emails):
            self.emailed.extend(e["to"][0] for e in emails)
            return (None, None)

        async def load(session, year):
            return _rcpts(cohort)

        async def due(session, today):
            return schedules or []

        async def run_allowance(session):
            # The number under test. Patched at the ONE place it is defined, so
            # both the cron's read and the sender's re-read see it — which is
            # also how the double-count question below can be asked at all.
            return allowance

        monkeypatch.setattr(survey_email, "_send_batch", batch)
        monkeypatch.setattr(survey_email, "_load_recipients", load)
        monkeypatch.setattr(survey_schedule, "_load_schedules_due", due)
        monkeypatch.setattr(survey_schedule, "_run_allowance", run_allowance)

    # -- actions -------------------------------------------------------------

    def send(self, *, dry_run=False, limit=None):
        """Exactly what the console's Send button does."""
        return asyncio.run(
            survey_email.send_campaign(
                self.session,
                graduation_year=_YEAR,
                actor_user_id=1,
                dry_run=dry_run,
                limit=limit,
            )
        )

    def cron(self):
        return asyncio.run(
            survey_schedule.run_due_schedules(self.session, actor_user_id=1)
        )


# ================================================ a manual send with NO limit ==


def test_a_manual_send_with_no_limit_cannot_exceed_the_daily_allowance(
    fake_settings, monkeypatch
):
    """THE bug, stated as directly as it can be.

    The console sends no `limit` at all. Before the fix that meant "uncapped" and
    the whole cohort was emailed in one call. 40 people, 10 emails of budget: ten
    emails, not forty."""
    world = _World(monkeypatch, cohort=40, allowance=10)

    result = world.send()

    assert len(world.emailed) == 10
    assert result.sent == 10
    # And the send log — the thing that decides who is emailed next time — holds
    # exactly those ten, so the other thirty are still owed their initial.
    assert len(world.session.send_log) == 10
    assert result.remaining == 30


def test_the_console_is_told_the_budget_is_what_cut_the_send_short(
    fake_settings, monkeypatch
):
    """"10 of 40 sent, budget exhausted" has to be sayable from the result.

    Without it a short send is indistinguishable from a broken one, and the
    operator's next move (press Send again — which does nothing) is the wrong
    one."""
    world = _World(monkeypatch, cohort=40, allowance=10)

    result = world.send()

    assert result.budget_limited is True
    assert result.budget_remaining == 0
    assert (result.sent, result.total_recipients) == (10, 40)


def test_an_unlimited_send_is_still_unlimited_when_the_cap_is_off(
    fake_settings, monkeypatch
):
    """The non-vacuity control. `allowance=None` means the cap is switched OFF in
    the console, and the gate must then be invisible — otherwise this whole
    change would read as a pass while quietly throttling every send to zero."""
    world = _World(monkeypatch, cohort=40, allowance=None)

    result = world.send()

    assert len(world.emailed) == 40
    assert result.sent == 40
    assert result.budget_limited is False
    assert result.budget_remaining is None


# ==================================================== an explicit limit ========


def test_an_explicit_limit_larger_than_the_budget_is_clamped(
    fake_settings, monkeypatch
):
    """`limit` is a ceiling the caller may LOWER, never one it may raise.

    An operator (or a future caller, or a hand-crafted request to the endpoint)
    asking for 500 with 10 left in the budget gets 10."""
    world = _World(monkeypatch, cohort=40, allowance=10)

    result = world.send(limit=500)

    assert len(world.emailed) == 10
    assert result.sent == 10
    assert result.budget_limited is True


def test_an_explicit_limit_below_the_budget_is_honoured(fake_settings, monkeypatch):
    """The other half of that: the cap does not overrule a deliberately small
    send. 3 asked for, 10 available -> 3, and it is NOT reported as budget-bound
    (the budget is not why it stopped, and telling the operator otherwise would
    send them to the cap screen for nothing)."""
    world = _World(monkeypatch, cohort=40, allowance=10)

    result = world.send(limit=3)

    assert len(world.emailed) == 3
    assert result.sent == 3
    assert result.budget_limited is False
    assert result.budget_remaining == 7


# ========================================================= a zero budget =======


def test_a_zero_budget_sends_nothing_and_refuses_out_loud(fake_settings, monkeypatch):
    """Zero left, a cohort still owed. Nothing may go out — and the operator must
    be TOLD, not handed a clean "sent 0" that reads as "there was nothing to
    send". They are the two outcomes it matters most to tell apart: one means the
    campaign is finished, the other that 40 people are still waiting."""
    world = _World(monkeypatch, cohort=40, allowance=0)

    with pytest.raises(ConflictError) as excinfo:
        world.send()

    assert world.emailed == []
    assert world.session.send_log == set()
    # The message has to name the cause and what is still owed, or it is just a
    # different flavour of silence.
    message = str(excinfo.value)
    assert "budget" in message.lower()
    assert "40" in message


def test_a_zero_budget_does_not_refuse_a_send_that_had_nothing_to_do(
    fake_settings, monkeypatch
):
    """The refusal is about the BUDGET, not about sending zero.

    A year whose recipients have all already been claimed sends nothing for a
    reason that has nothing to do with the cap — and that path is the #405 repair
    (pressing Send on an already-emailed year is how it gets its campaign back).
    A blanket "sent 0 -> 409" would break it."""
    world = _World(monkeypatch, cohort=3, allowance=0)
    world.session.seed_sent(_YEAR, survey_email.STAGE_INITIAL, [1, 2, 3])

    result = world.send()  # no raise

    assert result.sent == 0
    assert result.stage_complete is True
    assert world.emailed == []


# ============================================================= dry runs ========


def test_a_dry_run_is_unaffected_by_the_budget_in_the_way_that_matters(
    fake_settings, monkeypatch
):
    """A preview writes nothing and sends nothing, whatever the budget says —
    including a budget of zero, which must NOT turn a preview into a 409. Staff
    have to be able to inspect a cohort at any time."""
    world = _World(monkeypatch, cohort=40, allowance=0)

    result = world.send(dry_run=True)

    assert world.emailed == []
    assert world.session.send_log == set()
    assert result.dry_run is True
    assert result.sent == 0
    # The cohort account is still fully reported — the preview's real job.
    assert result.total_recipients == 40


def test_a_dry_run_previews_what_a_real_send_would_actually_take(
    fake_settings, monkeypatch
):
    """And it does not over-promise. `prepared` is "emails this call would
    build", so under a budget of 10 it is 10 — a preview claiming 40 would be the
    console promising more recipients than the sender would take, which is the
    standing bug class in this subsystem."""
    world = _World(monkeypatch, cohort=40, allowance=10)

    result = world.send(dry_run=True)

    assert result.prepared == 10
    assert result.budget_limited is True
    # Nothing was spent, so the whole allowance is still there for the real send.
    assert result.budget_remaining == 10
    assert world.session.send_log == set()


# ================================================ the cron is not double-capped =


def test_the_cron_still_spends_its_whole_budget_in_one_run(
    fake_settings, monkeypatch
):
    """The risk the fix introduces, pinned.

    The cron reads the allowance once, passes what is left as `limit`, and the
    sender now re-reads the same budget. Applied twice — or read as "10 each" per
    year — the run would send the wrong number. With 10 of budget and a cohort of
    40 the run sends TEN: not 5, not 100."""
    world = _World(
        monkeypatch, cohort=40, allowance=10, schedules=[_sched(start=_TODAY)]
    )

    summary = world.cron()

    assert len(world.emailed) == 10
    assert summary.ran[0].sent == 10
    assert summary.ran[0].remaining == 30


def test_the_cron_spreads_one_budget_across_years_without_re_granting_it(
    fake_settings, monkeypatch
):
    """Two campaigns, one shared budget of 10.

    The budget is ACCOUNT-WIDE: the earliest campaign drains first and the second
    gets what is left, not a fresh 10. This is the assertion that would fail if
    the sender's own read replaced the cron's running total instead of being
    `min`-ed with it — each year would see the full allowance again and the run
    would send 10 per campaign.

    Two DIFFERENT graduation years, because `survey_send_log` is unique per
    (year, alum, stage, cycle): with one year repeated, the second campaign would
    send nothing for a reason that has nothing to do with the budget and the test
    would pass vacuously.
    """
    world = _World(
        monkeypatch,
        cohort=8,
        allowance=10,
        schedules=[
            _sched(year=_YEAR, start=_TODAY),
            _sched(year=_YEAR + 1, start=_TODAY),
        ],
    )

    summary = world.cron()

    # 8 for the first campaign, and the second is capped at the remaining 2 —
    # never 16.
    assert [item.sent for item in summary.ran] == [8, 2]
    assert len(world.emailed) == 10


# ================================================== the gate is where it lives ==


def test_the_budget_is_read_by_the_sender_itself_not_only_by_its_callers(
    fake_settings, monkeypatch
):
    """The structural claim, asserted structurally.

    Both fixes to this subsystem have taken the same shape: the dangerous step is
    made impossible to skip rather than remembered at each call site (the send log
    after 2026-08-02, the budget after #417). So the test is not "send_campaign
    passes a limit" — it is that `send_survey_stage`, reached DIRECTLY with no
    limit and by no known caller, is still capped. That is what protects the
    caller that does not exist yet."""
    world = _World(monkeypatch, cohort=40, allowance=6)

    outcome = asyncio.run(
        survey_email.send_survey_stage(
            world.session,
            graduation_year=_YEAR,
            max_stage=survey_email.STAGE_INITIAL,
            actor_user_id=1,
            cycle_seq=1,
        )
    )

    assert outcome.sent == 6
    assert len(world.emailed) == 6
    assert outcome.budget_limited is True


# ============================================== the number itself is the cap's ==


def test_the_allowance_is_the_tighter_of_the_daily_and_monthly_remainders(
    fake_settings, monkeypatch
):
    """The gate spends the SAME number the console's cap screen shows — one
    implementation, reached through `survey_schedule`, never re-derived here. A
    second copy of "daily and monthly, minus usage, whichever is tighter" is how
    the meter and the sender come to disagree."""

    async def cfg(session):
        return SurveySendConfigItem(enabled=True, daily_limit=100, monthly_limit=3000)

    async def usage(session):
        return SurveyUsage(sent_today=90, sent_this_month=2995)

    monkeypatch.setattr(survey_schedule, "get_send_config", cfg)
    monkeypatch.setattr(survey_email, "get_send_usage", usage)

    # Monthly is the tighter of the two (5 vs 10), so 5 is what the sender gets.
    assert asyncio.run(survey_email.remaining_send_allowance(SendLogSession())) == 5


def test_a_disabled_cap_reaches_the_sender_as_unlimited(fake_settings, monkeypatch):
    async def cfg(session):
        return SurveySendConfigItem(enabled=False, daily_limit=1, monthly_limit=1)

    monkeypatch.setattr(survey_schedule, "get_send_config", cfg)

    assert asyncio.run(survey_email.remaining_send_allowance(SendLogSession())) is None

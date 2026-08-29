"""Notification on a survey job posting (#771).

Four things are worth pinning here, and they are the four things that would make
this feature either useless or harmful:

  1. **It fires.** A survey submission raises exactly one alert, through the
     EXISTING alerter (``failure_alert.deliver_alert``) rather than a second
     notification path — asserted by intercepting that function, so a rewrite
     that grew its own Slack client fails.
  2. **It cannot break the submission.** The endpoint is PUBLIC and token-gated
     and an alumnus is waiting on it. Slack refusing, Resend refusing, a timeout,
     or an outright exception must all still leave the posting saved and the
     response a 200. This is the assertion that matters most.
  3. **It leaks nothing.** No alumni name, no e-mail, no company name, no details
     text and no URL — the posting is unmoderated public input minutes old, and
     the recipient needs none of it to open the queue.
  4. **Unset means off, and the digest switch is a switch.** No configured
     channel ⇒ silence at zero cost (which is why the rest of the suite and every
     preview deployment stay quiet), and flipping the mode moves the message from
     the request path to the cron rather than sending twice.

Offline: no database, no network, no DATABASE_URL.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core import rate_limit
from app.core.config import get_settings
from app.core.database import get_session
from app.main import app
from app.models.alumni import Alumni
from app.models.opportunity_link import OpportunityLink
from app.services import failure_alert, opportunity_link_alert
from app.services import opportunity_links as service

GOOD_URL = "https://careers.acme-capital.example/jobs/analyst-2027"


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- fake session --


class _Session:
    """Enough session to run ``submit_links`` with no database behind it."""

    def __init__(self, alum: Alumni | None):
        self._alum = alum
        self.added: list = []
        self.committed = False
        self._next_id = 900

    async def get(self, model, pk):
        return self._alum if model is Alumni else None

    def add(self, obj):
        if isinstance(obj, OpportunityLink) and obj.opportunity_link_id is None:
            obj.opportunity_link_id = self._next_id
            self._next_id += 1
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    @property
    def links(self) -> list[OpportunityLink]:
        return [o for o in self.added if isinstance(o, OpportunityLink)]


def _alum() -> Alumni:
    alum = Alumni(
        alumni_id=1,
        first_name="Dana",
        last_name="Whitcomb",
        preferred_first_name=None,
    )
    alum.archived = False
    return alum


def _submission(count: int = 1):
    from app.schemas.opportunity_link import (
        OpportunityLinkSubmitRequest,
        OpportunitySurveyLinkSubmit,
    )

    return OpportunityLinkSubmitRequest(
        links=[
            OpportunitySurveyLinkSubmit(
                company_name="Acme Capital",
                url=GOOD_URL,
                location_city="Provo",
                location_state="Utah",
                role_type="internship",
                details="Summer analyst programme.",
            )
            for _ in range(count)
        ]
    )


@pytest.fixture(autouse=True)
def _reset_mode(monkeypatch):
    """Every test states its own mode. Without this an env var on the developer's
    machine would decide what the suite asserts."""
    monkeypatch.delenv("OPPORTUNITY_LINK_NOTIFY_MODE", raising=False)
    yield


@pytest.fixture
def alerting_on(monkeypatch):
    """Pretend a channel is configured, and intercept the SHARED alerter.

    ``deliver_alert`` is patched, not ``slack.post_webhook`` — the point of the
    interception is to prove this feature goes through the one existing delivery
    path (with its mode setting, its e-mail backstop and its never-raises rule)
    rather than around it.
    """
    sent: list[dict] = []

    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: True)

    async def _deliver(subject, intro, rows, *, purpose=None, slack_summary=None):
        sent.append(
            {
                "subject": subject,
                "intro": intro,
                "rows": rows,
                "purpose": purpose,
                "summary": slack_summary,
            }
        )
        return True

    monkeypatch.setattr(failure_alert, "deliver_alert", _deliver)
    return sent


def _token(monkeypatch, alumni_id: int = 1) -> str:
    monkeypatch.setattr(
        "app.services.opportunity_links.verify_survey_token", lambda _t: alumni_id
    )
    return "a-signed-token"


# =============================================================================
# 1. It fires, once, through the existing alerter
# =============================================================================


def test_a_survey_submission_raises_one_alert(monkeypatch, alerting_on):
    token = _token(monkeypatch)
    session = _Session(_alum())
    result = _run(service.submit_links(session, token, _submission(count=2)))

    assert result.staged is True and result.link_count == 2
    assert len(alerting_on) == 1, "one message per submission, not one per link"
    assert alerting_on[0]["purpose"] == failure_alert.SUBMISSION


def test_the_message_says_how_many_and_where_to_go(monkeypatch, alerting_on):
    token = _token(monkeypatch)
    _run(service.submit_links(_Session(_alum()), token, _submission(count=2)))

    rows = dict(alerting_on[0]["rows"])
    assert rows["Postings"] == "2"
    assert "internship" in rows["Role type"]
    assert "Links tab" in rows["Action"]
    # The ids are what turns "something arrived" into "review these".
    assert rows["Link IDs"]
    assert "Links tab" in alerting_on[0]["summary"]


def test_the_submission_purpose_routes_to_its_own_channel(monkeypatch):
    """A third channel, not a third copy of the routing. And it FALLS BACK to the
    operational webhook when unset — #771 exists because nothing was being sent,
    so a forgotten env var must not be a way back to silence."""
    get_settings.cache_clear()
    monkeypatch.setenv("SLACK_ALERT_WEBHOOK_URL", "https://hooks.slack.com/services/OPS")
    monkeypatch.delenv("SLACK_SUBMISSION_WEBHOOK_URL", raising=False)
    get_settings.cache_clear()
    assert failure_alert.slack_target(failure_alert.SUBMISSION) == (
        "https://hooks.slack.com/services/OPS"
    )

    monkeypatch.setenv(
        "SLACK_SUBMISSION_WEBHOOK_URL", "https://hooks.slack.com/services/POSTINGS"
    )
    get_settings.cache_clear()
    assert failure_alert.slack_target(failure_alert.SUBMISSION) == (
        "https://hooks.slack.com/services/POSTINGS"
    )
    # ...and an OPERATIONAL alert never drifts into it.
    assert failure_alert.slack_target(failure_alert.OPERATIONAL) == (
        "https://hooks.slack.com/services/OPS"
    )
    get_settings.cache_clear()


def test_the_slack_message_is_tagged_so_a_shared_channel_still_reads(monkeypatch):
    """Because of the fallback the three kinds can land together."""
    payload = failure_alert.render_slack(
        "subject", "intro", [("A", "B")], purpose=failure_alert.SUBMISSION
    )
    assert payload["text"].startswith("POSTING")


# =============================================================================
# 2. ⚠️ It can never break the submission
# =============================================================================


@pytest.mark.parametrize(
    "boom",
    [
        RuntimeError("slack is down"),
        TimeoutError("resend never answered"),
        ValueError("a rendering bug nobody predicted"),
    ],
)
def test_a_failed_alert_still_saves_the_posting(monkeypatch, boom):
    """THE ASSERTION THIS FEATURE LIVES OR DIES ON.

    The endpoint is public and token-gated and an alum is sitting in front of it.
    Whatever the alerter does, the links are committed and the caller is told
    they were staged.
    """
    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: True)

    async def _explode(*a, **kw):
        raise boom

    monkeypatch.setattr(failure_alert, "deliver_alert", _explode)
    token = _token(monkeypatch)
    session = _Session(_alum())

    result = _run(service.submit_links(session, token, _submission()))

    assert result.staged is True
    assert result.link_count == 1
    assert session.committed is True
    assert len(session.links) == 1


def test_a_hanging_channel_is_time_boxed(monkeypatch):
    """A third party that never answers costs seconds, not the request. The
    posting is already committed by the time this runs."""
    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: True)
    monkeypatch.setattr(opportunity_link_alert, "_DELIVERY_TIMEOUT_SECONDS", 0.01)

    async def _hang(*a, **kw):
        await asyncio.sleep(5)
        return True

    monkeypatch.setattr(failure_alert, "deliver_alert", _hang)
    token = _token(monkeypatch)
    session = _Session(_alum())
    result = _run(service.submit_links(session, token, _submission()))
    assert result.staged is True and session.committed is True


def test_the_alert_runs_after_the_commit(monkeypatch):
    """Ordering, asserted rather than assumed: the row must be durable BEFORE a
    third party is contacted, or a crash mid-POST loses the alum's posting."""
    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: True)
    order: list[str] = []
    session = _Session(_alum())

    original_commit = session.commit

    async def _commit():
        order.append("commit")
        await original_commit()

    session.commit = _commit  # type: ignore[method-assign]

    async def _deliver(*a, **kw):
        order.append("alert")
        return True

    monkeypatch.setattr(failure_alert, "deliver_alert", _deliver)
    token = _token(monkeypatch)
    _run(service.submit_links(session, token, _submission()))
    assert order == ["commit", "alert"]


def test_an_invalid_link_is_still_a_422_and_sends_nothing(monkeypatch, alerting_on):
    """Validation runs before anything is staged, so a rejected batch must not
    announce postings that were never written."""
    from app.schemas.opportunity_link import OpportunityLinkSubmitRequest

    token = _token(monkeypatch)
    session = _Session(_alum())
    with pytest.raises(ValueError):
        payload = OpportunityLinkSubmitRequest.model_construct(
            links=[
                type(
                    "Bad",
                    (),
                    {
                        "url": "javascript:alert(1)",
                        "company_name": "Acme",
                        "is_own_company": False,
                        "location_city": None,
                        "location_state": None,
                        "location_country": None,
                        "role_type": "internship",
                        "application_deadline": None,
                        "details": None,
                    },
                )()
            ]
        )
        _run(service.submit_links(session, token, payload))
    assert alerting_on == []


# =============================================================================
# 3. ⚠️ Nothing that leaves the system may be PII or unmoderated text
# =============================================================================


def test_the_alert_carries_no_pii_and_no_unmoderated_text(monkeypatch, alerting_on):
    """The posting is public input minutes old and has not been reviewed. The
    message says how many arrived and where to action them — nothing else."""
    token = _token(monkeypatch)
    _run(service.submit_links(_Session(_alum()), token, _submission()))

    message = alerting_on[0]
    blob = " ".join(
        [message["subject"], message["intro"], message["summary"]]
        + [f"{k} {v}" for k, v in message["rows"]]
    )
    for forbidden in (
        "Dana",  # the alum's name
        "Whitcomb",
        "Acme Capital",  # a typed company name
        "careers.acme-capital.example",  # the unmoderated URL
        GOOD_URL,
        "Summer analyst programme.",  # free-text details
        "Provo",
    ):
        assert forbidden not in blob, f"{forbidden!r} must not leave the system"


def test_a_hostile_company_name_cannot_reach_the_channel(monkeypatch, alerting_on):
    """Escaping would stop a ``<`` eating the line; it would not stop a channel
    full of whatever somebody typed into a public form. So the name never goes."""
    from app.schemas.opportunity_link import (
        OpportunityLinkSubmitRequest,
        OpportunitySurveyLinkSubmit,
    )

    token = _token(monkeypatch)
    payload = OpportunityLinkSubmitRequest(
        links=[
            OpportunitySurveyLinkSubmit(
                company_name="Call 555 0100 to claim",
                url=GOOD_URL,
                role_type="both",
            )
        ]
    )
    _run(service.submit_links(_Session(_alum()), token, payload))
    blob = " ".join(f"{k} {v}" for k, v in alerting_on[0]["rows"])
    assert "555 0100" not in blob
    assert "both" in blob  # the role type IS an enum, so it may travel


def test_the_ids_are_truncated_so_a_bulk_submit_cannot_flood_a_line():
    rendered = opportunity_link_alert._ids(list(range(1, 26)))
    assert rendered.endswith("+15 more")
    assert len(rendered.split(", ")) == 11


# =============================================================================
# 4. Unset means off; the digest is a config switch
# =============================================================================


def test_no_configured_channel_means_no_message_and_no_work(monkeypatch):
    """The rule the whole alerting stack shares, and what keeps this suite, CI and
    every preview deployment silent with nothing to remember to switch off."""
    monkeypatch.setattr(failure_alert, "alerting_enabled", lambda: False)
    called: list = []

    async def _deliver(*a, **kw):
        called.append(1)
        return True

    monkeypatch.setattr(failure_alert, "deliver_alert", _deliver)
    token = _token(monkeypatch)
    session = _Session(_alum())
    result = _run(service.submit_links(session, token, _submission()))
    assert result.staged is True
    assert called == []


def test_an_empty_submission_says_nothing(alerting_on):
    assert _run(opportunity_link_alert.notify_new_links([])) is False
    assert alerting_on == []


def test_the_default_mode_is_per_posting(monkeypatch):
    monkeypatch.delenv("OPPORTUNITY_LINK_NOTIFY_MODE", raising=False)
    assert opportunity_link_alert.notify_mode() == (
        opportunity_link_alert.MODE_PER_POSTING
    )


def test_an_unrecognised_mode_falls_back_to_sending(monkeypatch):
    """A typo in an env var must not turn the feature off — the whole defect is
    that nothing was being sent."""
    monkeypatch.setenv("OPPORTUNITY_LINK_NOTIFY_MODE", "weekly-ish")
    assert opportunity_link_alert.notify_mode() == (
        opportunity_link_alert.MODE_PER_POSTING
    )


def test_digest_mode_moves_the_message_off_the_request_path(monkeypatch, alerting_on):
    """The switch is a switch, not a second sender: in digest mode the public
    write says nothing and the cron does the talking."""
    monkeypatch.setenv("OPPORTUNITY_LINK_NOTIFY_MODE", "daily_digest")
    token = _token(monkeypatch)
    result = _run(service.submit_links(_Session(_alum()), token, _submission()))
    assert result.staged is True
    assert alerting_on == []


class _DigestSession:
    def __init__(self, rows, pending_total=0):
        self._rows = rows
        self._pending = pending_total

    async def execute(self, stmt):
        rows = self._rows

        class _R:
            def scalars(self):
                return self

            def all(self):
                return list(rows)

        return _R()

    async def scalar(self, stmt):
        return self._pending


def _survey_link(link_id: int, role_type: str = "internship") -> OpportunityLink:
    return OpportunityLink(
        opportunity_link_id=link_id,
        alumni_id=1,
        is_own_company=False,
        company_name="Acme Capital",
        url=GOOD_URL,
        role_type=role_type,
        status="pending",
        source="survey",
        submitted_at=datetime.datetime(2026, 8, 28, 9, 0, tzinfo=datetime.UTC),
    )


def test_the_digest_reports_the_window_and_the_queue_depth(monkeypatch, alerting_on):
    monkeypatch.setenv("OPPORTUNITY_LINK_NOTIFY_MODE", "daily_digest")
    session = _DigestSession(
        [_survey_link(1), _survey_link(2, "full_time")], pending_total=9
    )
    assert _run(opportunity_link_alert.send_digest(session)) is True
    rows = dict(alerting_on[0]["rows"])
    assert rows["Submitted"] == "2"
    assert rows["Pending in total"] == "9"
    assert alerting_on[0]["purpose"] == failure_alert.SUBMISSION


def test_the_digest_is_silent_on_a_quiet_day(monkeypatch, alerting_on):
    """A daily "nothing happened" message is how a channel gets muted, and a muted
    channel is the failure this is preventing."""
    monkeypatch.setenv("OPPORTUNITY_LINK_NOTIFY_MODE", "daily_digest")
    assert _run(opportunity_link_alert.send_digest(_DigestSession([]))) is False
    assert alerting_on == []


def test_the_digest_is_inert_in_the_shipped_mode(monkeypatch, alerting_on):
    """Both halves exist so the switch is config; only one of them ever speaks."""
    monkeypatch.delenv("OPPORTUNITY_LINK_NOTIFY_MODE", raising=False)
    session = _DigestSession([_survey_link(1)], pending_total=3)
    assert _run(opportunity_link_alert.send_digest(session)) is False
    assert alerting_on == []


def test_the_digest_carries_no_pii_either(monkeypatch, alerting_on):
    monkeypatch.setenv("OPPORTUNITY_LINK_NOTIFY_MODE", "daily_digest")
    _run(opportunity_link_alert.send_digest(_DigestSession([_survey_link(1)], 1)))
    blob = " ".join(
        [alerting_on[0]["subject"], alerting_on[0]["summary"]]
        + [f"{k} {v}" for k, v in alerting_on[0]["rows"]]
    )
    assert "Acme Capital" not in blob
    assert GOOD_URL not in blob


def test_a_broken_digest_query_does_not_500_the_cron(monkeypatch, alerting_on):
    monkeypatch.setenv("OPPORTUNITY_LINK_NOTIFY_MODE", "daily_digest")

    class _Broken:
        async def execute(self, stmt):
            raise RuntimeError("the database is unreachable")

    assert _run(opportunity_link_alert.send_digest(_Broken())) is False


# =============================================================================
# 5. The cron endpoint is shared-secret gated, like every other cron here
# =============================================================================


@pytest.fixture
def client(monkeypatch):
    async def _no_db_session():
        yield None

    app.dependency_overrides[get_session] = _no_db_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_the_digest_cron_is_closed_by_default(monkeypatch, client):
    """``CRON_SECRET`` unset ⇒ every caller is refused. Never open by default."""
    get_settings.cache_clear()
    monkeypatch.delenv("CRON_SECRET", raising=False)
    get_settings.cache_clear()
    assert client.post("/opportunity-links/cron/digest").status_code == 401
    assert client.get("/opportunity-links/cron/digest").status_code == 401
    get_settings.cache_clear()


def test_the_digest_cron_accepts_the_shared_secret(monkeypatch, client):
    get_settings.cache_clear()
    monkeypatch.setenv("CRON_SECRET", "s3cret")
    get_settings.cache_clear()

    async def _send(session):
        return False

    monkeypatch.setattr(opportunity_link_alert, "send_digest", _send)
    response = client.post(
        "/opportunity-links/cron/digest",
        headers={"Authorization": "Bearer s3cret"},
    )
    assert response.status_code == 200
    assert response.json() == {"sent": False}
    assert (
        client.post(
            "/opportunity-links/cron/digest",
            headers={"Authorization": "Bearer wrong"},
        ).status_code
        == 401
    )
    get_settings.cache_clear()


def test_the_digest_cron_stays_out_of_the_generated_types():
    """``include_in_schema=False``: no browser client calls it, so it must not
    move ``api.gen.ts`` and set CI's type-contract drift guard off."""
    assert "/opportunity-links/cron/digest" not in app.openapi()["paths"]
    # Unused-import guard: the limiter module is imported by the survey route this
    # feature hangs off, and a moved limiter would break the public path.
    assert rate_limit is not None
    assert uuid is not None

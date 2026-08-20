"""Slack delivery: which channel each kind of alert lands in (#456).

``tests/test_failure_alert.py`` proves WHEN an alert is produced. This file proves
WHERE it goes: that email and both Slack channels are independently optional, that
an operational alert and a security alert route to different webhooks, that a
missing security webhook falls back rather than dropping the alert, that one
channel being down cannot suppress another, and that nothing secret or personal
rides along.

THE ROUTING, which is what most of this file is about:

    OPERATIONAL (API failing / recovered)  -> SLACK_ALERT_WEBHOOK_URL, #error-alerts
    SECURITY    (login brute force)        -> SLACK_SECURITY_WEBHOOK_URL, #security-alerts
                                              falling back to the error webhook

and NOT the reverse: an operational alert never diverts into the security channel.

The webhook URLs used throughout are deliberately on ``hooks.slack.test`` — a
reserved TLD that resolves nowhere. A realistic ``hooks.slack.com/services/...``
literal is what gitleaks' bundled ``slack-webhook-url`` rule looks for, and a
test fixture is not worth an allowlist entry that would also blind the scanner to
a real webhook landing in this file later.

The two webhook properties are read from a REAL ``Settings`` instance rather than
stubbed on a namespace, because the fallback is implemented as one of those
properties. A hand-written fake would be a second copy of the rule being tested,
and it would agree with itself no matter what the app did.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import failure_alert, slack


async def _async_true(*a, **k):
    return True


async def _async_false(*a, **k):
    return False


def _rejecting_webhook(channels):
    """A Slack endpoint that answers 404, the way a revoked webhook does."""

    async def reject(url, *, payload, timeout):
        channels["slack"].append((url, payload))
        return SimpleNamespace(is_success=False, status_code=404)

    return reject

WEBHOOK = "https://hooks.slack.test/services/ERROR/CHANNEL/FAKE"
SECURITY_WEBHOOK = "https://hooks.slack.test/services/SECURITY/CHANNEL/FAKE"


@pytest.fixture
def channels(monkeypatch):
    """Capture what each channel would send, with no network call anywhere."""
    captured = {"email": [], "slack": []}

    async def fake_email(url, *, api_key, payload, timeout):
        captured["email"].append(payload)
        return SimpleNamespace(is_success=True, status_code=200)

    async def fake_slack(url, *, payload, timeout):
        captured["slack"].append((url, payload))
        return SimpleNamespace(is_success=True, status_code=200)

    monkeypatch.setattr(failure_alert.mailer, "post_json", fake_email)
    monkeypatch.setattr(failure_alert.slack, "post_webhook", fake_slack)
    return captured


def _settings(monkeypatch, *, webhook=WEBHOOK, security=None, email=True):
    """Install settings whose Slack properties come from the REAL Settings class.

    ``webhook`` / ``security`` are the raw env values; the derived
    ``slack_webhook`` / ``slack_security_webhook`` — and therefore the fallback —
    are computed by the app, not by this fixture.
    """
    derived = Settings(
        slack_alert_webhook_url=webhook, slack_security_webhook_url=security
    )
    fake = SimpleNamespace(
        environment="production",
        resend_api_key="re_test_key" if email else None,
        alert_from_name="BYU Finance Alumni API",
        alert_recipients=["engineer@example.edu"] if email else [],
        alert_sender="alerts@example.edu" if email else None,
        slack_webhook=derived.slack_webhook,
        slack_security_webhook=derived.slack_security_webhook,
    )
    monkeypatch.setattr(failure_alert, "get_settings", lambda: fake)


ROWS = [("Environment", "production"), ("Status code", "500")]


def _deliver(purpose=failure_alert.OPERATIONAL):
    return asyncio.run(
        failure_alert.deliver_alert("subject", "intro", ROWS, purpose=purpose)
    )


def _urls(channels):
    return [url for url, _payload in channels["slack"]]


# ------------------------------------------------------------------ routing --


def test_a_security_alert_goes_only_to_the_security_channel(monkeypatch, channels):
    """Both webhooks set: the attack alert lands in #security-alerts and nowhere
    else. Posting it to both would be two pings for one event and would put the
    thing the security channel exists for into the noisy channel as well."""
    _settings(monkeypatch, webhook=WEBHOOK, security=SECURITY_WEBHOOK)

    assert _deliver(failure_alert.SECURITY) is True
    assert _urls(channels) == [SECURITY_WEBHOOK]


def test_an_operational_alert_goes_only_to_the_error_channel(monkeypatch, channels):
    """The mirror image, and the one that must hold even harder: #security-alerts
    is a channel someone opens to answer "are we under attack?", and a stream of
    500s in it is how that channel stops being useful."""
    _settings(monkeypatch, webhook=WEBHOOK, security=SECURITY_WEBHOOK)

    assert _deliver(failure_alert.OPERATIONAL) is True
    assert _urls(channels) == [WEBHOOK]


def test_an_operational_alert_never_diverts_into_the_security_channel(
    monkeypatch, channels
):
    """With ONLY the security webhook set, an outage alert has no Slack channel.
    It goes to email, or nowhere — it does NOT fall into #security-alerts. The
    fallback is one-directional by design."""
    _settings(monkeypatch, webhook=None, security=SECURITY_WEBHOOK)

    assert _deliver(failure_alert.OPERATIONAL) is True  # the email still landed
    assert channels["slack"] == []
    assert len(channels["email"]) == 1


def test_a_security_alert_falls_back_to_the_error_channel(monkeypatch, channels):
    """SLACK_SECURITY_WEBHOOK_URL unset, SLACK_ALERT_WEBHOOK_URL set: the attack
    alert goes to #error-alerts rather than being dropped.

    Forgetting a second env var is an ordinary mistake — a preview, a
    re-provisioned project, the day someone rotates a webhook. A misfiled alert is
    a nuisance; a missing one is the exact failure this whole feature exists to
    end."""
    _settings(monkeypatch, webhook=WEBHOOK, security=None)

    assert _deliver(failure_alert.SECURITY) is True
    assert _urls(channels) == [WEBHOOK]


def test_the_fallback_message_still_says_it_is_a_security_alert(monkeypatch, channels):
    """The fallback is the ONE case where both kinds share a channel, so it is the
    case where the tag matters. Someone scanning #error-alerts at 9pm has to be
    able to tell the attack from the 500s without reading."""
    _settings(monkeypatch, webhook=WEBHOOK, security=None)
    _deliver(failure_alert.SECURITY)
    _deliver(failure_alert.OPERATIONAL)

    security_payload = channels["slack"][0][1]
    operational_payload = channels["slack"][1][1]

    assert security_payload["blocks"][0]["text"]["text"].startswith("SECURITY \u2014 ")
    assert operational_payload["blocks"][0]["text"]["text"].startswith("OUTAGE \u2014 ")
    # And in the phone-lock-screen preview, not just in the channel.
    assert security_payload["text"].startswith("SECURITY \u2014 ")
    assert operational_payload["text"].startswith("OUTAGE \u2014 ")


def test_the_routing_table_is_what_it_says(monkeypatch):
    """The routing in one assertion, without going through a delivery."""
    _settings(monkeypatch, webhook=WEBHOOK, security=SECURITY_WEBHOOK)
    assert failure_alert.slack_target(failure_alert.SECURITY) == SECURITY_WEBHOOK
    assert failure_alert.slack_target(failure_alert.OPERATIONAL) == WEBHOOK

    _settings(monkeypatch, webhook=WEBHOOK, security=None)
    assert failure_alert.slack_target(failure_alert.SECURITY) == WEBHOOK

    _settings(monkeypatch, webhook=None, security=SECURITY_WEBHOOK)
    assert failure_alert.slack_target(failure_alert.SECURITY) == SECURITY_WEBHOOK
    assert failure_alert.slack_target(failure_alert.OPERATIONAL) is None


def test_the_fallback_is_defined_on_settings_not_invented_at_the_call_site():
    """Read the rule directly off the config, since that is where it is documented
    and where anyone changing it will look."""
    both = Settings(
        slack_alert_webhook_url=WEBHOOK, slack_security_webhook_url=SECURITY_WEBHOOK
    )
    assert both.slack_webhook == WEBHOOK
    assert both.slack_security_webhook == SECURITY_WEBHOOK

    error_only = Settings(slack_alert_webhook_url=WEBHOOK)
    assert error_only.slack_security_webhook == WEBHOOK, "security must fall back"

    security_only = Settings(slack_security_webhook_url=SECURITY_WEBHOOK)
    assert security_only.slack_security_webhook == SECURITY_WEBHOOK
    assert security_only.slack_webhook is None, "the fallback is one-directional"

    neither = Settings()
    assert neither.slack_webhook is None
    assert neither.slack_security_webhook is None


# ------------------------------------------------- the two channels are separate --


def test_slack_gets_the_alert_and_the_mailbox_stays_quiet(monkeypatch, channels):
    """SLACK IS THE CHANNEL; EMAIL IS THE BACKSTOP (changed 2026-08-19).

    Both used to go every time, so one attack arrived twice. The owner asked for
    it all in Slack. The email was not deleted — see the test below — it is now
    conditional on Slack not landing, because a single channel that breaks is
    silence, and silence is the failure this module exists to prevent."""
    _settings(monkeypatch)
    assert _deliver() is True

    assert len(channels["slack"]) == 1
    assert channels["email"] == [], "the mail is a backstop, not a copy"
    url, payload = channels["slack"][0]
    assert url == WEBHOOK
    assert payload["text"] == "OUTAGE — subject"


def test_the_mail_still_goes_when_slack_rejects_the_post(monkeypatch, channels):
    """The reason there are two channels at all. A revoked webhook, a Slack
    outage or a typo'd URL must not turn into silence — the backstop fires with
    the FULL rows, untagged, byte-for-byte the mail that was sent before."""
    _settings(monkeypatch)
    monkeypatch.setattr(
        failure_alert.slack,
        "post_webhook",
        _rejecting_webhook(channels),
    )

    assert _deliver() is True
    assert len(channels["email"]) == 1
    assert channels["email"][0]["subject"] == "subject"


def test_slack_alone_works_without_any_mailbox(monkeypatch, channels):
    """A deployment may want the channel and no email at all. Slack must not be a
    bolt-on that only fires when email is also configured."""
    _settings(monkeypatch, email=False)

    assert _deliver() is True
    assert channels["email"] == []
    assert len(channels["slack"]) == 1
    assert failure_alert.alerting_enabled() is True


def test_email_alone_still_works_with_no_webhook(monkeypatch, channels):
    """The pre-existing behaviour, unchanged: this is what production runs today."""
    _settings(monkeypatch, webhook=None)

    assert _deliver() is True
    assert len(channels["email"]) == 1
    assert channels["slack"] == []


def test_neither_configured_is_silent_and_does_no_work(monkeypatch, channels):
    """The default everywhere except prod: local dev, CI, the test suite, every
    preview deployment. Unset means off, per channel, with no second flag."""
    _settings(monkeypatch, webhook=None, email=False)

    assert failure_alert.alerting_enabled() is False
    assert _deliver() is False
    assert channels["email"] == []
    assert channels["slack"] == []


def test_a_whitespace_only_webhook_reads_as_unset(monkeypatch):
    """Vercel env vars are edited in a web form. A stray space must not read as
    'configured' and turn every alert into a failed POST to ' '."""
    assert Settings(slack_alert_webhook_url="   ").slack_webhook is None
    assert Settings(slack_alert_webhook_url=WEBHOOK).slack_webhook == WEBHOOK
    # And the same for the security one, including that a blank value there still
    # falls back rather than reading as "configured, but broken".
    assert Settings(slack_security_webhook_url="  ").slack_security_webhook is None
    assert (
        Settings(
            slack_alert_webhook_url=WEBHOOK, slack_security_webhook_url="  "
        ).slack_security_webhook
        == WEBHOOK
    )


# ------------------------------------------- one channel failing is not two --


def test_slack_being_down_does_not_suppress_the_email(monkeypatch, channels):
    """The whole point of a second channel is that either one alone gets through.
    A sequential ``await slack(); await email()`` would lose this the first time
    Slack raised."""
    _settings(monkeypatch)

    async def exploding(url, *, payload, timeout):
        raise RuntimeError("slack unreachable")

    monkeypatch.setattr(failure_alert.slack, "post_webhook", exploding)

    assert _deliver() is True  # the email still landed
    assert len(channels["email"]) == 1


def test_email_being_down_does_not_suppress_slack(monkeypatch, channels):
    _settings(monkeypatch)

    async def exploding(url, *, api_key, payload, timeout):
        raise RuntimeError("resend unreachable")

    monkeypatch.setattr(failure_alert.mailer, "post_json", exploding)

    assert _deliver() is True
    assert len(channels["slack"]) == 1


def test_a_failing_slack_post_neither_raises_nor_retries(monkeypatch):
    """This runs on a request that is already broken. A retry loop against a third
    party during an outage is how a monitoring feature becomes part of the
    outage."""
    _settings(monkeypatch, email=False)
    attempts = []

    async def exploding(url, *, payload, timeout):
        attempts.append(payload)
        raise RuntimeError("slack unreachable")

    monkeypatch.setattr(failure_alert.slack, "post_webhook", exploding)

    assert _deliver() is False  # must not raise
    assert len(attempts) == 1, "a failed Slack post must not be retried"


def test_a_rejected_slack_post_is_not_retried(monkeypatch):
    """Slack answers a revoked webhook with 404 `no_service`. Same rule."""
    _settings(monkeypatch, email=False)
    attempts = []

    async def rejecting(url, *, payload, timeout):
        attempts.append(payload)
        return SimpleNamespace(is_success=False, status_code=404)

    monkeypatch.setattr(failure_alert.slack, "post_webhook", rejecting)

    assert _deliver() is False
    assert len(attempts) == 1


def test_a_failed_delivery_is_never_itself_reported(monkeypatch, caplog):
    """The one way to build a recursion here is to alert about a failed alert. The
    failure is logged and dropped — the log record must not name the webhook URL
    either, since that URL is the entire credential for posting to the channel."""
    _settings(monkeypatch, email=False)

    async def exploding(url, *, payload, timeout):
        raise RuntimeError("slack unreachable")

    monkeypatch.setattr(failure_alert.slack, "post_webhook", exploding)

    with caplog.at_level("DEBUG"):
        assert _deliver() is False

    assert WEBHOOK not in caplog.text
    assert SECURITY_WEBHOOK not in caplog.text
    assert "hooks.slack" not in caplog.text


# ---------------------------------------------------------- message contents --


def test_the_payload_carries_a_notification_preview_and_the_rows():
    """``blocks`` alone renders as "This content can't be displayed" in the
    channel list and on a lock screen, so ``text`` must be set too."""
    payload = failure_alert.render_slack(
        "[fa-web-api production] API failing", "intro line", ROWS
    )

    assert payload["text"] == "OUTAGE \u2014 [fa-web-api production] API failing"
    body = payload["blocks"][1]["text"]["text"]
    assert "intro line" in body
    assert "*Environment:* production" in body
    assert "*Status code:* 500" in body


def test_a_long_subject_is_cut_rather_than_rejected():
    """Slack rejects a header block over 150 characters with a 400, which would
    lose the whole message."""
    payload = failure_alert.render_slack(
        "x" * 400, "intro", ROWS, purpose=failure_alert.SECURITY
    )

    assert len(payload["blocks"][0]["text"]["text"]) == 150
    # The tag leads, so truncation can never be what removes it — the one thing
    # in the headline that must survive is the first thing in it.
    assert payload["blocks"][0]["text"]["text"].startswith("SECURITY \u2014 ")
    # The untruncated subject survives in the notification preview.
    assert payload["text"].endswith("x" * 400)


def test_markup_characters_in_a_value_cannot_eat_the_line():
    """Slack mrkdwn treats < as the start of a link. Unescaped, everything after
    it vanishes from the rendered message — an alert that silently loses its own
    contents."""
    payload = failure_alert.render_slack(
        "s", "i", [("Error type", "<script>Boom</script>")]
    )
    body = payload["blocks"][1]["text"]["text"]

    assert "&lt;script&gt;Boom&lt;/script&gt;" in body
    assert "<script>" not in body


def test_ampersands_are_escaped_exactly_once():
    """Order matters: escaping < and > before & would double-escape the
    ampersands those replacements introduce."""
    assert slack.escape_mrkdwn("a & b < c") == "a &amp; b &lt; c"
    assert "&amp;amp;" not in slack.escape_mrkdwn("<&>")


def test_no_webhook_url_ever_appears_in_a_message_body(monkeypatch, channels):
    """They are credentials, and the message is rendered from rows we control —
    but assert it, because a future 'helpful' diagnostic row is exactly how a
    secret ends up in a channel a dozen people can read. Both webhooks are checked
    against both kinds of message, so a routing change cannot leak one into the
    other's payload."""
    import json

    _settings(monkeypatch, webhook=WEBHOOK, security=SECURITY_WEBHOOK)
    _deliver(failure_alert.OPERATIONAL)
    _deliver(failure_alert.SECURITY)

    for _url, payload in channels["slack"]:
        rendered = json.dumps(payload)
        assert WEBHOOK not in rendered
        assert SECURITY_WEBHOOK not in rendered


# ================================== THE TEST ALERT (#457 follow-up) ==========
#
# "Is alerting wired up?" used to be answerable only by breaking something: an
# outage alert needs three sustained failures, so proving the operational channel
# meant deliberately 5xx-ing production for a minute. On 2026-08-19 a real
# security alert landed in #error-alerts because SLACK_SECURITY_WEBHOOK_URL was
# unset — the documented one-way fallback doing its job — and nothing anywhere
# made that visible. These pin the endpoint that answers both questions.


def _settings_with(monkeypatch, *, security, operational, email):
    class S:
        environment = "production"
        slack_security_webhook = security
        slack_webhook = operational
        alert_recipients = ["eng@byu.edu"] if email else []
        alert_sender = "alerts@byu.edu" if email else None
        resend_api_key = "key" if email else None

    monkeypatch.setattr(failure_alert, "get_settings", lambda: S())
    return S


def test_the_test_alert_reports_an_unconfigured_channel_as_unconfigured(monkeypatch):
    """The distinction the whole endpoint exists for: nothing arrived BECAUSE
    there is nowhere to send, not because a send failed."""
    _settings_with(monkeypatch, security=None, operational=None, email=False)

    result = asyncio.run(
        failure_alert.deliver_test_alert(purpose=failure_alert.SECURITY, requested_by=None)
    )

    assert result["slack_configured"] is False
    assert result["slack_delivered"] is False
    assert result["email_configured"] is False


def test_the_test_alert_names_the_fallback_that_hid_itself(monkeypatch):
    """When no security webhook is set, a security alert goes to the error
    channel. That is deliberate — a forgotten env var must never mean a missing
    attack alert — but it is invisible from inside Slack, which is precisely how
    it went unnoticed. The endpoint says so."""
    hook = "https://hooks.slack.test/services/ERROR/CHANNEL"
    _settings_with(monkeypatch, security=hook, operational=hook, email=False)
    monkeypatch.setattr(failure_alert, "_send_slack", _async_true)
    monkeypatch.setattr(failure_alert, "_send_email", _async_false)

    result = asyncio.run(
        failure_alert.deliver_test_alert(purpose=failure_alert.SECURITY, requested_by=None)
    )

    assert result["fell_back_to_error_channel"] is True


def test_a_properly_split_pair_does_not_report_a_fallback(monkeypatch):
    _settings_with(
        monkeypatch,
        security="https://hooks.slack.test/services/SECURITY/CHANNEL",
        operational="https://hooks.slack.test/services/ERROR/CHANNEL",
        email=False,
    )
    monkeypatch.setattr(failure_alert, "_send_slack", _async_true)
    monkeypatch.setattr(failure_alert, "_send_email", _async_false)

    result = asyncio.run(
        failure_alert.deliver_test_alert(purpose=failure_alert.SECURITY, requested_by=None)
    )

    assert result["fell_back_to_error_channel"] is False
    assert result["slack_delivered"] is True


def test_the_test_alert_opens_no_incident(monkeypatch):
    """A test must never be able to suppress a real alert. `service_incidents`
    dedups on ONE open row per environment, so a test that opened one would
    silence the outage starting a second later."""
    source = Path(failure_alert.__file__).read_text(encoding="utf-8")
    body = source[source.index("async def deliver_test_alert") :]
    body = body[: body.index("def email_alerting_enabled")]

    for forbidden in ("record_failure", "close_if_quiet", "_SQL_", "SessionLocal"):
        assert forbidden not in body, f"the test alert must not touch {forbidden}"


def test_the_test_alert_says_it_is_a_test_in_every_channel(monkeypatch):
    """Nobody should have to work out whether the thing that just pinged at 2am
    was real."""
    _settings_with(
        monkeypatch,
        security="https://hooks.slack.test/s",
        operational=None,
        email=False,
    )
    captured = {}

    async def capture(subject, intro, rows, *, purpose=None, summary=None):
        captured.update(subject=subject, intro=intro, summary=summary)
        return True

    monkeypatch.setattr(failure_alert, "_send_slack", capture)
    monkeypatch.setattr(failure_alert, "_send_email", _async_false)

    asyncio.run(
        failure_alert.deliver_test_alert(purpose=failure_alert.SECURITY, requested_by="e@byu.edu")
    )

    assert "TEST" in captured["subject"]
    assert "This is a test" in captured["intro"]
    assert "Test message" in captured["summary"]

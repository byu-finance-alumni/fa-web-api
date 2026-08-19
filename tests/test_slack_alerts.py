"""Slack as a second delivery channel for alerts (#456).

``tests/test_failure_alert.py`` proves WHEN an alert is produced. This file
proves WHERE it goes: that email and Slack are independently optional, that
either one alone works, that one being down cannot suppress the other, and that
nothing secret or personal rides along.

The webhook URL used throughout is deliberately on ``hooks.slack.test`` — a
reserved TLD that resolves nowhere. A realistic ``hooks.slack.com/services/...``
literal is what gitleaks' bundled ``slack-webhook-url`` rule looks for, and a
test fixture is not worth an allowlist entry that would also blind the scanner to
a real webhook landing in this file later.
"""

import asyncio
from types import SimpleNamespace

import pytest

from app.services import failure_alert, slack

WEBHOOK = "https://hooks.slack.test/services/FAKE/FAKE/FAKE"


class _Settings(SimpleNamespace):
    """Settings with both channels adjustable per test."""

    def __init__(self, **overrides):
        fields = {
            "environment": "production",
            "resend_api_key": "re_test_key",
            "alert_from_name": "BYU Finance Alumni API",
            "alert_recipients": ["engineer@example.edu"],
            "alert_sender": "alerts@example.edu",
            "slack_webhook": WEBHOOK,
        }
        fields.update(overrides)
        super().__init__(**fields)


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


def _settings(monkeypatch, **kwargs):
    monkeypatch.setattr(failure_alert, "get_settings", lambda: _Settings(**kwargs))


ROWS = [("Environment", "production"), ("Status code", "500")]


def _deliver():
    return asyncio.run(failure_alert.deliver_alert("subject", "intro", ROWS))


# ------------------------------------------------- the two channels are separate --


def test_both_channels_get_the_same_alert(monkeypatch, channels):
    _settings(monkeypatch)
    assert _deliver() is True

    assert len(channels["email"]) == 1
    assert len(channels["slack"]) == 1
    url, payload = channels["slack"][0]
    assert url == WEBHOOK
    assert payload["text"] == "subject"


def test_slack_alone_works_without_any_mailbox(monkeypatch, channels):
    """A deployment may want the channel and no email at all. Slack must not be a
    bolt-on that only fires when email is also configured."""
    _settings(monkeypatch, alert_recipients=[], alert_sender=None)

    assert _deliver() is True
    assert channels["email"] == []
    assert len(channels["slack"]) == 1
    assert failure_alert.alerting_enabled() is True


def test_email_alone_still_works_with_no_webhook(monkeypatch, channels):
    """The pre-existing behaviour, unchanged: this is what production runs today."""
    _settings(monkeypatch, slack_webhook=None)

    assert _deliver() is True
    assert len(channels["email"]) == 1
    assert channels["slack"] == []


def test_neither_configured_is_silent_and_does_no_work(monkeypatch, channels):
    """The default everywhere except prod: local dev, CI, the test suite, every
    preview deployment. Unset means off, per channel, with no second flag."""
    _settings(monkeypatch, alert_recipients=[], alert_sender=None, slack_webhook=None)

    assert failure_alert.alerting_enabled() is False
    assert _deliver() is False
    assert channels["email"] == []
    assert channels["slack"] == []


def test_a_whitespace_only_webhook_reads_as_unset(monkeypatch):
    """Vercel env vars are edited in a web form. A stray space must not read as
    'configured' and turn every alert into a failed POST to ' '."""
    from app.core.config import Settings

    assert Settings(slack_alert_webhook_url="   ").slack_webhook is None
    assert Settings(slack_alert_webhook_url=WEBHOOK).slack_webhook == WEBHOOK


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
    _settings(monkeypatch, alert_recipients=[], alert_sender=None)
    attempts = []

    async def exploding(url, *, payload, timeout):
        attempts.append(payload)
        raise RuntimeError("slack unreachable")

    monkeypatch.setattr(failure_alert.slack, "post_webhook", exploding)

    assert _deliver() is False  # must not raise
    assert len(attempts) == 1, "a failed Slack post must not be retried"


def test_a_rejected_slack_post_is_not_retried(monkeypatch):
    """Slack answers a revoked webhook with 404 `no_service`. Same rule."""
    _settings(monkeypatch, alert_recipients=[], alert_sender=None)
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
    _settings(monkeypatch, alert_recipients=[], alert_sender=None)

    async def exploding(url, *, payload, timeout):
        raise RuntimeError("slack unreachable")

    monkeypatch.setattr(failure_alert.slack, "post_webhook", exploding)

    with caplog.at_level("DEBUG"):
        assert _deliver() is False

    assert WEBHOOK not in caplog.text
    assert "hooks.slack" not in caplog.text


# ---------------------------------------------------------- message contents --


def test_the_payload_carries_a_notification_preview_and_the_rows():
    """``blocks`` alone renders as "This content can't be displayed" in the
    channel list and on a lock screen, so ``text`` must be set too."""
    payload = failure_alert.render_slack(
        "[fa-web-api production] API failing", "intro line", ROWS
    )

    assert payload["text"] == "[fa-web-api production] API failing"
    body = payload["blocks"][1]["text"]["text"]
    assert "intro line" in body
    assert "*Environment:* production" in body
    assert "*Status code:* 500" in body


def test_a_long_subject_is_cut_rather_than_rejected():
    """Slack rejects a header block over 150 characters with a 400, which would
    lose the whole message."""
    payload = failure_alert.render_slack("x" * 400, "intro", ROWS)

    assert len(payload["blocks"][0]["text"]["text"]) == 150
    # The untruncated subject survives in the notification preview.
    assert len(payload["text"]) == 400


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


def test_the_webhook_url_never_appears_in_the_message_body(monkeypatch, channels):
    """It is a credential, and the message is rendered from rows we control — but
    assert it, because a future 'helpful' diagnostic row is exactly how a secret
    ends up in a channel a dozen people can read."""
    _settings(monkeypatch)
    _deliver()

    _url, payload = channels["slack"][0]
    import json

    assert WEBHOOK not in json.dumps(payload)

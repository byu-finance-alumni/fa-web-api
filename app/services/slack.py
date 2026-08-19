"""The one outbound Slack transport: a single incoming-webhook URL.

Sibling of ``app/services/mailer.py`` and deliberately the same shape. That
module is the app's ONE email transport; this is the app's ONE Slack transport,
so there is one webhook URL (``SLACK_ALERT_WEBHOOK_URL``), one timeout policy,
and one place to look when messages stop arriving in the channel.

Like the mailer, this module does NOTHING but transport. It does not decide what
is worth saying, does not dedupe, and does not retry — those are the caller's
policy, and today the only caller is ``app/services/failure_alert.py``, whose
rule is that an alerter must never raise and never retry (it runs while the
service is already unwell; a retry loop against a third party during an outage is
how a monitoring feature becomes part of the outage).

NO NEW DEPENDENCY. ``httpx`` is already the app's HTTP client (it is what the
mailer posts to Resend with), and a Slack incoming webhook is one ordinary JSON
POST. Nothing here needs an SDK.

THE WEBHOOK URL IS A CREDENTIAL. Anyone holding it can post to the channel, so it
lives only in backend config, is never returned by an endpoint, and is never
logged — including in this module's own error paths, which name the failure and
never the target.
"""

from __future__ import annotations

import httpx

# Slack's incoming-webhook host. Kept here so a test can assert what the app
# would talk to without a real webhook URL in the repo.
SLACK_WEBHOOK_HOST = "hooks.slack.com"


def escape_mrkdwn(value: str) -> str:
    """Escape the three characters Slack's mrkdwn treats as markup.

    Slack requires ``&``, ``<`` and ``>`` to be HTML-escaped in message text;
    everything else is literal. Without this, a value containing ``<`` starts a
    link or a user mention and the rest of the line disappears from the rendered
    message — an alert that silently loses its own contents is worse than no
    alert.

    The order matters: ``&`` first, or the ampersands introduced by the other two
    replacements get escaped a second time.
    """
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def post_webhook(url: str, *, payload: object, timeout: float) -> httpx.Response:
    """POST ``payload`` to a Slack incoming webhook and return the raw response.

    Raises only what httpx raises (``httpx.HTTPError`` and subclasses) for a
    transport-level failure; a non-2xx RESPONSE is returned as-is for the caller
    to interpret — the same contract as ``mailer.post_json``, so both channels
    can share one error policy.

    The client is created per call and closed on exit: this runs on serverless,
    where a module-level pooled client would outlive the frozen invocation that
    created it (the same reasoning as ``NullPool`` in ``app/core/database.py``
    and as the mailer's own per-call client).

    ``follow_redirects`` is deliberately left OFF (httpx's default). A webhook
    POST should land in one hop; silently following a redirect would re-send the
    body — which carries no secret, but is still an alert going somewhere nobody
    configured.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(
            url,
            headers={"Content-Type": "application/json"},
            json=payload,
        )

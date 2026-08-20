"""Alert-delivery-mode schemas (#458).

The engineer console's read and write shapes for the one setting that decides
whether an alert goes to Slack alone or to Slack AND the alert mailbox.

``mode`` is a ``Literal`` rather than a free string, so an unknown value is a 422
at the edge of the application, before any query runs and long before the
database's CHECK constraint has to catch it. The two spellings are the same two
strings as ``app.services.alert_delivery.SLACK_ONLY`` /
``SLACK_AND_EMAIL`` — a parity test asserts they cannot drift apart.

⚠️ ``slack_configured`` / ``email_configured`` ARE ON THE READ SHAPE ON PURPOSE.
The console's whole job here is to stop somebody reading "Slack only" as "we
will be silent if Slack breaks". That promise — the e-mail backstop still fires
when Slack does not land — is only TRUE if a mailbox is actually configured, and
the console cannot say so honestly without knowing. Neither field ever carries
the webhook URL or the recipients: they are booleans, because the URL is a
credential and the recipient list is somebody's address.
"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

#: The permitted values, spelled once for the schemas below.
AlertDeliveryModeName = Literal["slack_only", "slack_and_email"]


class AlertDeliveryState(BaseModel):
    """Engineer-console view of the alert-delivery setting.

    ``updated_at`` / ``updated_by_email`` are who-and-when for the console only.
    The durable record of the change is the audit trail
    (``set_alert_delivery_mode``), which survives the actor being deleted.
    """

    mode: AlertDeliveryModeName
    updated_at: datetime.datetime | None = None
    updated_by_email: str | None = None
    # Whether each channel has somewhere to send AT ALL (see the module note).
    slack_configured: bool = False
    email_configured: bool = False


class AlertDeliveryUpdate(BaseModel):
    """Set the delivery mode. ``extra="forbid"`` so a typo'd field is a 422
    rather than a silently ignored no-op on a control somebody just changed."""

    model_config = ConfigDict(extra="forbid")

    mode: AlertDeliveryModeName

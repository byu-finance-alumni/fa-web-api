"""Maintenance-mode schemas.

Two DELIBERATELY DIFFERENT shapes:

  * ``MaintenanceStatus`` is the PUBLIC (unauthenticated) payload. It carries
    exactly two fields — whether maintenance is on, and the public message — so
    that an anonymous caller learns nothing beyond what the maintenance page
    already displays. Do not add fields to it.
  * ``MaintenanceState`` is the engineer-console payload and may carry the
    operational detail (who, when).
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, Field


class MaintenanceStatus(BaseModel):
    """PUBLIC status — the only thing an unauthenticated caller may learn.

    ``enabled`` plus the engineer-authored public ``message``. NEVER add the
    actor, the timestamps, version/build info, or any other internal detail:
    this endpoint is reachable by anyone on the internet.
    """

    enabled: bool
    message: str | None = None


class MaintenanceState(MaintenanceStatus):
    """Engineer-console view: the public status plus operational detail."""

    enabled_at: datetime.datetime | None = None
    enabled_by_email: str | None = None


class MaintenanceEnableResult(MaintenanceState):
    """State after enabling, plus how many sessions the switch ended."""

    # Count of accounts whose active session was invalidated (engineers
    # excluded — see app/services/maintenance.py).
    sessions_ended: int = 0


class MaintenanceEnableRequest(BaseModel):
    """Optional override for the public maintenance message.

    Bounded because the value is rendered to the public. Omit (or send null) to
    use the default copy.
    """

    model_config = ConfigDict(extra="forbid")

    message: str | None = Field(default=None, max_length=500)

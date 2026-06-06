"""Event request/response schemas.

Covers the client-editable fields on the ``events`` core table. System-managed
columns (``logged_by_user_id``, timestamps) are set by the service layer, never
by the client.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class EventCreate(BaseModel):
    """Client-editable fields for creating an event. ``extra='forbid'`` rejects
    unknown keys; ``event_name`` is required, non-empty, and at most 255 chars."""

    model_config = ConfigDict(extra="forbid")

    event_name: str
    event_type: str | None = None
    event_date: datetime.date | None = None
    event_location: str | None = None
    event_notes: str | None = None

    @field_validator("event_name")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("event_name must not be empty.")
        if len(stripped) > 255:
            raise ValueError("event_name must be at most 255 characters.")
        return stripped

    @field_validator("event_type", "event_location", "event_notes")
    @classmethod
    def _blank_to_none(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

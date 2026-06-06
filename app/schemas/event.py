"""Event request/response schemas.

Covers the client-editable fields on the ``events`` core table. System-managed
columns (``logged_by_user_id``, timestamps) are set by the service layer, never
by the client.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, field_validator


# Max accepted lengths for the free-text event fields (defence-in-depth caps so
# a client cannot push unbounded text into the column / audit log).
_TYPE_MAX = 255
_LOCATION_MAX = 255
_NOTES_MAX = 10000


class EventCreate(BaseModel):
    """Client-editable fields for creating an event. ``extra='forbid'`` rejects
    unknown keys; ``event_name`` is required, non-empty, and at most 255 chars.
    ``event_type``/``event_location`` are capped at 255 chars and ``event_notes``
    at 10000 chars."""

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

    @field_validator("event_type", "event_location")
    @classmethod
    def _blank_to_none_capped(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > _TYPE_MAX:
            raise ValueError(f"must be at most {_TYPE_MAX} characters.")
        return stripped

    @field_validator("event_notes")
    @classmethod
    def _notes_blank_to_none_capped(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > _NOTES_MAX:
            raise ValueError(f"must be at most {_NOTES_MAX} characters.")
        return stripped


class EventUpdate(BaseModel):
    """Partial update of an event's client-editable fields (full_access). Every
    field is optional; only the keys actually sent are applied. Reuses
    ``EventCreate``'s validators (non-empty / length caps), but ``event_name``
    may be omitted — only an explicitly provided blank name is rejected."""

    model_config = ConfigDict(extra="forbid")

    event_name: str | None = None
    event_type: str | None = None
    event_date: datetime.date | None = None
    event_location: str | None = None
    event_notes: str | None = None

    @field_validator("event_name")
    @classmethod
    def _name_non_empty_if_present(cls, value: str | None) -> str | None:
        # Omitted (None) is fine for a partial update; an explicit name must be
        # non-empty and within the cap (reuses EventCreate's rule).
        if value is None:
            return None
        return EventCreate._name_non_empty.__func__(cls, value)  # type: ignore[attr-defined]

    # Reuse EventCreate's free-text validators so the rules stay in one place.
    _validate_type_location = field_validator("event_type", "event_location")(
        EventCreate._blank_to_none_capped.__func__  # type: ignore[attr-defined]
    )
    _validate_notes = field_validator("event_notes")(
        EventCreate._notes_blank_to_none_capped.__func__  # type: ignore[attr-defined]
    )

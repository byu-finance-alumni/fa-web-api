"""Unified-notes request/response schemas.

A note attaches to exactly one of three entity types — an alumni profile, an
interaction, or an event — expressed on the wire as ``(entity_type, entity_id)``.
The service maps that pair to the right FK column on the ``notes`` table.

Write (create/edit) is ``full_access`` and up; read is any view-access role.
Author identity and timestamps are system-managed and returned read-only.
"""

from __future__ import annotations

import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Defence-in-depth cap so a client cannot push unbounded free text into the
# column or the audit snapshot. Matches the event/interaction notes cap.
_BODY_MAX = 10000


class NoteEntityType(StrEnum):
    """The three levels a note can attach to."""

    ALUMNI = "alumni"
    INTERACTION = "interaction"
    EVENT = "event"


def _clean_body(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError("Note body must not be empty.")
    if len(stripped) > _BODY_MAX:
        raise ValueError(f"Note body must be at most {_BODY_MAX} characters.")
    return stripped


class NoteCreate(BaseModel):
    """Body for creating a note. ``extra='forbid'`` rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")

    entity_type: NoteEntityType
    entity_id: int = Field(gt=0)
    body: str

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        return _clean_body(value)


class NoteUpdate(BaseModel):
    """Body for editing a note. Only the free-text body is mutable; the attach
    target is fixed at creation."""

    model_config = ConfigDict(extra="forbid")

    body: str

    @field_validator("body")
    @classmethod
    def _validate_body(cls, value: str) -> str:
        return _clean_body(value)


class NoteRead(BaseModel):
    """A note as returned to clients. ``author`` is the resolved display name of
    the creator (snapshot of the user record at read time, or ``None`` if the
    user was deleted)."""

    model_config = ConfigDict(from_attributes=True)

    note_id: int
    entity_type: NoteEntityType
    entity_id: int
    body: str
    author: str | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime

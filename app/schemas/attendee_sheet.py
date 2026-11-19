"""Schemas for the event attendee spreadsheet import (api#693).

The preview models mirror the dicts ``app/services/attendee_sheet.preview``
returns. The commit body is what the confirm screen sends back: every row the
reviewer kept, with their (possibly edited) values and choice. It is validated
here as untrusted input — the server never relies on the preview having run.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.attendee_sheet import MAX_SHEET_ROWS

SheetStatus = Literal[
    "matched", "ambiguous", "new_alumni", "new_friend", "already_attending", "invalid"
]
SheetAction = Literal["match", "new_alumni", "new_friend", "skip"]


class SheetCandidate(BaseModel):
    alumni_id: int
    name: str
    graduation_year: int | None = None
    graduate_graduation_year: int | None = None
    is_alumni: bool
    friend_id: str | None = None
    current_employer: str | None = None


class SheetRowFields(BaseModel):
    """The row as the file gave it (after trimming / year parsing)."""

    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    role: str | None = None
    email: str | None = None
    focus: str | None = None
    ug: str | None = None
    ug_year: int | None = None
    linkedin: str | None = None
    grad: str | None = None
    grad_year: int | None = None
    net_id: str | None = None


class SheetPreviewRow(SheetRowFields):
    row: int
    status: SheetStatus
    # The suggested choice; the confirm screen may change it.
    action: SheetAction
    # Set for a single match (``matched`` / ``already_attending``); ``None`` for
    # an ambiguous row until the reviewer picks a candidate.
    alumni_id: int | None = None
    candidates: list[SheetCandidate] = []
    warnings: list[str] = []


class SheetPreviewSummary(BaseModel):
    total_rows: int
    matched: int
    ambiguous: int
    new_alumni: int
    new_friend: int
    already_attending: int
    invalid: int


class SheetPreview(BaseModel):
    """``POST /events/{id}/attendees/sheet/preview`` — writes nothing."""

    columns_ok: bool
    header_errors: list[str]
    ignored_columns: list[str]
    summary: SheetPreviewSummary
    rows: list[SheetPreviewRow]
    warnings: list[str]


# --- Commit ------------------------------------------------------------------


def _clean(value: str | None, cap: int) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    if len(cleaned) > cap:
        raise ValueError(f"must be at most {cap} characters.")
    return cleaned


class SheetCommitRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row: int = Field(ge=1)
    action: SheetAction
    alumni_id: int | None = Field(default=None, ge=1)
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    role: str | None = None
    email: str | None = None
    linkedin: str | None = None
    ug_year: int | None = Field(default=None, ge=1900, le=2100)
    grad_year: int | None = Field(default=None, ge=1900, le=2100)
    net_id: str | None = None

    @field_validator("first_name", "last_name")
    @classmethod
    def _names(cls, v: str | None) -> str | None:
        return _clean(v, 100)

    @field_validator("company", "role", "email")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        return _clean(v, 255)

    @field_validator("linkedin")
    @classmethod
    def _linkedin(cls, v: str | None) -> str | None:
        return _clean(v, 500)

    @field_validator("net_id")
    @classmethod
    def _net_id(cls, v: str | None) -> str | None:
        cleaned = _clean(v, 12)
        return cleaned.lower() if cleaned else None

    @model_validator(mode="after")
    def _action_needs(self) -> SheetCommitRow:
        if self.action == "match" and self.alumni_id is None:
            raise ValueError(f"row {self.row}: pick the record to match.")
        if self.action in ("new_alumni", "new_friend") and not (
            self.first_name and self.last_name
        ):
            raise ValueError(f"row {self.row}: a first and last name are needed.")
        return self


class SheetCommitRequest(BaseModel):
    """``POST /events/{id}/attendees/sheet/commit`` body."""

    model_config = ConfigDict(extra="forbid")

    rows: list[SheetCommitRow] = Field(max_length=MAX_SHEET_ROWS)


SheetItemStatus = Literal[
    "added",
    "created_alumni",
    "created_friend",
    "already_attending",
    "skipped",
    "not_found",
    "rejected",
]


class SheetCommitItem(BaseModel):
    row: int
    status: SheetItemStatus
    alumni_id: int | None = None
    friend_id: str | None = None
    name: str | None = None
    message: str | None = None


class SheetCommitResult(BaseModel):
    event_id: int
    added: int
    created_alumni: int
    created_friends: int
    already_attending: int
    skipped: int
    not_found: int
    rejected: int
    items: list[SheetCommitItem]

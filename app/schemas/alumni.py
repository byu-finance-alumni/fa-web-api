"""Alumni request/response schemas.

Covers the editable fields on the ``alumni`` core table. Related detail
(contact info, employment, education, ...) lives in separate tables and gets its
own schemas as those endpoints are built. System-managed columns
(``archived``, ``manually_edited_at``, ``last_imported_at``, timestamps) are
read-only here — they're set by the service/import layers, never by the client.
"""

from __future__ import annotations

import datetime

from pydantic import BaseModel, ConfigDict, model_validator


class AlumniBase(BaseModel):
    """Client-editable fields. ``extra='forbid'`` rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")

    byu_id: str | None = None
    mst_id: str | None = None
    net_id: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    preferred_first_name: str | None = None
    birth_name: str | None = None
    gender: str | None = None
    birth_year: int | None = None
    graduation_year: int | None = None
    finance_program_year: int | None = None
    graduate_degree: str | None = None
    deceased: bool | None = None
    linkedin_url: str | None = None
    notes: str | None = None


class AlumniCreate(AlumniBase):
    @model_validator(mode="after")
    def _require_identifier(self) -> AlumniCreate:
        if not (self.first_name or self.last_name or self.byu_id):
            raise ValueError(
                "Provide at least one of first_name, last_name, or byu_id."
            )
        return self


class AlumniUpdate(AlumniBase):
    """All fields optional — only those sent (``exclude_unset``) are applied."""


class AlumniRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    alumni_id: int
    source_id: int | None = None
    byu_id: str | None = None
    mst_id: str | None = None
    net_id: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    preferred_first_name: str | None = None
    birth_name: str | None = None
    gender: str | None = None
    birth_year: int | None = None
    graduation_year: int | None = None
    finance_program_year: int | None = None
    graduate_degree: str | None = None
    deceased: bool
    linkedin_url: str | None = None
    notes: str | None = None
    archived: bool
    manually_edited_at: datetime.datetime | None = None
    last_imported_at: datetime.datetime | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class AlumniPage(BaseModel):
    """A page of alumni plus the pagination envelope."""

    items: list[AlumniRead]
    total: int
    limit: int
    offset: int

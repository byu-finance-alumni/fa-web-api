"""Pydantic schemas for engineer-managed support contacts.

Read shape is shown to any logged-in user (in-app error screen); create/update
are engineer-only. Email is shape-checked (no email-validator dependency, like
the rest of the project — see app/api/routes/admin.py) and stored lowercased.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_str(value: object, *, field: str, max_len: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be a string.")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty.")
    if len(value) > max_len:
        raise ValueError(f"{field} must be at most {max_len} characters.")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("Must not contain control characters.")
    return value


def _clean_email(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Must be a string.")
    value = value.strip().lower()
    if not _EMAIL_RE.match(value) or len(value) > 255:
        raise ValueError("Must be a valid email address.")
    return value


class SupportContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    support_contact_id: int
    role_label: str
    name: str
    email: str
    sort_order: int


class SupportContactCreate(BaseModel):
    """Add a support contact (engineer only)."""

    model_config = ConfigDict(extra="forbid")

    role_label: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    sort_order: int = Field(default=0, ge=0, le=9999)

    @field_validator("role_label", mode="before")
    @classmethod
    def _v_role(cls, value: object) -> str:
        return _clean_str(value, field="Role label", max_len=100)

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, value: object) -> str:
        return _clean_str(value, field="Name", max_len=255)

    @field_validator("email", mode="before")
    @classmethod
    def _v_email(cls, value: object) -> str:
        return _clean_email(value)


class SupportContactUpdate(BaseModel):
    """Edit a support contact (engineer only). Only fields present are applied."""

    model_config = ConfigDict(extra="forbid")

    role_label: str | None = Field(default=None, min_length=1, max_length=100)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    sort_order: int | None = Field(default=None, ge=0, le=9999)

    @field_validator("role_label", mode="before")
    @classmethod
    def _v_role(cls, value: object) -> str | None:
        return None if value is None else _clean_str(value, field="Role label", max_len=100)

    @field_validator("name", mode="before")
    @classmethod
    def _v_name(cls, value: object) -> str | None:
        return None if value is None else _clean_str(value, field="Name", max_len=255)

    @field_validator("email", mode="before")
    @classmethod
    def _v_email(cls, value: object) -> str | None:
        return None if value is None else _clean_email(value)

"""Pydantic schemas for engineer / super-admin-managed dashboard quick-filter
presets.

The read shape is shown to any logged-in user (dashboard Quick search tab);
create/update are restricted to engineer + super_admin. ``href`` must be a
relative in-app path (starts with a single "/") so a preset can never redirect
off-site.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


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


def _clean_href(value: object) -> str:
    href = _clean_str(value, field="Link", max_len=500)
    # Relative in-app links only: a single leading slash, never "//" or "/\"
    # (which browsers treat as protocol-relative = off-site).
    if not href.startswith("/") or href.startswith("//") or href.startswith("/\\"):
        raise ValueError("Link must be a relative in-app path, e.g. /alumni?...")
    return href


class DashboardPresetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    dashboard_preset_id: int
    label: str
    href: str
    sort_order: int


class DashboardPresetCreate(BaseModel):
    """Add a quick-filter preset (engineer / super_admin)."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=200)
    href: str = Field(min_length=1, max_length=500)
    sort_order: int = Field(default=0, ge=0, le=9999)

    @field_validator("label", mode="before")
    @classmethod
    def _v_label(cls, value: object) -> str:
        return _clean_str(value, field="Label", max_len=200)

    @field_validator("href", mode="before")
    @classmethod
    def _v_href(cls, value: object) -> str:
        return _clean_href(value)


class DashboardPresetUpdate(BaseModel):
    """Edit a quick-filter preset. Only fields present are applied."""

    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=200)
    href: str | None = Field(default=None, min_length=1, max_length=500)
    sort_order: int | None = Field(default=None, ge=0, le=9999)

    @field_validator("label", mode="before")
    @classmethod
    def _v_label(cls, value: object) -> str | None:
        return None if value is None else _clean_str(value, field="Label", max_len=200)

    @field_validator("href", mode="before")
    @classmethod
    def _v_href(cls, value: object) -> str | None:
        return None if value is None else _clean_href(value)

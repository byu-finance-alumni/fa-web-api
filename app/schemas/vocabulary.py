"""Pydantic schemas for the editable controlled vocabulary (#82)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.vocabularies import VocabularyCategory


def _clean_value(value: object) -> str:
    """Trim + validate a vocabulary term value (1–100 chars, no control chars)."""
    if not isinstance(value, str):
        raise ValueError("Must be a string.")
    value = value.strip()
    if not value:
        raise ValueError("Must not be empty.")
    if len(value) > 100:
        raise ValueError("Must be at most 100 characters.")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("Must not contain control characters.")
    return value


class VocabularyTermRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    term_id: int
    category: str
    value: str
    sort_order: int
    active: bool


class VocabularyTermCreate(BaseModel):
    """Create a vocabulary term. ``category`` must be one of the known
    categories (422 otherwise); ``value`` is trimmed and length-checked."""

    model_config = ConfigDict(extra="forbid")

    category: VocabularyCategory
    value: str = Field(min_length=1, max_length=100)
    sort_order: int = 0

    @field_validator("value", mode="before")
    @classmethod
    def _v(cls, value: object) -> str:
        return _clean_value(value)


class VocabularyTermUpdate(BaseModel):
    """Edit a term. Any subset of fields; only those present are applied
    (``exclude_unset``). ``category`` is immutable, so it is not editable here."""

    model_config = ConfigDict(extra="forbid")

    value: str | None = Field(default=None, min_length=1, max_length=100)
    sort_order: int | None = None
    active: bool | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _v(cls, value: object) -> str | None:
        if value is None:
            return None
        return _clean_value(value)

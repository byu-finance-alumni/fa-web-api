"""Donation request schemas — Pay It Forward Fund (#161).

Only the request bodies are modeled here. Responses are assembled as plain dicts
in the router so dollar AMOUNT fields can be nulled per-caller (field-level
gating: full_access+ see amounts, everyone else sees donor identity only) — a
fixed ``response_model`` would force the amount keys to always serialize.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_validator

_NOTES_MAX = 10000
_AMOUNT_MAX = Decimal("9999999999.99")  # numeric(12,2) ceiling


class DonationCreate(BaseModel):
    """Body for adding a donation to an alumnus (super_admin). ``extra='forbid'``
    rejects unknown keys. ``amount`` is required and non-negative; ``year`` is
    required; ``month`` is optional (1-12); ``notes`` is optional free text."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    year: int
    month: int | None = None
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, value: Decimal) -> Decimal:
        # A gift must be a positive amount — $0 (and negative-zero) carries no
        # financial meaning and would skew lifetime/per-year roll-ups.
        if value <= 0:
            raise ValueError("amount must be greater than 0.")
        if value > _AMOUNT_MAX:
            raise ValueError("amount is too large.")
        # Quantize to cents so a 3-decimal input can't silently round in the DB.
        return value.quantize(Decimal("0.01"))

    @field_validator("year")
    @classmethod
    def _year_range(cls, value: int) -> int:
        if not 1900 <= value <= 2200:
            raise ValueError("year must be between 1900 and 2200.")
        return value

    @field_validator("month")
    @classmethod
    def _month_range(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not 1 <= value <= 12:
            raise ValueError("month must be between 1 and 12.")
        return value

    @field_validator("notes")
    @classmethod
    def _notes_blank_to_none_capped(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > _NOTES_MAX:
            raise ValueError(f"notes must be at most {_NOTES_MAX} characters.")
        return stripped


class DonationUpdate(BaseModel):
    """Partial update of a donation (super_admin). Every field optional; only the
    keys sent are applied. Reuses ``DonationCreate``'s validators."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal | None = None
    year: int | None = None
    month: int | None = None
    notes: str | None = None

    @field_validator("amount")
    @classmethod
    def _amount_positive(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return None
        return DonationCreate._amount_positive.__func__(cls, value)  # type: ignore[attr-defined]

    @field_validator("year")
    @classmethod
    def _year_range(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return DonationCreate._year_range.__func__(cls, value)  # type: ignore[attr-defined]

    _validate_month = field_validator("month")(
        DonationCreate._month_range.__func__  # type: ignore[attr-defined]
    )
    _validate_notes = field_validator("notes")(
        DonationCreate._notes_blank_to_none_capped.__func__  # type: ignore[attr-defined]
    )

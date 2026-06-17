"""Alumni request/response schemas.

Covers the editable fields on the ``alumni`` core table. Related detail
(contact info, employment, education, ...) lives in separate tables and gets its
own schemas as those endpoints are built. System-managed columns
(``archived``, ``manually_edited_at``, ``last_imported_at``, timestamps) are
read-only here — they're set by the service/import layers, never by the client.
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.dropdowns import validate_industry

# --- Validation constants ----------------------------------------------------

# Names: deny-list (permissive) so international / Unicode names pass. We reject
# characters that carry no meaning inside a human name but ARE meaningful to a
# SQL parser (semicolons, comparison operators, pipes) plus any control chars.
# Allowed: Unicode letters, spaces, apostrophes (straight + curly), hyphens,
# periods — so O'Brien, Anne-Marie and St. John pass while ``' OR 1=1;--``
# fails on the ``;`` and ``=``.
_NAME_DISALLOWED = set(";=<>|")
# Lengths mirror database/schema.sql so we never accept a value the DB rejects.
_NAME_MAX = 100
_GENDER_MAX = 30
_GRADUATE_DEGREE_MAX = 100
_LINKEDIN_MAX = 500
_NOTES_MAX = 10000

# byu_id: no seed/mock data exists with a byu_id yet (checked database/ and
# scripts/), so we enforce the canonical BYU NetID-card length of exactly 9
# digits. If seeds later use 8 or 10, widen this to {8,10}.
_BYU_ID_RE = re.compile(r"^\d{9}$")
_NET_ID_RE = re.compile(r"^[a-z0-9]{2,12}$")

_YEAR_MIN = 1950
_YEAR_MAX = datetime.date.today().year + 10

# Birthdays: anyone in an alumni database was plausibly born no earlier than
# 1900; a birth date can't be in the future. Same bounds apply to a spouse.
_BIRTH_DATE_MIN = datetime.date(1900, 1, 1)


def _has_control_chars(value: str) -> bool:
    """True if *value* contains C0/C1 control characters (category ``Cc``)."""
    return any(unicodedata.category(ch) == "Cc" for ch in value)


def _empty_to_none(value: str | None) -> str | None:
    """Trim and normalize empty/whitespace-only strings to ``None``."""
    if value is None:
        return None
    value = value.strip()
    return value or None


class AlumniBase(BaseModel):
    """Client-editable fields. ``extra='forbid'`` rejects unknown keys.

    Field validators add *semantic* validation on top of parameterization:
    parameterized queries stop injection from executing, but only validation
    keeps SQL-shaped garbage (``' OR 1=1;--``) out of the data in the first
    place. The deny-list name rules are deliberately permissive so legitimate
    international names still pass.
    """

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
    birth_date: datetime.date | None = None
    graduation_year: int | None = None
    finance_program_year: int | None = None
    graduate_degree: str | None = None
    spouse_first_name: str | None = None
    spouse_last_name: str | None = None
    spouse_birth_date: datetime.date | None = None
    spouse_alumni_id: int | None = None
    deceased: bool | None = None
    linkedin_url: str | None = None
    notes: str | None = None

    # --- Name fields ---------------------------------------------------------

    @field_validator(
        "first_name",
        "middle_name",
        "last_name",
        "preferred_first_name",
        "birth_name",
        "spouse_first_name",
        "spouse_last_name",
        mode="before",
    )
    @classmethod
    def _validate_name(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if len(value) > _NAME_MAX:
            raise ValueError(f"Must be at most {_NAME_MAX} characters.")
        if _has_control_chars(value):
            raise ValueError("Must not contain control characters.")
        bad = sorted(_NAME_DISALLOWED & set(value))
        if bad:
            raise ValueError(
                "Must not contain these characters: " + " ".join(bad)
            )
        # Digits-only strings are never valid names (e.g. an ID typed by mistake).
        if value.isdigit():
            raise ValueError("Must not be only digits.")
        return value

    # --- Identifiers ---------------------------------------------------------

    @field_validator("byu_id", mode="before")
    @classmethod
    def _validate_byu_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if not _BYU_ID_RE.match(value):
            raise ValueError("Must be exactly 9 digits.")
        return value

    @field_validator("net_id", mode="before")
    @classmethod
    def _validate_net_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = value.strip().lower()
        if not value:
            return None
        if not _NET_ID_RE.match(value):
            raise ValueError(
                "Must be 2-12 lowercase letters and digits."
            )
        return value

    @field_validator("mst_id", mode="before")
    @classmethod
    def _normalize_mst_id(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        return _empty_to_none(value)

    # --- Years ---------------------------------------------------------------

    @field_validator("graduation_year", "finance_program_year")
    @classmethod
    def _validate_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (_YEAR_MIN <= value <= _YEAR_MAX):
            raise ValueError(f"Must be between {_YEAR_MIN} and {_YEAR_MAX}.")
        return value

    # --- Birthdays -----------------------------------------------------------

    @field_validator("birth_date", "spouse_birth_date")
    @classmethod
    def _validate_birth_date(
        cls, value: datetime.date | None
    ) -> datetime.date | None:
        # Pydantic has already coerced an ISO "YYYY-MM-DD" string to a date.
        if value is None:
            return None
        today = datetime.date.today()
        if not (_BIRTH_DATE_MIN <= value <= today):
            raise ValueError(
                f"Must be between {_BIRTH_DATE_MIN.isoformat()} and today."
            )
        return value

    # --- Spouse link ---------------------------------------------------------

    @field_validator("spouse_alumni_id")
    @classmethod
    def _validate_spouse_alumni_id(cls, value: int | None) -> int | None:
        # Existence and not-self are checked in the service (it has the DB
        # session and, on update, the alumnus's own id); here we just reject
        # structurally invalid ids early.
        if value is None:
            return None
        if value <= 0:
            raise ValueError("Must be a positive alumni id.")
        return value

    # --- Free-text columns ---------------------------------------------------

    @field_validator("gender", mode="before")
    @classmethod
    def _validate_gender(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if len(value) > _GENDER_MAX:
            raise ValueError(f"Must be at most {_GENDER_MAX} characters.")
        return value

    @field_validator("graduate_degree", mode="before")
    @classmethod
    def _validate_graduate_degree(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if len(value) > _GRADUATE_DEGREE_MAX:
            raise ValueError(
                f"Must be at most {_GRADUATE_DEGREE_MAX} characters."
            )
        return value

    @field_validator("notes", mode="before")
    @classmethod
    def _validate_notes(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if len(value) > _NOTES_MAX:
            raise ValueError(f"Must be at most {_NOTES_MAX} characters.")
        return value

    # --- LinkedIn URL --------------------------------------------------------

    @field_validator("linkedin_url", mode="before")
    @classmethod
    def _validate_linkedin_url(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if len(value) > _LINKEDIN_MAX:
            raise ValueError(f"Must be at most {_LINKEDIN_MAX} characters.")
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Must be an http(s) URL.")
        host = parts.hostname.lower()
        if host != "linkedin.com" and not host.endswith(".linkedin.com"):
            raise ValueError("Must be a linkedin.com URL.")
        return value


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


# --- Optional nested write sections ------------------------------------------
#
# Each section is all-optional and maps 1:1 to a related table's columns. Empty
# strings are normalized to ``None`` (mirroring ``_empty_to_none``) so a blank
# input never persists an empty string. These are written by the service only
# when a section has at least one non-empty value (see ``has_values``).


class _Section(BaseModel):
    """Base for nested write sections: forbid unknown keys, trim strings."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _trim_strings(cls, value: object) -> object:
        if isinstance(value, str):
            return _empty_to_none(value)
        return value

    def has_values(self) -> bool:
        """True if any field carries a meaningful (non-None/non-False) value.

        Booleans default to ``False`` and a section of only-False flags is
        treated as "nothing to write" — matches the DB ``server_default`` of
        ``false`` so we don't create rows for untouched engagement profiles.
        """
        for value in self.model_dump().values():
            if value is None or value is False:
                continue
            return True
        return False


class ContactCreate(_Section):
    personal_email: str | None = None
    work_email: str | None = None
    phone: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    country: str | None = None
    region: str | None = None


class CareerCreate(_Section):
    current_employer: str | None = None
    current_title: str | None = None
    current_industry: str | None = None
    current_industry_secondary: str | None = None
    current_city: str | None = None
    current_state: str | None = None
    current_country: str | None = None
    current_zip: str | None = None
    seniority_level: str | None = None

    @field_validator(
        "current_industry", "current_industry_secondary", mode="before"
    )
    @classmethod
    def _validate_industry(cls, value: object) -> str | None:
        if value is not None and not isinstance(value, str):
            raise ValueError("Must be a string.")
        # validate_industry trims, normalizes empty -> None, and raises on a
        # value outside the canonical INDUSTRIES list (surfaced as a 422).
        return validate_industry(value)


class EducationCreate(_Section):
    university: str | None = None
    college: str | None = None
    department: str | None = None
    degree: str | None = None
    major: str | None = None
    degree_status: str | None = None
    degree_year: int | None = None

    @field_validator("degree_year")
    @classmethod
    def _validate_degree_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (_YEAR_MIN <= value <= _YEAR_MAX):
            raise ValueError(f"Must be between {_YEAR_MIN} and {_YEAR_MAX}.")
        return value


class EngagementCreate(_Section):
    nettrek_host_willing: bool = False
    finance_conference_willing: bool = False
    mentor_willing: bool = False
    company_event_sponsor_willing: bool = False
    guest_speaker_willing: bool = False
    help_at_event_willing: bool = False
    case_competition_host_willing: bool = False
    women_in_finance_mentor_willing: bool = False
    hired_finance_intern: bool = False
    hired_finance_full_time: bool = False
    piff_donor: bool = False
    cfp_designation: bool = False
    cfa_designation: bool = False
    engagement_notes: str | None = None


class AlumniCreateFull(AlumniCreate):
    """Create payload: required core fields plus optional nested sections.

    Core validation (``_require_identifier``, name/id/year/linkedin rules) is
    inherited unchanged from ``AlumniCreate``. The nested sections are written
    by the service only when they contain at least one non-empty value.
    """

    contact: ContactCreate | None = None
    career: CareerCreate | None = None
    education: EducationCreate | None = None
    engagement: EngagementCreate | None = None


class AlumniUpdateFull(AlumniUpdate):
    """Update payload: optional core fields plus optional nested sections.

    Mirrors ``AlumniCreateFull`` so the edit wizard can persist every section.
    Core stays all-optional (inherited from ``AlumniUpdate``); each nested
    section is upserted by the service only when it carries a non-empty value.
    The same section schemas are reused, so industry validation and empty-string
    trimming behave identically to create.
    """

    contact: ContactCreate | None = None
    career: CareerCreate | None = None
    education: EducationCreate | None = None
    engagement: EngagementCreate | None = None


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
    birth_date: datetime.date | None = None
    graduation_year: int | None = None
    finance_program_year: int | None = None
    graduate_degree: str | None = None
    spouse_first_name: str | None = None
    spouse_last_name: str | None = None
    spouse_birth_date: datetime.date | None = None
    spouse_alumni_id: int | None = None
    deceased: bool
    linkedin_url: str | None = None
    notes: str | None = None
    archived: bool
    manually_edited_at: datetime.datetime | None = None
    last_imported_at: datetime.datetime | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class AlumniListItem(AlumniRead):
    """List-row variant: adds the alumnus's current employer + industry (joined
    from ``current_employment``) for the alumni table. Single-record reads use
    plain ``AlumniRead``, which omits these."""

    current_employer: str | None = None
    current_industry: str | None = None


class AlumniPage(BaseModel):
    """A page of alumni plus the pagination envelope."""

    items: list[AlumniListItem]
    total: int
    limit: int
    offset: int


# --- FERPA role-scoping ------------------------------------------------------
#
# Fields a ``view_only`` ("Professor") caller must NOT receive on any alumni
# read. These are sensitive PII (national/birth identifiers, demographics,
# spouse PII), free-text notes, and import-provenance metadata that has no
# bearing on the read-only directory view. They are NULLED (not dropped) so the
# response still validates against ``AlumniRead`` / ``AlumniListItem``.
VIEW_ONLY_HIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "byu_id",
        "net_id",
        "mst_id",
        "birth_date",
        "birth_year",
        "gender",
        "spouse_first_name",
        "spouse_last_name",
        "spouse_birth_date",
        "notes",
        "manually_edited_at",
        "last_imported_at",
        "source_id",
    }
)


def minimize_alumni_read[T: AlumniRead](read: T, *, can_edit: bool) -> T:
    """Null the FERPA-sensitive fields for a ``view_only`` caller.

    ``can_edit`` is ``user.can_edit_alumni`` (engineer / super_admin /
    full_access / student). Those callers get the record untouched; only
    ``view_only`` is scoped down. Returns a copy (the input is left intact).
    """
    if can_edit:
        return read
    return read.model_copy(
        update={field: None for field in VIEW_ONLY_HIDDEN_FIELDS}
    )

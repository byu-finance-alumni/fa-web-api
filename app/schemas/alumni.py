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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
# Secondary affiliation / education (#47). Short single-value fields mirror the
# related-table varchar(255) convention; the narrative free-text fields share
# the generous notes-style cap.
_AFFILIATION_NAME_MAX = 255
_AFFILIATION_TEXT_MAX = 10000

# Survey / demographics + graduation-detail widths mirror database/schema.sql
# (migrations/2026-07-08_add_alumni_survey_citizenship_grad_fields.sql).
_CITIZENSHIP_MAX = 100
_MARITAL_STATUS_MAX = 50
_HOME_COUNTRY_MAX = 100
_EMPLOYMENT_STATUS_MAX = 50
_OTHER_DESIGNATIONS_MAX = 10000
_GRADUATION_SEMESTER_MAX = 20
_PROFILE_UPDATED_BY_MAX = 200

# byu_id: no seed/mock data exists with a byu_id yet (checked database/ and
# scripts/), so we enforce the canonical BYU NetID-card length of exactly 9
# digits. If seeds later use 8 or 10, widen this to {8,10}.
_BYU_ID_RE = re.compile(r"^\d{9}$")
_NET_ID_RE = re.compile(r"^[a-z0-9]{2,12}$")

_YEAR_MIN = 1950
_YEAR_MAX = datetime.date.today().year + 10

# Which contact method an alum flags as "preferred". A single nullable string on
# the contact record naming WHICH method is preferred (NULL/empty = none). Kept
# in sync with the frontend star affordance; validated once via this set below.
_PREFERRED_CONTACT_METHODS = frozenset(
    {"personal_email", "work_email", "phone", "linkedin"}
)

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
    graduation_month: int | None = None
    # Semester + graduating class supersede graduation_month in the API read
    # schema; graduation_month + its validator stay for back-compat writes.
    graduation_semester: str | None = Field(
        default=None, max_length=_GRADUATION_SEMESTER_MAX
    )
    graduation_class: int | None = None
    finance_program_year: int | None = None
    graduate_degree: str | None = None
    # Survey / demographics (all optional, nullable, additive). Free-text
    # single-value fields; other_designations shares the generous notes-style cap.
    citizenship: str | None = Field(default=None, max_length=_CITIZENSHIP_MAX)
    marital_status: str | None = Field(default=None, max_length=_MARITAL_STATUS_MAX)
    home_country: str | None = Field(default=None, max_length=_HOME_COUNTRY_MAX)
    employment_status: str | None = Field(
        default=None, max_length=_EMPLOYMENT_STATUS_MAX
    )
    other_designations: str | None = Field(
        default=None, max_length=_OTHER_DESIGNATIONS_MAX
    )
    survey_completed_date: datetime.date | None = None
    profile_updated_date: datetime.date | None = None
    # Free-text "updated by" NAME from the intake sheet (as typed). DISTINCT from
    # the profile_updated_by_user_id FK (set by the service, never the client).
    profile_updated_by: str | None = Field(
        default=None, max_length=_PROFILE_UPDATED_BY_MAX
    )
    # Secondary affiliation / education (#47, PRD section 6). All optional.
    mba_program: str | None = None
    law_school: str | None = None
    medical_school: str | None = None
    graduate_school: str | None = None
    startup_involvement: str | None = None
    advisory_roles: str | None = None
    secondary_employment: str | None = None
    spouse_first_name: str | None = None
    spouse_last_name: str | None = None
    spouse_birth_date: datetime.date | None = None
    spouse_alumni_id: int | None = None
    deceased: bool | None = None
    # Friends of the finance program (#218). Omitted on existing alumni payloads
    # -> stays None here and the DB ``server_default`` of true applies, so the
    # record remains an alumnus. Send ``false`` to create/flag a "friend".
    is_alumni: bool | None = None
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
        # A leading =, +, -, or @ turns the cell into a live formula when the
        # value is later exported to CSV/Excel (formula injection). '=' is already
        # blocked below; block a LEADING +/-/@ here (they're legitimate mid-name,
        # e.g. hyphenated surnames, so only the first char is rejected).
        if value[0] in "+-@":
            raise ValueError("Must not start with '+', '-', or '@'.")
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
        # Strip formatting (dashes/spaces) before validating, matching the spouse
        # BYU-ID resolver and hygiene._clean_byu_id — a dashed "900-11-2233" is an
        # ordinary way to paste a 9-digit id and shouldn't hard-reject the row.
        value = re.sub(r"\D", "", value)
        if not _BYU_ID_RE.match(value):
            raise ValueError("Must be exactly 9 digits (remove any dashes or spaces).")
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

    @field_validator("graduation_year", "finance_program_year", "graduation_class")
    @classmethod
    def _validate_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (_YEAR_MIN <= value <= _YEAR_MAX):
            raise ValueError(f"Must be between {_YEAR_MIN} and {_YEAR_MAX}.")
        return value

    @field_validator("graduation_month")
    @classmethod
    def _validate_grad_month(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (1 <= value <= 12):
            raise ValueError("Must be between 1 and 12.")
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

    # --- Survey / demographics (additive, all optional) ----------------------

    @field_validator(
        "graduation_semester",
        "citizenship",
        "marital_status",
        "home_country",
        "employment_status",
        "other_designations",
        "profile_updated_by",
        mode="before",
    )
    @classmethod
    def _validate_survey_text(cls, value: object) -> str | None:
        # Trim + normalize empty -> None; reject control chars. Length caps are
        # enforced by each field's Field(max_length=...) after this runs.
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if _has_control_chars(value):
            raise ValueError("Must not contain control characters.")
        return value

    # --- Secondary affiliation / education (#47) -----------------------------

    @field_validator(
        "mba_program",
        "law_school",
        "medical_school",
        "graduate_school",
        mode="before",
    )
    @classmethod
    def _validate_affiliation_name(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if _has_control_chars(value):
            raise ValueError("Must not contain control characters.")
        if len(value) > _AFFILIATION_NAME_MAX:
            raise ValueError(
                f"Must be at most {_AFFILIATION_NAME_MAX} characters."
            )
        return value

    @field_validator(
        "startup_involvement",
        "advisory_roles",
        "secondary_employment",
        mode="before",
    )
    @classmethod
    def _validate_affiliation_text(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Must be a string.")
        value = _empty_to_none(value)
        if value is None:
            return None
        if len(value) > _AFFILIATION_TEXT_MAX:
            raise ValueError(
                f"Must be at most {_AFFILIATION_TEXT_MAX} characters."
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
            cleaned = _empty_to_none(value)
            # Reject NUL/other C0 control bytes here, at preview time — Postgres
            # rejects a literal NUL in a text column, which otherwise slips past
            # /preview and fails opaquely at commit (import #176). Tab/newline/CR
            # are allowed so multi-line free-text (e.g. engagement notes) is fine.
            if cleaned is not None and any(
                unicodedata.category(ch) == "Cc" and ch not in "\t\n\r"
                for ch in cleaned
            ):
                raise ValueError("Must not contain control characters.")
            return cleaned
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
    # max_length mirrors database/schema.sql column widths so an over-long value
    # is caught at /preview with a clear message instead of a DBAPIError at commit.
    personal_email: str | None = Field(default=None, max_length=255)
    work_email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    address_line_1: str | None = Field(default=None, max_length=255)
    address_line_2: str | None = Field(default=None, max_length=255)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    zip: str | None = Field(default=None, max_length=20)
    country: str | None = Field(default=None, max_length=100)
    region: str | None = Field(default=None, max_length=100)
    preferred_contact_method: str | None = Field(default=None, max_length=30)
    # The literal best phone/email VALUE from the intake sheet (free text) —
    # distinct from preferred_contact_method, which only names a method.
    best_contact: str | None = Field(default=None, max_length=255)

    @field_validator("preferred_contact_method")
    @classmethod
    def _validate_preferred_contact_method(cls, value: str | None) -> str | None:
        # _Section._trim_strings already normalized empty -> None ahead of this.
        if value is None:
            return None
        if value not in _PREFERRED_CONTACT_METHODS:
            allowed = ", ".join(sorted(_PREFERRED_CONTACT_METHODS))
            raise ValueError(f"Must be one of: {allowed}.")
        return value


class CareerCreate(_Section):
    # max_length mirrors database/schema.sql column widths (see ContactCreate).
    current_employer: str | None = Field(default=None, max_length=255)
    current_title: str | None = Field(default=None, max_length=255)
    current_industry: str | None = Field(default=None, max_length=255)
    current_industry_secondary: str | None = Field(default=None, max_length=255)
    current_city: str | None = Field(default=None, max_length=100)
    current_state: str | None = Field(default=None, max_length=100)
    current_country: str | None = Field(default=None, max_length=100)
    current_zip: str | None = Field(default=None, max_length=20)
    seniority_level: str | None = Field(default=None, max_length=100)

    # Only the PRIMARY industry is a controlled dropdown. The secondary industry
    # is free-text / open response (not restricted to the canonical list), so it
    # is intentionally NOT validated against INDUSTRIES.
    @field_validator("current_industry", mode="before")
    @classmethod
    def _validate_industry(cls, value: object) -> str | None:
        if value is not None and not isinstance(value, str):
            raise ValueError("Must be a string.")
        # validate_industry trims, normalizes empty -> None, and raises on a
        # value outside the canonical INDUSTRIES list (surfaced as a 422).
        return validate_industry(value)


class EducationCreate(_Section):
    # max_length mirrors database/schema.sql column widths (see ContactCreate).
    university: str | None = Field(default=None, max_length=255)
    college: str | None = Field(default=None, max_length=255)
    department: str | None = Field(default=None, max_length=255)
    degree: str | None = Field(default=None, max_length=255)
    major: str | None = Field(default=None, max_length=255)
    degree_status: str | None = Field(default=None, max_length=100)
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
    cfp_designation: str | None = Field(default=None, max_length=100)
    cfa_designation: str | None = Field(default=None, max_length=100)
    cpa_designation: str | None = Field(default=None, max_length=100)
    engagement_notes: str | None = None


class FormerCreate(_Section):
    """A single PRIOR (non-current) role -> one ``employment_history`` row.

    max_length mirrors database/schema.sql column widths (see ContactCreate).
    The service persists this with ``is_current=False``.
    """

    employer_name: str | None = Field(default=None, max_length=255)
    employment_title: str | None = Field(default=None, max_length=255)
    employment_industry: str | None = Field(default=None, max_length=255)


class LeadershipCreate(_Section):
    """A student finance-society leadership role -> one
    ``finance_society_leadership`` row. ``leadership_role`` is required on that
    model; ``role_year`` is optional."""

    leadership_role: str | None = Field(default=None, max_length=100)
    role_year: int | None = None

    @field_validator("role_year")
    @classmethod
    def _validate_role_year(cls, value: int | None) -> int | None:
        if value is None:
            return None
        if not (_YEAR_MIN <= value <= _YEAR_MAX):
            raise ValueError(f"Must be between {_YEAR_MIN} and {_YEAR_MAX}.")
        return value


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
    former: FormerCreate | None = None
    leadership: LeadershipCreate | None = None


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
    # graduation_month is intentionally NOT exposed here anymore -- it is
    # superseded by graduation_semester + graduation_class. The physical column
    # remains on the model/table (dormant), just no longer in the API response.
    graduation_semester: str | None = None
    graduation_class: int | None = None
    finance_program_year: int | None = None
    graduate_degree: str | None = None
    # Survey / demographics (nullable, additive).
    citizenship: str | None = None
    marital_status: str | None = None
    hometown: str | None = None
    home_country: str | None = None
    employment_status: str | None = None
    other_designations: str | None = None
    survey_completed_date: datetime.date | None = None
    # Manual-edit provenance ("Profile updated by ..."). profile_updated_by_name
    # is the updater's resolved "First Last" for the hover; it is NOT a model
    # column -- it is populated by the profile service via a join on
    # profile_updated_by_user_id, so it defaults to None on plain reads.
    profile_updated_date: datetime.date | None = None
    # Free-text "updated by" NAME from the intake sheet (a real column); the hover
    # falls back to this when profile_updated_by_name (resolved from the user FK)
    # is unset.
    profile_updated_by: str | None = None
    profile_updated_by_name: str | None = None
    mba_program: str | None = None
    law_school: str | None = None
    medical_school: str | None = None
    graduate_school: str | None = None
    startup_involvement: str | None = None
    advisory_roles: str | None = None
    secondary_employment: str | None = None
    spouse_first_name: str | None = None
    spouse_last_name: str | None = None
    spouse_birth_date: datetime.date | None = None
    spouse_alumni_id: int | None = None
    deceased: bool
    is_alumni: bool = True
    linkedin_url: str | None = None
    notes: str | None = None
    archived: bool
    manually_edited_at: datetime.datetime | None = None
    last_imported_at: datetime.datetime | None = None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class AlumniListItem(AlumniRead):
    """List-row variant: adds the alumnus's current employer + industry (joined
    from ``current_employment``) and current city + state (from
    ``alumni_contact_info`` — the SAME source the geography map shades by, so the
    list and the map agree on a record's location) for the alumni table.
    Single-record reads use plain ``AlumniRead``, which omits these."""

    current_employer: str | None = None
    current_industry: str | None = None
    # Secondary industry (the actual non-finance industry) for alumni bucketed
    # under "Other" — shown in the list's Other drill-down instead of "Other".
    current_industry_secondary: str | None = None
    current_city: str | None = None
    current_state: str | None = None


class AlumniLocation(BaseModel):
    """Interpretation of a natural-language location search (#358).

    Returned on ``AlumniPage.location`` only when the list request carried a
    ``near`` phrase. ``label`` is a short human string ("Los Angeles, CA within
    50 mi"); ``radius_miles`` is the effective radius; ``resolved`` is ``False``
    when the phrase couldn't be pinpointed (the list then falls back to the
    normal search and the UI shows a soft note)."""

    label: str
    radius_miles: float | None = None
    resolved: bool = True


class AlumniPage(BaseModel):
    """A page of alumni plus the pagination envelope."""

    items: list[AlumniListItem]
    total: int
    limit: int
    offset: int
    # Present only when the request included a ``near`` location search (#358);
    # omitted (``None``) otherwise so a plain list response is unchanged.
    location: AlumniLocation | None = None


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
        # The addressable link to the spouse's own record (its resolved name is
        # nulled separately in _minimize_profile_for_view_only).
        "spouse_alumni_id",
        # Demographic PII from the 2026-07-08 import fields — same sensitivity
        # class as gender/birth_date above, so hidden from view_only. NOTE:
        # employment_status + other_designations are intentionally NOT hidden
        # (career/credential info, like employer/title/graduate_degree, which
        # stay visible to view_only).
        "citizenship",
        "marital_status",
        # Hometown / home country are origin PII (same class as citizenship),
        # hidden from view_only like the other demographic origin fields.
        "hometown",
        "home_country",
        # NOTE: this is the alumni record's import-provenance "Notes" column
        # (CSV intake), hidden from view_only. It is DISTINCT from the unified
        # CRM `notes` table (#39), whose engagement/interaction/event notes are
        # intentionally visible to view_only per the unified-notes spec.
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

"""Data-hygiene / validation pipeline for alumni write payloads.

Three jobs, enforced primarily at the *application* layer. A DB-level partial
unique index on active byu_id / net_id (migrations/2026-06-12_alumni_unique_
byu_net.sql) backs the duplicate check as the authoritative TOCTOU guard, but
the app-layer detection here is what produces the friendly preview/blocker
messages and the archived "ghost" warnings:

  1. Cleaning  — normalize strings (trim/collapse whitespace, casing, phone,
     LinkedIn URL, US-state codes) so what we store is consistent. Cleaning is
     **idempotent**: cleaning already-clean data yields no changes, which means
     the value reported by ``/preview`` is byte-for-byte what the write path
     persists.
  2. Duplicate detection — exact (``byu_id`` / ``net_id``) duplicates *block*
     the write (409); fuzzy (same first+last+grad-year) duplicates only *warn*.
  3. Recommended warnings — soft "you probably want to fill this in" nudges
     (no email, no employer, no grad year). Never block.

The pure functions (``clean_alumni_payload``, ``recommended_warnings``) take no
session and are unit-testable in isolation; ``detect_duplicates`` /
``build_preview`` take a session for the duplicate queries.

Nothing here mutates its input — we always work on a dict copy of the payload.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment

# --- Field maps --------------------------------------------------------------
#
# Which section each nested write-schema belongs to, plus the human label used
# in the ``changes`` list the UI renders. Core fields live on the top-level
# payload; section fields live under payload.contact/career/education/engagement.

_SECTIONS = ("contact", "career", "education", "engagement")

# Human labels for every cleanable field, keyed by (section, field). Only fields
# we actually clean need an entry; everything else passes through untouched.
_LABELS: dict[tuple[str, str], str] = {
    # core
    ("core", "byu_id"): "BYU ID",
    ("core", "net_id"): "Net ID",
    ("core", "first_name"): "First name",
    ("core", "middle_name"): "Middle name",
    ("core", "last_name"): "Last name",
    ("core", "preferred_first_name"): "Preferred first name",
    ("core", "birth_name"): "Birth name",
    ("core", "spouse_first_name"): "Spouse first name",
    ("core", "spouse_last_name"): "Spouse last name",
    ("core", "linkedin_url"): "LinkedIn URL",
    # contact
    ("contact", "personal_email"): "Personal email",
    ("contact", "work_email"): "Work email",
    ("contact", "phone"): "Phone",
    ("contact", "address_line_1"): "Address line 1",
    ("contact", "address_line_2"): "Address line 2",
    ("contact", "city"): "City",
    ("contact", "state"): "State",
    ("contact", "zip"): "ZIP",
    ("contact", "country"): "Country",
    ("contact", "region"): "Region",
    # career
    ("career", "current_employer"): "Current employer",
    ("career", "current_title"): "Current title",
    ("career", "current_city"): "Current city",
    ("career", "current_state"): "Current state",
    ("career", "current_country"): "Current country",
    ("career", "current_zip"): "Current ZIP",
}

# Nobiliary / nominal particles kept lowercase when title-casing a name —
# UNLESS the particle is the first word (then it's capitalized normally). This
# is the standard English-language convention for European surnames, e.g.
# "van der berg" -> "Van der Berg", "de la cruz" -> "De la Cruz".
_NAME_PARTICLES = frozenset(
    {
        "van",
        "von",
        "der",
        "den",
        "de",
        "del",
        "della",
        "di",
        "da",
        "das",
        "dos",
        "du",
        "la",
        "le",
        "lo",
        "ten",
        "ter",
        "of",
    }
)

# Name-style fields per section (trim+collapse + smart Title-Case).
_NAME_FIELDS = {
    "core": (
        "first_name",
        "middle_name",
        "last_name",
        "preferred_first_name",
        "birth_name",
        "spouse_first_name",
        "spouse_last_name",
    ),
}

# US states + DC: full name -> 2-letter code (lower-cased keys for matching).
_US_STATES: dict[str, str] = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "district of columbia": "DC",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
}


# --- Pure cleaning primitives ------------------------------------------------


def _collapse(value: str | None) -> str | None:
    """Trim + collapse internal runs of whitespace to single spaces.

    Empty-after-trim becomes ``None`` so a blank input never persists "".
    """
    if value is None:
        return None
    collapsed = re.sub(r"\s+", " ", value).strip()
    return collapsed or None


def _clean_email(value: str | None) -> str | None:
    """Lowercase + trim an email (collapse handles stray internal whitespace)."""
    cleaned = _collapse(value)
    return cleaned.lower() if cleaned is not None else None


def _smart_title(value: str | None) -> str | None:
    """Title-Case a name **only** when it is entirely upper or entirely lower.

    Already-mixed input (``McDonald``, ``O'Brien``, ``DeShawn``) is assumed to be
    intentionally cased and is preserved. Casing is applied per whitespace token
    so multi-word names ("anne marie") each get capitalized; hyphen/apostrophe
    sub-parts are handled by ``str.title``'s word-boundary logic.

    Nobiliary particles (``van, von, der, de, la, ...`` — see ``_NAME_PARTICLES``)
    are kept lowercase when they are NOT the first word, following the standard
    English convention for European surnames::

        "van der berg" -> "Van der Berg"   (first word always capitalized)
        "de la cruz"   -> "De la Cruz"
        "VAN DER BERG" -> "Van der Berg"

    Limits: this only fires on the all-upper / all-lower normalization path, so
    an intentionally-mixed input is left untouched. The particle list is a fixed
    set of common particles, not exhaustive, and it cannot distinguish a genuine
    particle from a same-spelled given name (a first name "Della" coming in as
    all-lower would be lowercased if it appeared after the first word). These are
    acceptable trade-offs for cleaning bulk-imported names.
    """
    cleaned = _collapse(value)
    if cleaned is None:
        return None
    # Compare against the cased forms ignoring non-alphabetic chars so that
    # "o'brien" still counts as all-lower and "MCDONALD" as all-upper.
    letters = [ch for ch in cleaned if ch.isalpha()]
    if not letters:
        return cleaned
    if all(ch.islower() for ch in letters) or all(ch.isupper() for ch in letters):
        titled = cleaned.title()
        # Re-lowercase recognized particles, but never the first word.
        tokens = titled.split(" ")
        for i in range(1, len(tokens)):
            if tokens[i].lower() in _NAME_PARTICLES:
                tokens[i] = tokens[i].lower()
        return " ".join(tokens)
    return cleaned  # already mixed -> leave as-is


def _clean_phone(value: str | None) -> str | None:
    """Format US phone numbers; leave anything non-standard as trimmed text.

    * 10 digits          -> ``(XXX) XXX-XXXX``
    * 11 digits, leads 1 -> ``+1 (XXX) XXX-XXXX``
    * otherwise          -> the trimmed original (we don't guess).
    """
    cleaned = _collapse(value)
    if cleaned is None:
        return None
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    if len(digits) == 11 and digits[0] == "1":
        d = digits[1:]
        return f"+1 ({d[0:3]}) {d[3:6]}-{d[6:10]}"
    return cleaned


def _clean_linkedin(value: str | None) -> str | None:
    """Normalize a LinkedIn URL: force https, lowercase host, drop query/
    fragment and any trailing slash from the path."""
    cleaned = _collapse(value)
    if cleaned is None:
        return None
    parts = urlsplit(cleaned)
    host = (parts.hostname or "").lower()
    if not host:
        # No host to normalize (schema validation will have rejected this on the
        # write path); leave the collapsed value untouched.
        return cleaned
    path = parts.path.rstrip("/")
    port = f":{parts.port}" if parts.port else ""
    return f"https://{host}{port}{path}"


def _clean_state(value: str | None) -> str | None:
    """Map a full US state name to its 2-letter UPPER code; otherwise uppercase
    a 2-letter input. Non-state values pass through trimmed."""
    cleaned = _collapse(value)
    if cleaned is None:
        return None
    mapped = _US_STATES.get(cleaned.lower())
    if mapped is not None:
        return mapped
    if len(cleaned) == 2 and cleaned.isalpha():
        return cleaned.upper()
    return cleaned


def _clean_byu_id(value: str | None) -> str | None:
    """Strip every non-digit from a BYU ID."""
    cleaned = _collapse(value)
    if cleaned is None:
        return None
    digits = re.sub(r"\D", "", cleaned)
    return digits or None


def _clean_net_id(value: str | None) -> str | None:
    """Lowercase + trim a Net ID."""
    cleaned = _collapse(value)
    return cleaned.lower() if cleaned is not None else None


# Per-field cleaner registry. Each entry is (section, field) -> callable. Fields
# not listed here that are still strings get a default trim+collapse pass.
_CLEANERS: dict[tuple[str, str], object] = {
    ("core", "byu_id"): _clean_byu_id,
    ("core", "net_id"): _clean_net_id,
    ("core", "linkedin_url"): _clean_linkedin,
    ("contact", "personal_email"): _clean_email,
    ("contact", "work_email"): _clean_email,
    ("contact", "phone"): _clean_phone,
    ("contact", "city"): _smart_title,
    ("contact", "state"): _clean_state,
    ("career", "current_city"): _smart_title,
    ("career", "current_state"): _clean_state,
}

# Plain trim+collapse fields (no special rule, but explicitly cleaned so casing
# of surrounding logic is centralized). Anything else string-typed is also
# collapsed by the generic pass below.
_COLLAPSE_FIELDS = {
    "contact": (
        "address_line_1",
        "address_line_2",
        "zip",
        "country",
        "region",
    ),
    "career": ("current_country", "current_zip"),
}


def _clean_field(section: str, field: str, value: object) -> object:
    """Clean a single field's value per its rule. Non-strings pass through."""
    cleaner = _CLEANERS.get((section, field))
    if cleaner is not None:
        return cleaner(value)
    if section in _NAME_FIELDS and field in _NAME_FIELDS[section]:
        return _smart_title(value)
    # Generic: collapse any string; leave non-strings (ints, bools, dates).
    if isinstance(value, str):
        return _collapse(value)
    return value


def _label(section: str, field: str) -> str:
    """Human label for a field, defaulting to a prettified field name."""
    return _LABELS.get((section, field), field.replace("_", " ").capitalize())


def clean_section(section: str, values: dict) -> dict:
    """Clean every field of a *full* section dict (used by the update write path,
    which overwrites the whole related row). Returns a new dict; input untouched.
    """
    return {field: _clean_field(section, field, value) for field, value in values.items()}


# --- Payload cleaning --------------------------------------------------------


def clean_alumni_payload(payload, *, jsonable: bool = True) -> tuple[dict, list[dict]]:
    """Clean an ``AlumniCreateFull`` / ``AlumniUpdateFull`` payload.

    Returns ``(cleaned, changes)`` where:
      * ``cleaned`` is a plain dict shaped like the input — core fields at the
        top level, sections nested. For updates, only fields that were *present*
        (``exclude_unset``) appear, so we never accidentally clear an omitted
        field. With ``jsonable=True`` (the default, used by ``/preview``) dates
        serialize to ISO strings; with ``jsonable=False`` (the write path) dates
        stay as ``date`` objects so they can be written straight to the ORM.
      * ``changes`` is a list of ``{"section","field","label","before","after"}``
        dicts, one per field whose value actually changed.

    Cleaning only ever touches string fields, so ``changes`` is identical
    regardless of ``jsonable`` (dates/ints/bools are never "changed").

    The input is never mutated; we dump it to a dict first and work on that.
    """
    # exclude_unset so update payloads only carry the fields the client sent.
    mode = "json" if jsonable else "python"
    data = payload.model_dump(mode=mode, exclude_unset=True)
    changes: list[dict] = []

    # Core fields (everything that isn't a nested section).
    cleaned: dict = {}
    for field, value in data.items():
        if field in _SECTIONS:
            continue
        after = _clean_field("core", field, value)
        cleaned[field] = after
        if after != value:
            changes.append(
                {
                    "section": "core",
                    "field": field,
                    "label": _label("core", field),
                    "before": value,
                    "after": after,
                }
            )

    # Nested sections.
    for section in _SECTIONS:
        section_data = data.get(section)
        if section_data is None:
            continue
        cleaned_section: dict = {}
        for field, value in section_data.items():
            after = _clean_field(section, field, value)
            cleaned_section[field] = after
            if after != value:
                changes.append(
                    {
                        "section": section,
                        "field": field,
                        "label": _label(section, field),
                        "before": value,
                        "after": after,
                    }
                )
        cleaned[section] = cleaned_section

    return cleaned, changes


# --- Duplicate detection -----------------------------------------------------


def _display_name(first: str | None, last: str | None) -> str:
    """Best-effort display name for a duplicate message."""
    name = " ".join(part for part in (first, last) if part).strip()
    return name or "another record"


async def detect_duplicates(
    session: AsyncSession,
    cleaned: dict,
    exclude_alumni_id: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """Find exact (blocking) and fuzzy (warning) duplicates for *cleaned*.

    ``cleaned`` is the dict from :func:`clean_alumni_payload` (or the effective
    overlaid record for an update). All queries skip archived rows and the
    record itself (``exclude_alumni_id``).

    Returns ``(blockers, warnings)``:
      * blockers: exact ``byu_id`` and/or ``net_id`` collisions (each a
        ``{"code","field","message","alumni_id"}`` dict).
      * warnings: fuzzy ``possible_duplicate`` matches (same first+last+grad
        year, excluding anyone already flagged as a blocker), plus
        ``duplicate_archived`` warnings when an ARCHIVED record carries the same
        byu_id / net_id (active matches block; archived ones only warn, so the
        admin knows restoring the ghost would create a duplicate).
    """
    blockers: list[dict] = []
    blocker_ids: set[int] = set()
    warnings: list[dict] = []

    byu_id = cleaned.get("byu_id")
    net_id = cleaned.get("net_id")

    # --- Exact: BYU ID ---
    if byu_id:
        stmt = select(Alumni).where(
            Alumni.byu_id == byu_id, Alumni.archived.is_(False)
        )
        if exclude_alumni_id is not None:
            stmt = stmt.where(Alumni.alumni_id != exclude_alumni_id)
        match = await session.scalar(stmt.limit(1))
        if match is not None:
            blockers.append(
                {
                    "code": "duplicate_byu_id",
                    "field": "byu_id",
                    "message": (
                        f"BYU ID {byu_id} already belongs to "
                        f"{_display_name(match.first_name, match.last_name)}."
                    ),
                    "alumni_id": match.alumni_id,
                }
            )
            blocker_ids.add(match.alumni_id)
        else:
            # No ACTIVE collision — but an ARCHIVED ("ghost") record may carry
            # the same id. Surface it as a warning so the admin knows restoring
            # that record (instead of creating a new one) would otherwise make a
            # duplicate. Never blocks.
            stmt_arch = select(Alumni).where(
                Alumni.byu_id == byu_id, Alumni.archived.is_(True)
            )
            if exclude_alumni_id is not None:
                stmt_arch = stmt_arch.where(Alumni.alumni_id != exclude_alumni_id)
            ghost = await session.scalar(stmt_arch.limit(1))
            if ghost is not None:
                warnings.append(
                    {
                        "code": "duplicate_archived",
                        "message": (
                            f"BYU ID {byu_id} matches an archived record for "
                            f"{_display_name(ghost.first_name, ghost.last_name)}"
                            " — restoring it would create a duplicate."
                        ),
                        "alumni_id": ghost.alumni_id,
                    }
                )

    # --- Exact: Net ID ---
    if net_id:
        stmt = select(Alumni).where(
            Alumni.net_id == net_id, Alumni.archived.is_(False)
        )
        if exclude_alumni_id is not None:
            stmt = stmt.where(Alumni.alumni_id != exclude_alumni_id)
        match = await session.scalar(stmt.limit(1))
        if match is not None:
            blockers.append(
                {
                    "code": "duplicate_net_id",
                    "field": "net_id",
                    "message": (
                        f"Net ID {net_id} already belongs to "
                        f"{_display_name(match.first_name, match.last_name)}."
                    ),
                    "alumni_id": match.alumni_id,
                }
            )
            blocker_ids.add(match.alumni_id)
        else:
            stmt_arch = select(Alumni).where(
                Alumni.net_id == net_id, Alumni.archived.is_(True)
            )
            if exclude_alumni_id is not None:
                stmt_arch = stmt_arch.where(Alumni.alumni_id != exclude_alumni_id)
            ghost = await session.scalar(stmt_arch.limit(1))
            if ghost is not None:
                warnings.append(
                    {
                        "code": "duplicate_archived",
                        "message": (
                            f"Net ID {net_id} matches an archived record for "
                            f"{_display_name(ghost.first_name, ghost.last_name)}"
                            " — restoring it would create a duplicate."
                        ),
                        "alumni_id": ghost.alumni_id,
                    }
                )

    # --- Fuzzy: same first+last (case-insensitive) AND same graduation_year ---
    first = cleaned.get("first_name")
    last = cleaned.get("last_name")
    grad_year = cleaned.get("graduation_year")
    if first and last and grad_year is not None:
        stmt = select(Alumni).where(
            func.lower(Alumni.first_name) == first.lower(),
            func.lower(Alumni.last_name) == last.lower(),
            Alumni.graduation_year == grad_year,
            Alumni.archived.is_(False),
        )
        if exclude_alumni_id is not None:
            stmt = stmt.where(Alumni.alumni_id != exclude_alumni_id)
        result = await session.execute(stmt)
        for match in result.scalars().all():
            if match.alumni_id in blocker_ids:
                continue  # already a hard blocker; don't double-report
            warnings.append(
                {
                    "code": "possible_duplicate",
                    "message": (
                        "Possible duplicate of "
                        f"{_display_name(match.first_name, match.last_name)} "
                        f"(Class of {grad_year})."
                    ),
                    "alumni_id": match.alumni_id,
                }
            )

    return blockers, warnings


# --- Recommended (soft) warnings ---------------------------------------------


def recommended_warnings(effective: dict) -> list[dict]:
    """Soft data-completeness nudges over an *effective* record dict.

    ``effective`` is the cleaned create payload, or (for updates) the cleaned
    partial overlaid on the current record — so these reflect the resulting
    state, not just the edited fields. None of these block a write.
    """
    warnings: list[dict] = []
    contact = effective.get("contact") or {}
    career = effective.get("career") or {}

    if not (contact.get("personal_email") or contact.get("work_email")):
        warnings.append(
            {
                "code": "missing_email",
                "message": "No email on file — this alum can't be contacted.",
            }
        )
    if not career.get("current_employer"):
        warnings.append(
            {
                "code": "missing_employer",
                "message": "No current employer on file.",
            }
        )
    if effective.get("graduation_year") is None:
        warnings.append(
            {
                "code": "missing_grad_year",
                "message": "No graduation year on file.",
            }
        )
    return warnings


# --- Effective-record assembly (for update previews) -------------------------

# Which ORM section model + payload key + the columns we read back for the
# effective overlay. Engagement is intentionally omitted from the effective
# read: recommended/dup checks never look at engagement flags.
_EFFECTIVE_CORE_FIELDS = (
    "byu_id",
    "net_id",
    "first_name",
    "last_name",
    "graduation_year",
)


async def _load_effective(
    session: AsyncSession,
    existing: Alumni,
    cleaned: dict,
) -> dict:
    """Overlay the cleaned partial payload on top of the current record.

    Loads the alumnus's current contact + career sections (only the fields the
    duplicate/recommended checks consult) and applies any cleaned overrides so
    the checks see the *resulting* values, not just the changed ones.
    """
    effective: dict = {}
    # Core: start from the stored record, override with cleaned core fields.
    for field in _EFFECTIVE_CORE_FIELDS:
        effective[field] = getattr(existing, field, None)
    for field in _EFFECTIVE_CORE_FIELDS:
        if field in cleaned:
            effective[field] = cleaned[field]

    # Contact (emails) from the stored row, then overlay cleaned contact.
    contact_row = await session.scalar(
        select(AlumniContactInfo)
        .where(AlumniContactInfo.alumni_id == existing.alumni_id)
        .order_by(AlumniContactInfo.contact_info_id)
        .limit(1)
    )
    contact: dict = {
        "personal_email": getattr(contact_row, "personal_email", None),
        "work_email": getattr(contact_row, "work_email", None),
    }
    contact.update(cleaned.get("contact") or {})
    effective["contact"] = contact

    # Career (current_employer) from the stored row, then overlay cleaned career.
    career_row = await session.scalar(
        select(CurrentEmployment)
        .where(CurrentEmployment.alumni_id == existing.alumni_id)
        .order_by(CurrentEmployment.current_employment_id.desc())
        .limit(1)
    )
    career: dict = {
        "current_employer": getattr(career_row, "current_employer", None),
    }
    career.update(cleaned.get("career") or {})
    effective["career"] = career

    return effective


# --- Preview builder ---------------------------------------------------------


async def build_preview(
    session: AsyncSession,
    payload,
    existing: Alumni | None = None,
    exclude_alumni_id: int | None = None,
) -> dict:
    """Build the dry-run preview for a create or update payload.

    Returns a dict with four keys (the exact shape the frontend consumes):
      * ``cleaned``  — jsonable dict shaped like the input payload.
      * ``changes``  — list of per-field {section,field,label,before,after}.
      * ``warnings`` — soft warnings (recommended + fuzzy duplicates). Never
        includes blockers.
      * ``blockers`` — exact duplicate blockers (each {code,field,message,
        alumni_id}); a non-empty list means the real write would 409.

    For an UPDATE (``existing`` given), duplicate + recommended checks run
    against the *effective* record (cleaned partial overlaid on the stored row),
    so they reflect the resulting state rather than only the edited fields.
    """
    cleaned, changes = clean_alumni_payload(payload)
    blockers, dup_warnings = await detect_duplicates(
        session, cleaned, exclude_alumni_id=exclude_alumni_id
    )

    if existing is not None:
        effective = await _load_effective(session, existing, cleaned)
    else:
        effective = cleaned

    warnings = recommended_warnings(effective) + dup_warnings

    return {
        "cleaned": cleaned,
        "changes": changes,
        "warnings": warnings,
        "blockers": blockers,
    }

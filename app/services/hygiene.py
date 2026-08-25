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

from app.core.dropdowns import employer_applies
from app.core.us_states import is_us_country as _is_us_country
from app.core.us_states import to_full_name as _state_full_name
from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.schemas.alumni import AlumniUpdate
from app.services.state_regions import region_for_state

# --- Field maps --------------------------------------------------------------
#
# Which section each nested write-schema belongs to, plus the human label used
# in the ``changes`` list the UI renders. Core fields live on the top-level
# payload; section fields live under payload.contact/career/education/engagement.

_SECTIONS = ("contact", "career", "education", "engagement")

# Top-level payload keys that are WRITE CONTROLS, not data: they change what the
# save DOES rather than naming a column to write (#446's `archive_previous_role`
# demotes the outgoing current role into employment_history). They are skipped
# entirely here so `cleaned` keeps meaning "the values that will be stored" --
# `/preview` renders that dict as the record-to-be, and a checkbox showing up in
# it as a field would be a lie about what is being saved.
#
# Mirrors `alumni_service.CONTROL_KEYS`, the same way `_SECTIONS` mirrors
# `alumni_service.SECTION_KEYS`: this module is imported BY the alumni service,
# so it cannot import back.
_CONTROL_KEYS = frozenset({"archive_previous_role"})

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
    """Normalize a US state to its canonical FULL name (e.g. "UT"/"utah" ->
    "Utah"). Non-US values pass through trimmed (whitespace collapsed)."""
    cleaned = _collapse(value)
    if cleaned is None:
        return None
    return _state_full_name(cleaned)


# Country values that mean "the United States" now live in app.core.us_states
# (``US_COUNTRY_ALIASES`` / ``is_us_country``, imported above as
# ``_is_us_country``) alongside the state crosswalk, because the dashboard's
# country KPI and the world map need the same answer to "is this person abroad?".
# This module used to keep its own copy of the list; the shared one adds the
# spelling "America", so a record whose country reads "America" is now recognized
# as domestic and can derive a US region, where before it was treated as abroad
# and left the region untouched.


# "The caller has no stored work state to compare against." Distinct from None,
# which means "the record HAS no work state" (a real value to compare with).
UNKNOWN_STATE = object()


def derive_region(cleaned: dict, stored_state: object = UNKNOWN_STATE) -> str | None:
    """Region derived from the EMPLOYMENT state, or ``None`` to leave it alone.

    Issue #283: when an alum's work state changes, their ``region`` should follow
    automatically instead of being hand-entered twice. ``region`` physically
    lives on the contact/residence row, but per Tanya it now means *where they
    work* — so ``career.current_state`` is what drives it, not ``contact.state``.

    *cleaned* is the dict from :func:`clean_alumni_payload`, i.e. already run
    through :func:`_clean_state`, so ``career.current_state`` is a canonical full
    state name. Because that dict is built with ``exclude_unset``, a key being
    PRESENT means the caller explicitly sent it — which is how the rules below
    tell "touched" from "omitted".

    *stored_state* is the work state currently ON the record: a value, or ``None``
    when the record has none (or doesn't exist yet, i.e. create). Pass
    ``UNKNOWN_STATE`` only when there is genuinely nothing to compare against —
    it makes "supplied" the trigger, which is right for a create and wrong for an
    update (see :func:`clean_alumni_payload`).

    Returns ``None`` (derive nothing, leave any stored region as-is) unless every
    condition holds:

    * ``career.current_state`` was supplied — an edit that didn't touch the work
      state must never move the region.
    * the supplied state CHANGED versus *stored_state* — re-submitting the state
      a record already has is not a move, and must not touch the region. This is
      what makes Tanya's override durable: she can deliberately set a Texas-based
      remote worker to "West", and opening their Employment card and saving must
      leave that alone rather than silently reverting her (#283 — she chose
      auto-filled-but-overridable, and untouched records keep their region).
      Both sides are normalized through :func:`_clean_state`, so "TX" against a
      stored "Texas" is correctly seen as no change.
    * ``contact.region`` was NOT supplied — an explicit region always wins, which
      is the escape hatch for the cases the map gets wrong. An explicit ``null``
      counts as supplied, so an intentional clear still applies.
    * ``career.current_country``, IF supplied, is the US — the five regions are
      US-only, so a move abroad leaves the region untouched rather than blanking
      it. An omitted country is not treated as non-US: the state map itself is
      the backstop, since only the 50 states + DC resolve.
    * the state resolves to a region — a blank state, or a non-US/unrecognized
      one ("Ontario"), derives nothing rather than clearing the stored value.

    Note that ``clean_alumni_payload`` calls this itself and writes the result
    into ``cleaned["contact"]["region"]``. Re-calling it on that same dict
    therefore returns ``None`` (the region is now "explicitly supplied") — which
    is what keeps re-cleaning idempotent, but means write-path callers should
    read ``cleaned["contact"]["region"]`` rather than call this a second time.
    """
    career = cleaned.get("career")
    if not isinstance(career, dict) or "current_state" not in career:
        return None  # work state untouched -> region must not move
    contact = cleaned.get("contact")
    if isinstance(contact, dict) and "region" in contact:
        return None  # caller supplied a region explicitly -> theirs wins
    incoming_state = career.get("current_state")
    if stored_state is not UNKNOWN_STATE and _clean_state(
        stored_state if isinstance(stored_state, str) else None
    ) == _clean_state(incoming_state):
        return None  # work state didn't actually change -> region must not move
    country = career.get("current_country")
    if country is not None and not _is_us_country(country):
        return None  # works abroad -> the US-only regions don't apply
    # None for a blank / non-US / unrecognized state -> leave the stored region.
    return region_for_state(incoming_state)


def work_state_supplied(payload) -> bool:
    """True when *payload* explicitly carries ``career.current_state``.

    The one condition under which the region can derive at all, so both the
    preview and the write use it to skip the :func:`stored_work_state` lookup
    entirely when no work state was sent — the answer couldn't change anything.
    An explicit ``null`` counts as supplied (it's a real edit); a merely-absent
    field does not.
    """
    career = getattr(payload, "career", None)
    return career is not None and "current_state" in career.__pydantic_fields_set__


async def stored_work_state(session: AsyncSession, alumni_id: int) -> str | None:
    """The work state currently stored for *alumni_id* (or ``None``).

    The single source of "what state is this record on today" for the region
    derivation, shared by ``/preview`` and the write path so the two can never
    disagree about whether the state changed. Order-by mirrors
    ``alumni_service._upsert_section`` so this reads the SAME row the write
    upserts. Returns the whole row's field rather than selecting the column so a
    caller's session sees one predictable query shape.
    """
    row = await session.scalar(
        select(CurrentEmployment)
        .where(CurrentEmployment.alumni_id == alumni_id)
        .order_by(CurrentEmployment.current_employment_id.desc())
        .limit(1)
    )
    return getattr(row, "current_state", None)


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


def clean_alumni_payload(
    payload, *, jsonable: bool = True, stored_state: object = UNKNOWN_STATE
) -> tuple[dict, list[dict]]:
    """Clean an ``AlumniCreateFull`` / ``AlumniUpdateFull`` payload.

    *stored_state* is the work state currently on the record, threaded through to
    :func:`derive_region` so the region only auto-fills when the state actually
    CHANGED (#283). Every UPDATE caller should pass it — ``update_alumni`` and
    ``build_preview`` both read it via :func:`stored_work_state`, which is what
    keeps the preview and the write in lockstep. An update payload whose caller
    left it ``UNKNOWN_STATE`` derives NOTHING rather than guessing: guessing
    "supplied means changed" would make ``/preview`` promise a region change that
    the write then declines to make, which is exactly the half-wired failure this
    card already had once. (A create has nothing stored by definition, so
    "supplied" is the correct trigger there and the default is right.)

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
        if field in _SECTIONS or field in _CONTROL_KEYS:
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

    # Region auto-fill (#283). Runs last, over the CLEANED sections, so the map
    # keys off the normalized full state name. Reported as a change so /preview
    # shows the caller what the save will do; ``before`` is None because the
    # caller sent no region (this function has no session and so can't know the
    # stored one — the "after" is what's authoritative).
    if isinstance(payload, AlumniUpdate) and stored_state is UNKNOWN_STATE:
        # An update that can't tell whether the state changed must not guess —
        # see the note on this function. Deriving here would show the caller a
        # region change the write path (which DOES know) would skip.
        derived_region = None
    else:
        derived_region = derive_region(cleaned, stored_state)
    if derived_region is not None:
        cleaned.setdefault("contact", {})["region"] = derived_region
        changes.append(
            {
                "section": "contact",
                "field": "region",
                "label": _label("contact", "region"),
                "before": None,
                "after": derived_region,
            }
        )

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
    # Missing employer (#608). Suppressed for the statuses where an employer is
    # inapplicable or optional — Military (Jake: "the branch does not matter"),
    # Unemployed, Not in the Labor Force, Graduate Student. Nagging about data
    # that isn't wanted is how a warning list gets ignored wholesale. Single
    # source of truth: ``EMPLOYER_NOT_APPLICABLE_STATUSES``.
    if not career.get("current_employer") and employer_applies(
        effective.get("employment_status")
    ):
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
    # Address placeholder / malformed-ZIP nudges (non-blocking): departments often
    # type "N/A"/"unknown" instead of leaving a cell blank, which then looks like
    # real data to any downstream mailing export.
    _placeholders = {"n/a", "n.a.", "na", "none", "null", "unknown", "tbd"}
    flagged = [
        f
        for f in ("address_line_1", "address_line_2", "city", "state", "zip", "country")
        if isinstance(contact.get(f), str)
        and contact[f].strip()
        and (
            contact[f].strip().lower() in _placeholders
            or set(contact[f].strip().lower()) == {"x"}
        )
    ]
    if flagged:
        warnings.append(
            {
                "code": "address_placeholder",
                "message": (
                    "Address field(s) look like a placeholder rather than real "
                    "data: " + ", ".join(flagged) + ". Leave blank if unknown."
                ),
            }
        )
    zip_val = contact.get("zip")
    if isinstance(zip_val, str) and zip_val.strip():
        z = zip_val.strip()
        # Warn (never block) on non-US-ZIP shape — international postal codes vary.
        if z.lower() not in _placeholders and not re.fullmatch(
            r"\d{5}(-\d{4})?", z
        ):
            warnings.append(
                {
                    "code": "zip_format",
                    "message": (
                        f"ZIP '{z}' isn't a US 5-digit or ZIP+4 format — double-check "
                        "it (non-US postal codes are fine to ignore)."
                    ),
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
    # #608 — ``recommended_warnings`` suppresses the missing-employer nudge for
    # statuses that make an employer inapplicable, so an UPDATE preview has to see
    # the STORED status when the payload doesn't resend it. Without it here, an
    # unrelated edit to an Unemployed alumnus would show the warning again.
    "employment_status",
)


def effective_identity(existing: Alumni, cleaned: dict) -> dict:
    """Overlay the cleaned partial payload's CORE fields on the stored record.

    Pure and query-free: everything it reads is already on the loaded ``Alumni``
    row. This is the input :func:`detect_duplicates` needs — it consults only
    ``byu_id`` / ``net_id`` / ``first_name`` / ``last_name`` /
    ``graduation_year``, all of which live on the core record.

    Duplicate detection on an UPDATE **must** run against this rather than the
    partial payload (#627). ``clean_alumni_payload`` uses ``exclude_unset``, so a
    focused edit form that submits only the name fields produces a ``cleaned``
    with no ``graduation_year`` — and the fuzzy check needs first + last + grad
    year all present, so it silently does nothing and a rename into an exact
    collision saves with no warning at all. Overlaying the stored row restores
    the legs the patch didn't resend.
    """
    effective: dict = {}
    for field in _EFFECTIVE_CORE_FIELDS:
        effective[field] = getattr(existing, field, None)
    for field in _EFFECTIVE_CORE_FIELDS:
        if field in cleaned:
            effective[field] = cleaned[field]
    return effective


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
    effective = effective_identity(existing, cleaned)

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
    so they reflect the resulting state rather than only the edited fields, and
    the region derivation is fed the record's stored work state so the preview
    promises exactly what ``update_alumni`` will write — same rule, same query.
    """
    if existing is not None and work_state_supplied(payload):
        cleaned, changes = clean_alumni_payload(
            payload, stored_state=await stored_work_state(session, existing.alumni_id)
        )
    else:
        # Create (nothing stored yet, so "supplied" is the right trigger), or an
        # update that sent no work state — nothing to derive from either way.
        cleaned, changes = clean_alumni_payload(payload)
    if existing is not None:
        effective = await _load_effective(session, existing, cleaned)
    else:
        effective = cleaned

    # Duplicate detection runs against the EFFECTIVE record, not the partial
    # payload (#627). This used to be passed ``cleaned``, which made the
    # docstring's promise false for exactly the case that matters: a focused edit
    # form sends the name fields and nothing else, ``cleaned`` therefore has no
    # graduation year, and the fuzzy first+last+grad-year check needs all three —
    # so a rename into a real collision previewed clean.
    blockers, dup_warnings = await detect_duplicates(
        session, effective, exclude_alumni_id=exclude_alumni_id
    )

    warnings = recommended_warnings(effective) + dup_warnings

    return {
        "cleaned": cleaned,
        "changes": changes,
        "warnings": warnings,
        "blockers": blockers,
    }

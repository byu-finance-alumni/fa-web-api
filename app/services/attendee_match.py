"""Conference-attendee matching for a single event (#612).

Conference registrations do not collect Net IDs, and attendees do not know
theirs — so every existing bulk path into this database (photo import, alumni
import, the events attendee import) is unusable for a conference list. This
module matches an attendee list to existing alumni on **email, then name**, and
PROPOSES the matches for a human to approve one at a time.

Nothing here ever writes. :func:`propose` is a pure dry-run report; the writes
live in the route layer and only ever act on ids a human explicitly approved.

Three stages, mirroring ``import_csv`` / ``import_events``:

  1. :func:`parse_and_map` — decode, alias the file's headers onto the intake
     template's headers, and map each row through ``import_csv._map_row`` with
     the alumni importer's OWN ``_MAPPING`` and ``friend=True`` (which stamps
     ``is_alumni = False``). The full alumni mapping is used rather than the
     narrower friend subset on purpose: Jake asked for "everything we have on
     them that matches a field in the db columns already", and the friend subset
     drops columns a conference list really does carry (graduation year, Net ID).
     **Unmappable columns are ignored, never an error** (Jake, 2026-08-04) — a
     conference export carries whatever the registration form happened to ask
     for.
  2. :func:`propose` — resolve every row against the DB in a bounded number of
     BATCHED queries (never one query per row, never "load 8,000 alumni into
     Python"), score the small candidate pool, and report per-row
     ``matched`` / ``ambiguous`` / ``no_match``. NO writes.
  3. the route layer — ``/approve`` writes attendance for approved
     ``alumni_id``s (idempotent per (event, alumni)); ``/friends`` re-parses the
     SAME file and creates the selected no-match rows as friend records
     (``is_alumni = false``) through the shared ``alumni_service.create_alumni``
     path, then attaches them to the event.

Safety rules this module exists to enforce (from the issue, non-negotiable):

  * **Nothing is auto-applied.** ``propose`` returns proposals only. There is
    deliberately no confidence threshold above which a match is applied — the
    caller cannot ask for one.
  * **Ambiguity surfaces as ambiguity.** Every candidate that survives scoring
    is returned, ranked, and a row with more than one candidate is reported as
    ``ambiguous``. The top-scoring candidate is never silently chosen.
  * **Preferred and maiden names are first-class.** Given names are compared
    against ``first_name`` / ``preferred_first_name`` / ``middle_name`` through
    a nickname table; surnames against ``last_name`` AND ``birth_name`` (the
    maiden-name column, #216).
  * **Company is corroborating evidence, never a key.** "Goldman" / "Goldman
    Sachs" / "Goldman Sachs & Co." corroborate each other; a company mismatch
    never rejects a candidate on its own.
"""

from __future__ import annotations

import csv
import io
import logging
import re
import unicodedata

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alumni import Alumni
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.event import Event, EventAttendance
from app.services.import_csv import (
    _MAPPING,
    _canonicalize_header,
    _decode_upload,
    _map_row,
)

log = logging.getLogger(__name__)

# --- Upload guards -----------------------------------------------------------
#
# 4 MiB, deliberately BELOW Vercel's ~4.5 MB serverless request-body ceiling so
# the app's own friendly 413 fires instead of the raw platform error (which the
# browser misreports as a CORS failure — see the bulk photo import, app #595).
MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_ATTENDEE_ROWS = 2000

# Bounds on the DB fan-out. The candidate pool is capped so a hostile / sloppy
# file (2,000 rows of "Smith") cannot pull the whole roster into memory; hitting
# the cap is REPORTED as a warning, never silently truncated away.
_MAX_CANDIDATE_ROWS = 3000
# How many candidates a single attendee row may carry back to the reviewer.
# Beyond this the row is still ``ambiguous`` — it just stops being a useful list.
MAX_CANDIDATES_PER_ROW = 10
# Distinct employer patterns fed to the given-name corroboration leg (tier C).
_MAX_COMPANY_PATTERNS = 60
# How many rows may be approved / created as friends in one request.
MAX_APPROVALS_PER_REQUEST = 500
MAX_FRIENDS_PER_REQUEST = 500


# --- Header aliasing ---------------------------------------------------------
#
# A conference registration export does NOT use the intake template's headers.
# Rather than build a second column-mapping table (Jake, 2026-08-04: "follow the
# column-mapping approach the alumni CSV import already uses"), each realistic
# conference header is ALIASED onto the intake header that already carries the
# right (section, field, kind) target, and the file is then mapped through the
# existing ``import_csv._map_row`` with ``_MAPPING``.
#
# Keys are lower-cased and punctuation-stripped (see :func:`_header_key`), so
# "E-mail Address", "email address" and "Email  Address" all land on the same
# alias. Anything with no alias and no exact intake header is IGNORED.
_HEADER_ALIASES: dict[str, str] = {
    # Names
    "first name": "First name",
    "firstname": "First name",
    "first": "First name",
    "given name": "First name",
    "preferred name": "Preferred first name",
    "preferred first name": "Preferred first name",
    "nickname": "Preferred first name",
    "goes by": "Preferred first name",
    "middle name": "Middle name",
    "last name": "Last Name",
    "lastname": "Last Name",
    "last": "Last Name",
    "surname": "Last Name",
    "family name": "Last Name",
    "maiden name": "Maiden name",
    "birth name": "Maiden name",
    # Contact
    "email": "Personal Email",
    "e mail": "Personal Email",
    "email address": "Personal Email",
    "e mail address": "Personal Email",
    "personal email": "Personal Email",
    "attendee email": "Personal Email",
    "work email": "Work Email",
    "business email": "Work Email",
    "company email": "Work Email",
    "phone": "Phone #",
    "phone number": "Phone #",
    "mobile": "Phone #",
    "mobile phone": "Phone #",
    "cell": "Phone #",
    "cell phone": "Phone #",
    "telephone": "Phone #",
    "linkedin": "LinkedIn URL",
    "linkedin url": "LinkedIn URL",
    "linkedin profile": "LinkedIn URL",
    # Employment
    "company": "Current employer",
    "employer": "Current employer",
    "organization": "Current employer",
    "organisation": "Current employer",
    "firm": "Current employer",
    "company name": "Current employer",
    "current company": "Current employer",
    "title": "Current title",
    "job title": "Current title",
    "position": "Current title",
    "role": "Current title",
    "current position": "Current title",
    # Location (the intake sheet's "Current ..." block is the EMPLOYER's
    # address, #287 — which is exactly what a conference list means by these.)
    "city": "Current city",
    "work city": "Current city",
    "state": "Current state",
    "work state": "Current state",
    "country": "Current country",
    "zip": "Current ZIP",
    "zip code": "Current ZIP",
    "postal code": "Current ZIP",
    # Academic (a conference list occasionally carries these)
    "graduation year": "Graduation Year",
    "grad year": "Graduation Year",
    "class year": "Class of",
    "class of": "Class of",
    "net id": "Net ID",
    "netid": "Net ID",
    "byu id": "BYU ID (9 digits)",
    # Free text
    "notes": "Notes",
    "note": "Notes",
    "comments": "Notes",
    "comment": "Notes",
}

# Combined "full name" columns: split into a synthetic First name / Last Name
# pair before mapping (the intake template has no combined-name column, and the
# split has to happen before ``_map_row`` sees the row).
_FULL_NAME_HEADERS: frozenset[str] = frozenset(
    {
        "name",
        "full name",
        "attendee name",
        "attendee",
        "participant",
        "participant name",
        "registrant",
        "registrant name",
        "guest name",
    }
)

# Headers that carry the maiden / birth name. ``birth_name`` is a real alumni
# column but is NOT on the friend intake template, so it has no entry in
# ``_MAPPING`` — it is applied to the payload directly after mapping.
_MAIDEN_HEADER = "Maiden name"

_HEADER_PUNCT = re.compile(r"[^a-z0-9]+")


def _header_key(header: str) -> str:
    """Lower-case, punctuation-stripped header key used for alias lookup."""
    return _HEADER_PUNCT.sub(" ", header.strip().lower()).strip()


def _alias_headers(raw_headers: list[str]) -> tuple[list[str], list[str], int | None]:
    """Translate the file's headers into intake-template headers.

    Returns ``(headers, ignored, full_name_index)``:
      * ``headers`` — one entry per source column: the canonical intake header
        when the column maps to a DB field, else the ORIGINAL header text (which
        ``_map_row`` then ignores, because ``_MAPPING.get`` misses).
      * ``ignored`` — the original text of every column that maps to nothing.
        Reported to the operator so an ignored column is visible, not silent —
        but it is never an error (Jake, 2026-08-04).
      * ``full_name_index`` — column index of a combined "Name" column, or None.
    """
    # Exact intake headers win over the alias table (a file that genuinely uses
    # the template's spelling must map identically to the alumni importer).
    exact = {_header_key(h): h for h in _MAPPING}
    headers: list[str] = []
    ignored: list[str] = []
    full_name_index: int | None = None

    for i, raw in enumerate(raw_headers):
        canonical = _canonicalize_header(raw.strip())
        key = _header_key(canonical)
        if not key:
            headers.append(canonical)
            continue
        if key in _FULL_NAME_HEADERS and full_name_index is None:
            full_name_index = i
            headers.append(canonical)
            continue
        target = exact.get(key) or _HEADER_ALIASES.get(key)
        if target is None:
            headers.append(canonical)
            ignored.append(raw.strip())
            continue
        headers.append(target)
    return headers, ignored, full_name_index


def _split_full_name(value: str) -> tuple[str, str]:
    """Split a combined name cell into (first, last).

    Handles "Last, First" as well as "First Last" and "First Middle Last" — the
    surname is the LAST token in the plain form, everything before it is the
    given-name run (so a middle name never becomes the surname).
    """
    text = " ".join(value.split())
    if not text:
        return "", ""
    if "," in text:
        last, _, rest = text.partition(",")
        return rest.strip(), last.strip()
    parts = text.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


# --- Stage 1: parse + map ----------------------------------------------------


def parse_and_map(
    file_bytes: bytes, max_rows: int | None = MAX_ATTENDEE_ROWS
) -> tuple[list[dict], list[str], list[str]]:
    """Parse an attendee file into per-row friend payloads + match keys.

    Returns ``(rows, header_errors, ignored_columns)``.

    A row dict is::

        {"row": int, "display_name": str, "first_name": str|None,
         "last_name": str|None, "maiden_name": str|None, "emails": [str],
         "company": str|None, "graduation_year": int|None, "note": str|None,
         "payload": dict, "cell_warnings": [str]}

    ``payload`` is a full ``AlumniCreateFull``-shaped friend payload
    (``is_alumni = False``) built by the SAME ``import_csv._map_row`` the alumni
    and friends imports use, so a friend created from a no-match row carries
    every column the file gave us that maps to a DB field.

    ``header_errors`` is non-empty ONLY for problems that make the file
    unusable: undecodable, empty, or carrying no name/email column at all. An
    unrecognised column is NEVER a header error — it is reported in
    ``ignored_columns`` and dropped.
    """
    text = _decode_upload(file_bytes)
    if text is None:  # pragma: no cover - latin-1 never raises
        return [], ["The file could not be read. Re-save it as CSV UTF-8."], []
    reader = csv.reader(io.StringIO(text))
    try:
        raw_header_row = next(reader)
    except StopIteration:
        return [], ["The file is empty."], []

    if len(raw_header_row) == 1 and (
        raw_header_row[0].count(";") >= 2 or raw_header_row[0].count("\t") >= 2
    ):
        return (
            [],
            [
                "This looks like a semicolon- or tab-delimited file. Re-save it "
                "as a comma-delimited CSV and try again."
            ],
            [],
        )

    headers, ignored, full_name_index = _alias_headers(raw_header_row)

    # A duplicated MAPPED column is ambiguous (header->index is last-wins), so
    # reject it. Duplicated ignored columns are irrelevant and stay ignored.
    mapped = [h for h in headers if h in _MAPPING or h == _MAIDEN_HEADER]
    header_errors = [
        f"Duplicate column: {dup!r}."
        for dup in sorted({h for h in mapped if mapped.count(h) > 1})
    ]

    has_name = full_name_index is not None or any(
        h in ("First name", "Last Name", "Preferred first name") for h in headers
    )
    has_email = any(h in ("Personal Email", "Work Email") for h in headers)
    if not has_name and not has_email:
        header_errors.append(
            "No usable attendee column found. The file needs at least a name "
            "column (Name, or First name / Last name) or an email column."
        )
    if header_errors:
        return [], header_errors, ignored

    rows: list[dict] = []
    for offset, raw_row in enumerate(reader, start=2):
        if not any((cell or "").strip() for cell in raw_row):
            continue
        if max_rows is not None and len(rows) >= max_rows:
            return (
                [],
                [
                    f"File exceeds the {max_rows:,}-row limit. Split it into "
                    "smaller batches."
                ],
                ignored,
            )
        rows.append(_build_row(offset, headers, list(raw_row), full_name_index))
    return rows, [], ignored


def _build_row(
    row_num: int,
    headers: list[str],
    raw_row: list[str],
    full_name_index: int | None,
) -> dict:
    """Map one attendee row to a friend payload + the keys matching needs."""
    row_headers = list(headers)
    cells = list(raw_row)

    # A combined name column becomes a synthetic First name / Last Name pair,
    # but never overwrites explicit first/last columns that are already filled.
    if full_name_index is not None and full_name_index < len(cells):
        combined = (cells[full_name_index] or "").strip()
        if combined:
            first, last = _split_full_name(combined)
            if first and "First name" not in row_headers:
                row_headers.append("First name")
                cells.append(first)
            if last and "Last Name" not in row_headers:
                row_headers.append("Last Name")
                cells.append(last)

    # The maiden-name column is a real alumni field with no friend-template
    # header, so it is pulled out here and applied to the payload below.
    maiden = None
    for i, header in enumerate(row_headers):
        if header == _MAIDEN_HEADER and i < len(cells):
            maiden = (cells[i] or "").strip() or None

    mapped = _map_row(row_num, row_headers, cells, _MAPPING, friend=True)
    payload: dict = mapped["payload"]
    if maiden:
        payload["birth_name"] = maiden

    contact = payload.get("contact") or {}
    career = payload.get("career") or {}
    emails = [
        e
        for e in (
            _norm_email(contact.get("personal_email")),
            _norm_email(contact.get("work_email")),
        )
        if e
    ]

    first_name = payload.get("preferred_first_name") or payload.get("first_name")
    last_name = payload.get("last_name")
    display = " ".join(p for p in (first_name, last_name) if p).strip()
    if not display:
        display = emails[0] if emails else "(unnamed)"

    # A cell that failed to coerce (a bad year, an unparseable date) is a
    # WARNING, not a rejection: _map_row already dropped that one cell and the
    # rest of the row is still perfectly usable for matching. Never fail a row
    # over one bad cell.
    cell_warnings = [mapped["error"]] if mapped["error"] else []

    return {
        "row": row_num,
        "display_name": display,
        "first_name": payload.get("first_name") or payload.get("preferred_first_name"),
        "preferred_first_name": payload.get("preferred_first_name"),
        "last_name": last_name,
        "maiden_name": maiden,
        "emails": emails,
        "company": career.get("current_employer"),
        "graduation_year": payload.get("graduation_year"),
        "note": payload.get("notes"),
        "payload": payload,
        "cell_warnings": cell_warnings,
    }


# --- Normalization + name knowledge ------------------------------------------


def _norm_email(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


_NAME_PUNCT = re.compile(r"[^a-z0-9]+")


def _strip_accents(value: str) -> str:
    return "".join(
        ch
        for ch in unicodedata.normalize("NFKD", value)
        if not unicodedata.combining(ch)
    )


def _norm_name(value: object) -> str:
    """Accent-folded, lower-cased, punctuation-collapsed name form."""
    if not isinstance(value, str):
        return ""
    return " ".join(_NAME_PUNCT.sub(" ", _strip_accents(value).lower()).split())


# Surname suffixes that carry no identity ("John Smith Jr" == "John Smith").
_SURNAME_SUFFIXES: frozenset[str] = frozenset(
    {"jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "cfa", "cpa", "esq", "mba"}
)


def surname_keys(value: object) -> set[str]:
    """Every surname form a match may key on, for ONE surname.

    Expansion happens HERE, in Python, so the SQL stays exact equality against a
    functional index — never a leading-wildcard ILIKE over the whole roster.
    "Smith-Jones" yields {"smith jones", "smithjones", "smith", "jones"} so a
    hyphenated married name matches either half, in either direction.
    """
    base = _norm_name(value)
    if not base:
        return set()
    parts = [p for p in base.split(" ") if p and p not in _SURNAME_SUFFIXES]
    if not parts:
        return set()
    keys = {" ".join(parts), "".join(parts)}
    if len(parts) > 1:
        keys.update(parts)
    return {k for k in keys if len(k) >= 2}


# Nickname / diminutive equivalences. Conference lists use what the attendee
# types on a registration form ("Mike Smith"); the database holds what the
# registrar recorded ("Michael Smith"), and preferred_first_name only helps when
# somebody filled it in. Groups are symmetric — every member is equivalent to
# every other. Deliberately conservative: only unambiguous, common pairs.
_NICKNAME_GROUPS: tuple[tuple[str, ...], ...] = (
    ("abigail", "abby", "abbie"),
    ("alexander", "alex", "xander", "sandy"),
    ("alexandra", "alex", "alexa", "sasha"),
    ("andrew", "andy", "drew"),
    ("anthony", "tony"),
    ("benjamin", "ben", "benji"),
    ("bradley", "brad"),
    ("catherine", "kate", "katie", "cathy", "cate", "kathy"),
    ("katherine", "kate", "katie", "kathy", "kat"),
    ("charles", "charlie", "chuck", "chas"),
    ("christopher", "chris", "topher"),
    ("christine", "chris", "chrissy", "tina"),
    ("daniel", "dan", "danny"),
    ("deborah", "deb", "debbie"),
    ("douglas", "doug"),
    ("edward", "ed", "eddie", "ted", "ned"),
    ("elizabeth", "liz", "beth", "lizzie", "betsy", "eliza"),
    ("frederick", "fred", "freddie"),
    ("gregory", "greg"),
    ("jacob", "jake"),
    ("james", "jim", "jimmy", "jamie"),
    ("jeffrey", "jeff"),
    ("jennifer", "jen", "jenny"),
    ("jonathan", "jon", "jonny"),
    ("joseph", "joe", "joey"),
    ("joshua", "josh"),
    ("kenneth", "ken", "kenny"),
    ("lawrence", "larry"),
    ("margaret", "maggie", "meg", "peggy", "marge"),
    ("matthew", "matt"),
    ("michael", "mike", "mikey", "mick"),
    ("nathaniel", "nate", "nathan"),
    ("nicholas", "nick", "nicky"),
    ("patricia", "pat", "patty", "trish"),
    ("patrick", "pat", "paddy"),
    ("peter", "pete"),
    ("philip", "phil"),
    ("phillip", "phil"),
    ("rebecca", "becky", "becca"),
    ("richard", "rick", "dick", "richie", "rich"),
    ("robert", "bob", "rob", "bobby", "robbie"),
    ("ronald", "ron", "ronnie"),
    ("samuel", "sam", "sammy"),
    ("stephen", "steve", "steph"),
    ("steven", "steve"),
    ("susan", "sue", "susie", "suzy"),
    ("theodore", "ted", "teddy", "theo"),
    ("thomas", "tom", "tommy"),
    ("timothy", "tim", "timmy"),
    ("victoria", "vicky", "tori"),
    ("william", "will", "bill", "billy", "willie", "liam"),
    ("zachary", "zach", "zack"),
)

_NICKNAME_INDEX: dict[str, set[str]] = {}
for _group in _NICKNAME_GROUPS:
    for _member in _group:
        _NICKNAME_INDEX.setdefault(_member, set()).update(_group)


def given_keys(value: object) -> set[str]:
    """Every given-name form to key on: the normalized name, each of its tokens,
    and every nickname equivalent of those tokens."""
    base = _norm_name(value)
    if not base:
        return set()
    keys = {base}
    keys.update(base.split(" "))
    for token in list(keys):
        keys.update(_NICKNAME_INDEX.get(token, ()))
    return {k for k in keys if k}


def given_names_agree(file_given: object, candidate_names: list[object]) -> str | None:
    """How a file's given name agrees with a candidate's name columns.

    Returns ``"exact"``, ``"nickname"``, ``"initial"``, or ``None``. The caller
    passes ``first_name``, ``preferred_first_name`` and ``middle_name`` — the
    preferred name matters because this app stores and displays it everywhere,
    and an alumna listed as "Kate" on a conference badge is "Katherine" in the
    registrar's export.
    """
    left = _norm_name(file_given)
    if not left:
        return None
    left_tokens = set(left.split(" "))
    left_keys = given_keys(file_given)

    best: str | None = None
    for name in candidate_names:
        right = _norm_name(name)
        if not right:
            continue
        right_tokens = set(right.split(" "))
        if left == right or (left_tokens & right_tokens):
            return "exact"
        if left_keys & given_keys(name):
            best = "nickname"
        elif best is None and left[0] == right[0] and (len(left) == 1 or len(right) == 1):
            # One side is a bare initial ("J. Smith" vs "John Smith"). Weakest
            # possible agreement — kept only so the reviewer SEES the candidate.
            best = "initial"
    return best


# Company words that carry no identity, so "Goldman Sachs & Co." and "Goldman"
# corroborate each other.
_COMPANY_STOPWORDS: frozenset[str] = frozenset(
    {
        "the", "and", "of", "co", "company", "corp", "corporation", "inc",
        "incorporated", "llc", "llp", "lp", "ltd", "limited", "plc", "group",
        "holdings", "partners", "capital", "international", "global", "usa",
        "us", "na", "sa", "ag", "gmbh", "bv", "nv", "pte", "pty", "pc",
        "associates", "services", "solutions",
    }
)


def company_tokens(value: object) -> list[str]:
    """Significant, order-preserving tokens of a company name."""
    base = _norm_name(value)
    if not base:
        return []
    tokens = [t for t in base.split(" ") if t and t not in _COMPANY_STOPWORDS]
    return tokens or [t for t in base.split(" ") if t]


def companies_corroborate(left: object, right: object) -> bool:
    """True when two employer strings plausibly name the same employer.

    Corroboration ONLY — never a match key and never a rejection. Two names
    corroborate when their significant token sets intersect, or when one
    normalized name contains the other ("Goldman" in "Goldman Sachs & Co.").
    """
    left_tokens = company_tokens(left)
    right_tokens = company_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if set(left_tokens) & set(right_tokens):
        return True
    left_join = "".join(left_tokens)
    right_join = "".join(right_tokens)
    return left_join in right_join or right_join in left_join


# --- Candidate loading (batched, SQL-side) -----------------------------------


def _candidate_select():
    """The candidate projection: enough context for a human to actually decide
    (name, grad year, employer, title, work city/state, net id, email)."""
    return (
        select(
            Alumni.alumni_id,
            Alumni.net_id,
            Alumni.first_name,
            Alumni.middle_name,
            Alumni.last_name,
            Alumni.preferred_first_name,
            Alumni.birth_name,
            Alumni.graduation_year,
            Alumni.is_alumni,
            AlumniContactInfo.personal_email,
            AlumniContactInfo.work_email,
            CurrentEmployment.current_employer,
            CurrentEmployment.current_title,
            CurrentEmployment.current_city,
            CurrentEmployment.current_state,
        )
        .outerjoin(
            AlumniContactInfo, AlumniContactInfo.alumni_id == Alumni.alumni_id
        )
        .outerjoin(
            CurrentEmployment, CurrentEmployment.alumni_id == Alumni.alumni_id
        )
        .where(Alumni.archived.is_(False))
    )


def _norm_col(column):
    """SQL-side twin of :func:`_norm_name` for the exact-equality legs.

    ``lower(trim(col))`` and nothing else — deliberately NOT a leading-wildcard
    ILIKE, and deliberately un-CAST, so it matches the functional indexes added
    in ``2026-08-04_attendee_match_indexes.sql`` verbatim (a stray ``::varchar``
    would silently disqualify the index). Accent / punctuation / hyphen variants
    are handled by expanding the PYTHON key set instead, which keeps the SQL an
    indexable equality test rather than a scan over 8,000+ alumni.
    """
    return func.lower(func.trim(column))


def build_email_query(emails: list[str]):
    """Candidates whose personal OR work email is one of ``emails`` (exact,
    case-insensitive). One query for the whole file."""
    return (
        _candidate_select()
        .where(
            or_(
                _norm_col(AlumniContactInfo.personal_email).in_(emails),
                _norm_col(AlumniContactInfo.work_email).in_(emails),
            )
        )
        .limit(_MAX_CANDIDATE_ROWS)
    )


def build_surname_query(surnames: list[str]):
    """Candidates whose ``last_name`` OR ``birth_name`` is one of ``surnames``.

    ``birth_name`` is the maiden-name column (#216): an alumna who married after
    graduating is found by either surname without a second query.
    """
    return (
        _candidate_select()
        .where(
            or_(
                _norm_col(Alumni.last_name).in_(surnames),
                _norm_col(Alumni.birth_name).in_(surnames),
            )
        )
        .limit(_MAX_CANDIDATE_ROWS)
    )


def build_given_company_query(givens: list[str], company_patterns: list[str]):
    """Candidates matching a given name AND corroborated by employer.

    This is the safety net for the case the surname legs CANNOT catch: the file
    carries a married surname the database has never seen (and ``birth_name`` is
    empty), so no surname key can match. Requiring an employer hit in SQL keeps
    the fan-out bounded — a bare "every alumnus called John" query is exactly
    the thing this module must not do.
    """
    return (
        _candidate_select()
        .where(
            and_(
                or_(
                    _norm_col(Alumni.first_name).in_(givens),
                    _norm_col(Alumni.preferred_first_name).in_(givens),
                ),
                or_(
                    *[
                        _norm_col(CurrentEmployment.current_employer).like(p)
                        for p in company_patterns
                    ]
                ),
            )
        )
        .limit(_MAX_CANDIDATE_ROWS)
    )


def _company_patterns(companies: list[str]) -> list[str]:
    """``%token%`` patterns from the most distinctive token of each company."""
    patterns: list[str] = []
    seen: set[str] = set()
    for company in companies:
        tokens = company_tokens(company)
        if not tokens:
            continue
        token = tokens[0]
        if len(token) < 3 or token in seen:
            continue
        seen.add(token)
        patterns.append(f"%{token}%")
        if len(patterns) >= _MAX_COMPANY_PATTERNS:
            break
    return patterns


def _row_to_candidate(row) -> dict:
    """One DB row -> the candidate dict the reviewer sees."""
    display = " ".join(
        p
        for p in (row.preferred_first_name or row.first_name, row.last_name)
        if p
    ).strip()
    return {
        "alumni_id": row.alumni_id,
        "name": display or f"Alumni #{row.alumni_id}",
        "first_name": row.first_name,
        "middle_name": row.middle_name,
        "last_name": row.last_name,
        "preferred_first_name": row.preferred_first_name,
        "birth_name": row.birth_name,
        "net_id": row.net_id,
        "graduation_year": row.graduation_year,
        "is_alumni": bool(row.is_alumni),
        "employer": row.current_employer,
        "title": row.current_title,
        "city": row.current_city,
        "state": row.current_state,
        "personal_email": row.personal_email,
        "work_email": row.work_email,
    }


# --- Scoring -----------------------------------------------------------------
#
# Tiers, highest first. The tier decides precedence; the numeric score only
# ranks WITHIN a tier. There is intentionally no threshold that turns a score
# into an applied match — a human approves every one.
TIER_EMAIL = "email"
TIER_NAME = "name"
TIER_NAME_COMPANY = "name_company"

_TIER_RANK = {TIER_EMAIL: 3, TIER_NAME: 2, TIER_NAME_COMPANY: 1}

# How much agreement on the given name is worth, within the name tier.
_GIVEN_SCORE = {"exact": 40, "nickname": 30, "initial": 10}


def score_candidate(row: dict, candidate: dict) -> dict | None:
    """Score ONE candidate against ONE attendee row.

    Returns ``None`` when the candidate does not plausibly match at all;
    otherwise a dict with ``tier``, ``score``, ``confidence`` and a list of
    human-readable ``evidence`` strings ("Email matches", "Maiden name matches",
    "Employer corroborates") so the reviewer can judge rather than trust a badge.
    """
    evidence: list[str] = []
    candidate_emails = {
        e
        for e in (
            _norm_email(candidate.get("personal_email")),
            _norm_email(candidate.get("work_email")),
        )
        if e
    }
    email_hit = next((e for e in row["emails"] if e in candidate_emails), None)

    given = given_names_agree(
        row.get("first_name") or row.get("preferred_first_name"),
        [
            candidate.get("first_name"),
            candidate.get("preferred_first_name"),
            candidate.get("middle_name"),
        ],
    )
    file_surnames = surname_keys(row.get("last_name")) | surname_keys(
        row.get("maiden_name")
    )
    surname_hit: str | None = None
    if file_surnames:
        if file_surnames & surname_keys(candidate.get("last_name")):
            surname_hit = "last"
        elif file_surnames & surname_keys(candidate.get("birth_name")):
            surname_hit = "birth"

    company_hit = companies_corroborate(
        row.get("company"), candidate.get("employer")
    )
    year_hit = (
        row.get("graduation_year") is not None
        and row.get("graduation_year") == candidate.get("graduation_year")
    )

    if email_hit:
        tier = TIER_EMAIL
        score = 100
        evidence.append(f"Email matches ({email_hit})")
    elif surname_hit and given:
        tier = TIER_NAME
        score = _GIVEN_SCORE[given]
        evidence.append(
            "Surname matches"
            if surname_hit == "last"
            else "Maiden name matches the record's birth name"
        )
        if given == "exact":
            evidence.append("Given name matches")
        elif given == "nickname":
            evidence.append("Given name matches a common nickname")
        else:
            evidence.append("Given name matches by initial only")
    elif given in ("exact", "nickname") and company_hit:
        # No surname agreement at all — this is the "married and we never
        # recorded the new surname" case, rescued only because the employer
        # corroborates. Lowest tier by construction.
        tier = TIER_NAME_COMPANY
        score = _GIVEN_SCORE[given] - 15
        evidence.append("Given name matches but the surname does NOT")
    else:
        return None

    if company_hit:
        score += 20
        evidence.append(f"Employer corroborates ({candidate.get('employer')})")
    elif row.get("company") and candidate.get("employer"):
        evidence.append(
            f"Employer differs (file: {row['company']}; "
            f"record: {candidate.get('employer')})"
        )
    if year_hit:
        score += 10
        evidence.append(f"Graduation year matches ({candidate.get('graduation_year')})")
    if not candidate.get("is_alumni"):
        evidence.append("Existing friend record (not an alumnus)")

    if tier == TIER_EMAIL:
        confidence = "high"
    elif tier == TIER_NAME and given == "exact" and (company_hit or year_hit):
        confidence = "high"
    elif tier == TIER_NAME and given in ("exact", "nickname"):
        confidence = "medium"
    else:
        confidence = "low"

    return {
        **candidate,
        "tier": tier,
        "score": score,
        "confidence": confidence,
        "evidence": evidence,
    }


def rank_candidates(row: dict, candidates: list[dict]) -> list[dict]:
    """Score, filter and rank a row's candidate pool.

    An EMAIL hit is decisive about which candidates are worth showing at all: if
    any candidate matches on email, only the email-tier candidates survive
    (Jake, 2026-08-04 — "an email hit is effectively certain"). Otherwise every
    surviving name-tier candidate is returned, ranked, so the runner-up is never
    hidden from the reviewer.
    """
    scored = [s for s in (score_candidate(row, c) for c in candidates) if s]
    if not scored:
        return []
    best_rank = max(_TIER_RANK[s["tier"]] for s in scored)
    if best_rank == _TIER_RANK[TIER_EMAIL]:
        scored = [s for s in scored if s["tier"] == TIER_EMAIL]
    scored.sort(key=lambda s: (-_TIER_RANK[s["tier"]], -s["score"], s["alumni_id"]))
    return scored[:MAX_CANDIDATES_PER_ROW]


# --- Stage 2: propose (dry run, NO writes) -----------------------------------


async def propose(session: AsyncSession, event: Event, rows: list[dict]) -> dict:
    """Build the per-row match proposal for ONE event. NO writes, ever.

    Resolves the whole file in at most FOUR batched queries (email leg, surname
    leg, given-name+employer leg, and the event's existing roster) regardless of
    how many rows the file has. Never fetches the roster into Python.
    """
    emails = sorted({e for r in rows for e in r["emails"]})
    surnames = sorted(
        {
            k
            for r in rows
            for k in (surname_keys(r["last_name"]) | surname_keys(r["maiden_name"]))
        }
    )
    givens = sorted(
        {
            k
            for r in rows
            if r.get("company")
            for k in given_keys(r.get("first_name") or r.get("preferred_first_name"))
        }
    )
    companies = [r["company"] for r in rows if r.get("company")]
    patterns = _company_patterns(companies)

    warnings: list[dict] = []
    pool: dict[int, dict] = {}

    async def _collect(stmt) -> None:
        result = (await session.execute(stmt)).all()
        if len(result) >= _MAX_CANDIDATE_ROWS:
            warnings.append(
                {
                    "code": "candidate_cap",
                    "message": (
                        f"More than {_MAX_CANDIDATE_ROWS:,} possible records were "
                        "considered; some candidates may not be shown. Split the "
                        "file into smaller batches for a complete review."
                    ),
                }
            )
        for db_row in result:
            candidate = _row_to_candidate(db_row)
            pool.setdefault(candidate["alumni_id"], candidate)

    if emails:
        await _collect(build_email_query(emails))
    if surnames:
        await _collect(build_surname_query(surnames))
    if givens and patterns:
        await _collect(build_given_company_query(givens, patterns))

    # Index the pool by the keys each leg can be looked up on, so per-row
    # scoring touches a handful of candidates instead of the whole pool.
    by_email: dict[str, list[dict]] = {}
    by_surname: dict[str, list[dict]] = {}
    by_given: dict[str, list[dict]] = {}
    for candidate in pool.values():
        for email in (
            _norm_email(candidate["personal_email"]),
            _norm_email(candidate["work_email"]),
        ):
            if email:
                by_email.setdefault(email, []).append(candidate)
        for key in surname_keys(candidate["last_name"]) | surname_keys(
            candidate["birth_name"]
        ):
            by_surname.setdefault(key, []).append(candidate)
        for key in given_keys(candidate["first_name"]) | given_keys(
            candidate["preferred_first_name"]
        ):
            by_given.setdefault(key, []).append(candidate)

    attending: set[int] = set(
        (
            await session.execute(
                select(EventAttendance.alumni_id).where(
                    EventAttendance.event_id == event.event_id
                )
            )
        )
        .scalars()
        .all()
    )

    reports: list[dict] = []
    seen_keys: dict[tuple, int] = {}
    tally = {"matched": 0, "ambiguous": 0, "no_match": 0, "already_attending": 0}

    for row in rows:
        row_warnings = list(row["cell_warnings"])

        # A row repeated inside the same file is counted once (the second
        # occurrence is reported, never silently merged away).
        dedup_key = (
            tuple(sorted(row["emails"])),
            _norm_name(row.get("first_name")),
            _norm_name(row.get("last_name")),
        )
        duplicate_of = seen_keys.get(dedup_key) if any(dedup_key) else None
        if duplicate_of is None and any(dedup_key):
            seen_keys[dedup_key] = row["row"]

        pool_for_row: dict[int, dict] = {}
        for email in row["emails"]:
            for candidate in by_email.get(email, ()):
                pool_for_row[candidate["alumni_id"]] = candidate
        for key in surname_keys(row["last_name"]) | surname_keys(row["maiden_name"]):
            for candidate in by_surname.get(key, ()):
                pool_for_row[candidate["alumni_id"]] = candidate
        if row.get("company"):
            for key in given_keys(
                row.get("first_name") or row.get("preferred_first_name")
            ):
                for candidate in by_given.get(key, ()):
                    pool_for_row[candidate["alumni_id"]] = candidate

        candidates = rank_candidates(row, list(pool_for_row.values()))
        for candidate in candidates:
            candidate["already_attending"] = candidate["alumni_id"] in attending

        if not candidates:
            status = "no_match"
        elif len(candidates) == 1:
            status = "matched"
        else:
            status = "ambiguous"
        tally[status] += 1
        if candidates and all(c["already_attending"] for c in candidates):
            tally["already_attending"] += 1

        if duplicate_of is not None:
            row_warnings.append(
                f"Also listed on row {duplicate_of}; approve it only once."
            )

        reports.append(
            {
                "row": row["row"],
                "status": status,
                "attendee": {
                    "name": row["display_name"],
                    "first_name": row["first_name"],
                    "last_name": row["last_name"],
                    "maiden_name": row["maiden_name"],
                    "email": row["emails"][0] if row["emails"] else None,
                    "company": row["company"],
                    "title": (row["payload"].get("career") or {}).get("current_title"),
                    "graduation_year": row["graduation_year"],
                },
                "match_key": "email" if row["emails"] else "name",
                "candidates": candidates,
                "warnings": row_warnings,
                "friend_fields": _friend_field_labels(row["payload"]),
            }
        )

    return {
        "event": {
            "event_id": event.event_id,
            "event_name": event.event_name,
            "event_date": event.event_date.isoformat() if event.event_date else None,
        },
        "columns_ok": True,
        "header_errors": [],
        "ignored_columns": [],
        "summary": {
            "total_rows": len(rows),
            "matched": tally["matched"],
            "ambiguous": tally["ambiguous"],
            "no_match": tally["no_match"],
            "already_attending": tally["already_attending"],
        },
        "rows": reports,
        "warnings": warnings,
    }


_SECTION_KEYS = ("contact", "career", "education", "engagement", "former", "leadership")


def _friend_field_labels(payload: dict) -> list[str]:
    """The DB fields a friend record created from this row would carry.

    Shown in the review UI so "create a friend" is never a black box — the
    operator sees exactly what the file gives us that maps to a column.
    """
    fields: list[str] = []
    for key, value in payload.items():
        if key in _SECTION_KEYS or key == "is_alumni" or value in (None, ""):
            continue
        fields.append(key)
    for section in _SECTION_KEYS:
        for key, value in (payload.get(section) or {}).items():
            if value not in (None, ""):
                fields.append(f"{section}.{key}")
    return sorted(fields)


# --- CSV template ------------------------------------------------------------

TEMPLATE_HEADERS: list[str] = [
    "First name",
    "Last name",
    "Maiden name",
    "Email",
    "Company",
    "Title",
    "Phone",
    "City",
    "State",
    "Notes",
]
_TEMPLATE_EXAMPLES: list[list[str]] = [
    [
        "Michael",
        "Smith",
        "",
        "mike.smith@goldman.com",
        "Goldman Sachs",
        "Managing Director",
        "801-555-0100",
        "New York",
        "New York",
        "Panelist",
    ],
    [
        "Kate",
        "Nielsen",
        "Barker",
        "",
        "Deseret Trust",
        "Analyst",
        "",
        "Salt Lake City",
        "Utah",
        "",
    ],
]


def build_template_csv() -> str:
    """A starting-point attendee CSV.

    Only a starting point on purpose: this importer does NOT require the
    template's columns. Any recognisable spelling maps (Email / E-mail Address /
    Company / Employer / Organization ...) and anything unrecognised is ignored,
    so a raw conference registration export can be uploaded untouched.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(TEMPLATE_HEADERS)
    for row in _TEMPLATE_EXAMPLES:
        writer.writerow(row)
    return buffer.getvalue()

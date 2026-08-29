"""Survey response review queue.

Alumni submit "confirm your info" updates from the public survey link; we STAGE
them (`submit_response`) as pending rows instead of touching the record. Staff
review each in the console (`list_pending`, with a before/after diff) and apply
(`apply_response` — writes the whitelisted fields to the real record) or reject
(`reject_response`).

Every field an alum can submit is in `_FIELDS` (key -> table/column/kind), which
is the ONLY thing that gets written — nothing else in the payload is applied.

READ THIS BEFORE ADDING A FIELD. `apply_response` writes with a raw `setattr`,
so NOTHING in `app/schemas/alumni.py` runs on this path — every field validator
the staff create/edit endpoints rely on is bypassed here. The survey made these
columns a PUBLIC write surface (anyone holding a link can POST at the
whitelist), and staff review is not a substitute for validation: a reviewer sees
a plausible before/after diff, not a `javascript:` href or a second recipient
hidden in an email. A field whose value is trusted downstream — rendered as a
link, handed to the sender, grouped in a dashboard, filtered on — must carry its
rule HERE, as a `choice`, a `designation`, a `_Field.max_length` or a
`_Field.validate` predicate.

Reuse the staff rule, never restate it. Every predicate below delegates to the
matching validator in `app/schemas/alumni.py` (or is built from the same
constants), because two copies of "what is a valid name / year / birthday" drift,
and the drift is invisible until someone notices the public path accepting what
the staff path rejects — which is the whole bug this file keeps re-fixing.

An apply is also audited FIELD BY FIELD (#45) — see `apply_response`. The
`setattr` is exactly why: nothing upstream of it records what a column held
before, so the capture lives at the write. Audit `field_name`s use the STAFF
path's namespace, not the survey key's — see `_AUDIT_SECTION_BY_GROUP`.

These rows ARE the survey history the profile's Surveys tab shows: it derives
from them in `profile._derive_survey_history`. Do NOT also insert into the
legacy `surveys` table (see `models.crm.Survey`) — one fact, one home.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit_context import (
    AUDIT_SOURCE_SURVEY,
    audit_source,
    audit_source_scope,
    new_change_set_id,
)
from app.core.dropdowns import (
    INDUSTRIES,
    MARITAL_STATUSES,
    SURVEY_EMPLOYMENT_STATUSES,
    holds_designation,
)
from app.core.errors import InvalidRequestError, NotFoundError, ServiceError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.contact import AlumniContactInfo
from app.models.employment import CurrentEmployment
from app.models.engagement import AlumniProgramEngagement
from app.models.survey_response import SurveyResponse
from app.schemas.alumni import (
    _NAME_DISALLOWED,
    AlumniBase,
    _has_control_chars,
    _has_invisible_chars_strict,
)
from app.schemas.survey import (
    SurveyChange,
    SurveyResponseItem,
    SurveySubmitResult,
)
from app.services import headshot_index, hygiene, supabase_storage
from app.services.alumni import (
    _unchanged,
    archive_current_role,
    audit_role_archive,
    career_snapshot,
    employer_changed,
)
from app.services.images import normalise_headshot
from app.services.survey_email import (
    LINK_DEAD_MESSAGE,
    RESPONDED_STATUSES,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    _resurvey_cutoff,
    response_not_superseded,
    sent_cycle_and_stage,
    verify_survey_token,
)

log = logging.getLogger(__name__)

# A staged survey photo lives in the SAME private bucket as real headshots, but
# under a `survey-pending/` prefix so it can NEVER overwrite an alum's actual
# headshot before an admin approves it. On apply it's copied to the headshot key
# (the alum's net_id, or their alumni_id as a fallback) and the staged copy is
# removed; on reject the staged copy is just removed.
_HEADSHOT_BUCKET = "headshots"
_STAGED_PHOTO_PREFIX = "survey-pending"


def _staged_photo_path(survey_response_id: int) -> str:
    return f"{_STAGED_PHOTO_PREFIX}/{survey_response_id}"


def _headshot_key(alum: Alumni) -> str:
    """The object key an alum's headshot is stored under: their net_id, or their
    alumni_id as a string when they have no net_id (so a missing net_id NEVER
    hard-fails an otherwise-valid approval)."""
    net_id = (alum.net_id or "").strip()
    return net_id or str(alum.alumni_id)


# Every photo this module writes is the output of `normalise_headshot`, which
# always encodes JPEG. The stored content type is therefore a CONSTANT, not
# something to derive from the bytes.
#
# This replaces a `_sniff_image_content_type` helper that read as a check and was
# not one: it tested the PNG and WebP magic, never the JPEG magic, and returned
# "image/jpeg" for everything else — including a text file. It could not reject
# anything, so an unreadable object was promoted onto an alum's profile labelled
# as a JPEG. Re-encoding answers both halves at once: the bytes are known good
# because we produced them, and their type is known because we chose it.
_PROMOTED_CONTENT_TYPE = "image/jpeg"


@dataclass(frozen=True)
class _Field:
    key: str
    label: str
    group: str  # alumni | contact | employment | engagement
    column: str
    kind: str  # text | int | bool | date | designation | choice
    # `designation` only: the canonical string written to the column when the alum
    # ticks the box. Lives HERE, server-side, never in the payload — see `_coerce`.
    marker: str | None = None
    # `choice` only: the exact values that may be written. Anything else the alum
    # (or anyone holding a survey link) submits is IGNORED — see `_coerce`.
    options: tuple[str, ...] | None = None
    # `text` only: the column's own width, from database/schema.sql. A longer
    # submission is refused (`_IGNORE`) rather than truncated — see `_coerce`.
    #
    # This is a SEPARATE rule from `validate`, not part of it, because the two
    # answer different questions: `validate` asks "is this value's SHAPE
    # acceptable", this asks "does the column physically hold it". Bundling the
    # length into each predicate would mean one cap per rule rather than one cap
    # per column, and `_valid_free_text` guards columns from varchar(20) to
    # varchar(255).
    #
    # Without it a public submit could stage a value no column can hold. For most
    # of these the apply is simply a 500 (Postgres 22001) — bad, but loud. The one
    # that is quietly expensive is `other_designations`: an unbounded `text`
    # column carrying a trigram GIN index, so a multi-megabyte answer is accepted,
    # stored, and bloats an index every alumni search reads.
    max_length: int | None = None
    # False when a blank submission is NOT an instruction to clear the column.
    # Defaults True, which is the long-standing `text` behaviour (blank -> NULL).
    # Turned off for the identity columns the confirm page pre-fills: an empty box
    # there means the field was cleared or never rendered, not that the alumnus has
    # no surname. See the name block in `_FIELDS`.
    blankable: bool = True
    # `text` only: a predicate the SUBMITTED value must satisfy before the server
    # will write it. Returning False is treated exactly like an off-list `choice`
    # — `_coerce` yields `_IGNORE` and the column keeps whatever it already held.
    #
    # This exists because `apply_response` writes with a raw `setattr`, which runs
    # NO Pydantic validator: everything `app/schemas/alumni.py` enforces on the
    # staff create/edit path is bypassed on this one. That was tolerable while the
    # only writers were staff; the survey turned these columns into a PUBLIC write
    # surface (anything holding a link can POST at the whitelist), so a field whose
    # value is trusted downstream needs its rule restated HERE, on the path the
    # public actually writes through. See `_valid_linkedin_url` / `_valid_email`.
    validate: Callable[[str], bool] | None = None


# What `_coerce` returns for a value the server refuses to write. DISTINCT from
# `None`, which is a real instruction ("store NULL"). The apply path skips the
# column entirely on this, so an off-list or disallowed-blank answer leaves what
# is on file exactly as it was rather than overwriting it.
_IGNORE = object()


# --------------------------------------------------- public-write field rules --


def _valid_linkedin_url(value: str) -> bool:
    """The STAFF LinkedIn rule, reused verbatim: http(s) scheme, a linkedin.com
    host (or subdomain), within the column's 500 characters.

    Deliberately delegates to `AlumniBase._validate_linkedin_url` rather than
    restating the checks. Two copies of "what is a LinkedIn URL" would drift, and
    the drift would be invisible until someone noticed the public path accepting
    what the staff path rejects — which is the whole bug this fixes.

    Why it matters that this is enforced at all: the stored value is rendered as
    an `href` on staff pages, so `javascript:...` becomes a script a signed-in
    reviewer runs by clicking, and `https://linkedin.com.evil.example/in/jdoe`
    becomes a credential-harvest link that LOOKS like the profile they meant to
    open. The reviewer's before/after diff shows a plausible URL either way — a
    human eyeballing the queue is not a control that catches this.
    """
    try:
        AlumniBase._validate_linkedin_url(value)
    except ValueError:
        return False
    return True


# Mirrors alumni_contact_info.personal_email / work_email (varchar(255)), so a
# submission we accept is one the column can actually hold.
_EMAIL_MAX = 255

# Characters that must never appear in a stored address. This is a SHAPE gate,
# not a deliverability gate:
#
# * whitespace (including CR/LF/TAB) and commas/semicolons are RECIPIENT-LIST and
#   header separators. The stored value is what `email_reach.resolve_email` hands
#   the sender as the `to` address, so `alum@byu.edu, attacker@evil.example` is a
#   silent BCC of every future survey mail, reminder and notification to a third
#   party — and it renders in the console as one ordinary-looking address.
# * angle brackets, quotes and backslashes are the RFC 5322 display-name
#   machinery (`"Finance Alumni" <attacker@evil.example>`), which lets a value
#   that displays as one address deliver to another.
#
# Everything else stays permissive ON PURPOSE — see `_valid_email`.
# `?` and `&` are here for a reason that is NOT about email syntax: a stored
# address is rendered as `href={`mailto:${email}`}` on the profile page, and in
# a mailto: URL those two characters start and separate QUERY PARAMETERS
# (RFC 6068). An address containing `?subject=…&body=…` therefore pre-fills the
# compose window a staff member opens by clicking "Send" — attacker-authored
# text, in a mail client, from a message the staff member believes they wrote.
# `:` is excluded for the same family of reasons (scheme confusion).
# A true second recipient is already blocked by the single-`@` rule below.
# Found re-reviewing the #418 fix, 2026-08-07.
_EMAIL_DISALLOWED = frozenset(',;<>"()[]\\?&:')


def _valid_email(value: str) -> bool:
    """A minimal SHAPE gate for an address submitted through the public survey.

    Note what this deliberately does NOT do: it does not call
    `email_reach.is_sendable_email`. That function's permissiveness is a
    documented decision (see its docstring) — being stricter there was silently
    dropping real alumni, and it also rejects the reserved domains that dev's
    entire seed dataset uses. Reusing it as the survey's write gate would make
    the survey un-testable on dev and would re-introduce the exact failure that
    module exists to remove. The staff/import gate stays exactly as it is; this
    rule only governs what a stranger with a survey link may PUT in the column.

    So the bar here is "is this one address, and only one" rather than "is this
    address real": exactly one `@`, a non-empty local part and a domain with a
    dot, nothing longer than the column, and none of `_EMAIL_DISALLOWED`. A
    misspelled-but-well-formed address still gets through and is still a data
    problem staff can see and fix — which is correct. A second recipient smuggled
    into the field does not.
    """
    if len(value) > _EMAIL_MAX:
        return False
    if any(ch.isspace() or ch in _EMAIL_DISALLOWED or ord(ch) < 0x20 for ch in value):
        return False
    # The whitespace test above does NOT cover zero-width characters — Python's
    # `str.isspace()` is False for U+200B, and its ordinal is far above 0x20, so
    # all three of the checks on the line above miss it (found 2026-08-08,
    # re-reviewing the #418 fix). An address carrying one looks identical to a
    # clean one everywhere a human reads it, while being a different string to
    # every exact-match suppression and dedup check — and it will bounce at the
    # provider. No invisible character is ever legitimate in an address, so this
    # uses the STRICT set, joiners included, unlike the name rule.
    if _has_invisible_chars_strict(value):
        return False
    local, sep, domain = value.partition("@")
    if not sep or "@" in domain:
        return False
    return (
        bool(local)
        and "." in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
    )


def _valid_name(value: str) -> bool:
    """The STAFF name rule, reused verbatim on the six name columns (#426).

    Deliberately delegates to `AlumniBase._validate_name` for the same reason
    `_valid_linkedin_url` delegates: two copies of "what may appear in a name"
    would drift, and the drift would be invisible until someone noticed the
    public path accepting what the staff path rejects.

    What it blocks: `;=<>|` (meaningful to a SQL parser, meaningless in a name),
    control characters, a LEADING `=`/`+`/`-`/`@` (CSV formula injection), a
    digits-only value, and anything past the column's 100 characters.

    Deliberately PERMISSIVE about real names — verified against the staff rule,
    2026-08-08: O'Brien-Smith, St. John, Anne-Marie, 'Alohilani and N'Diaye (both
    curly-apostrophe spellings), accented Latin and non-Latin scripts all pass. A
    deny-list is used precisely so a surname we have never seen is accepted by
    default; refusing one is far worse than storing an odd one.

    Applied here rather than left to the reviewer because a control character in
    an identity column is invisible in a before/after diff, and these are the
    columns search, the duplicate check and every export key off.
    """
    try:
        AlumniBase._validate_name(value)
    except ValueError:
        return False
    return True


def _free_text_ok(value: str, leads: str) -> bool:
    """The CHARACTER half of the staff name rule, for the free-text columns.

    Built from the same two things `AlumniBase._validate_name` is built from —
    `_NAME_DISALLOWED` and `_has_control_chars`, imported, not re-typed — so
    "which characters are disallowed" still has exactly one definition. What it
    deliberately does NOT carry are that rule's two NAME-SHAPE checks, because
    neither is true of these columns:

    * the 100-character cap — these run from varchar(20) (`zip`) to varchar(255)
      (`current_employer`), so the width belongs on `_Field.max_length`, per
      column, not baked into a shared predicate;
    * the digits-only rejection — a ZIP code and a phone number ARE digits only.

    *leads* is the set of first characters that turn the cell into a live formula
    on CSV export. `=` is in `_NAME_DISALLOWED` already, so callers pass the
    remainder.
    """
    if _has_control_chars(value):
        return False
    if value[0] in leads:
        return False
    return not (_NAME_DISALLOWED & set(value))


def _valid_free_text(value: str) -> bool:
    """The free-text rule for employer, title, city/state/country/zip, gender,
    citizenship, home country and the graduate fields (#426).

    Same disposition as everything else on this path: a value that fails is
    IGNORED, never rejected back at the alum and never written as NULL. That
    matters more here than for the emails — this is data pollution, mitigated at
    the far end (the CSV export neutralises formula leads, React escapes on
    render), so the cost of a false positive (silently dropping a real answer)
    is higher than the cost of a false negative.
    """
    return _free_text_ok(value, "+-@")


def _valid_phone(value: str) -> bool:
    """`_valid_free_text` with ONE exception: a leading `+`.

    `+1 801-555-0100` is how an international number is written, and it is the
    single most likely legitimate value in this whole whitelist to start with a
    formula lead. Blocking it would silently discard exactly the numbers hardest
    to re-collect. The staff path applies no character rule to `phone` at all
    (`ContactCreate.phone` carries only a length), so this is still strictly
    tighter than what staff can write, and a leading `+` is neutralised by the
    CSV export like any other lead.
    """
    return _free_text_ok(value, "-@")


def _valid_survey_text(value: str) -> bool:
    """The staff `_validate_survey_text` rule — reject control characters — for
    `other_designations`.

    Narrower than `_valid_free_text` on purpose: this column legitimately holds
    punctuation-heavy free text ("Series 7, Series 63; CFP Level 1"), so applying
    the name character set here would be STRICTER than the staff path, which is
    the mirror-image of the bug this issue is about. Control characters are the
    part staff already refuse.
    """
    return not _has_control_chars(value)


# The whitelist of fields an alum may submit + where each writes. Order matches
# the confirm page. Anything not here is ignored on submit AND on apply.
_FIELDS: tuple[_Field, ...] = (
    # CONTROLLED VOCABULARIES (#426). Both were free text here while the staff
    # path constrained them, which is the same shape of bug as marital status in
    # #647: a public submit could mint a phantom bucket that then appears in the
    # dashboard breakdown, the filter and `search_terms` as though it were one of
    # ours. `choice` is the fix for both, and it already does the two things that
    # make it safe to apply to columns prod has been filling from a free-text
    # intake sheet for years — an off-list answer is IGNORED rather than stored or
    # NULLed, and an off-list value already ON FILE still reads back verbatim.
    #
    # The lists are the canonical ones, imported, never restated: `INDUSTRIES` is
    # what `validate_industry` enforces on the staff path, and
    # `SURVEY_EMPLOYMENT_STATUSES` exists in `core/dropdowns.py` specifically for
    # this field (the canonical eight minus "Unknown", which is meaningless as a
    # self-description) and was, until now, imported by nothing.
    #
    # Checked against the frontend on 2026-08-08, because a mismatch here would
    # silently ignore every legitimate answer — far worse than the free text it
    # replaces. The survey's own dropdowns are static, not vocabulary-backed, so
    # they cannot drift at runtime:
    #   * employment status — `SURVEY_EMPLOYMENT_STATUS_OPTIONS` is the same seven
    #     strings in the same order, both derived the same way from the canonical
    #     eight. Exact match.
    #   * industry — the survey offers `PRIMARY_INDUSTRY_OPTIONS` minus "Other"
    #     (19 strings); all 19 are in `INDUSTRIES` exactly. The five it does not
    #     offer (the four primary-excluded, plus "Other") stay WRITABLE here on
    #     purpose: they are legitimate stored values, and `choice` re-canonicalises
    #     casing, so an alum re-submitting one already on file is not refused.
    #
    # KNOWN GAP, needs a frontend change (Jake): the survey's industry control has
    # an "Other" option that reveals a FREE-TEXT box. Whatever is typed there is
    # now off-list and therefore ignored, silently. The frontend fix is to make
    # that option submit the literal "Other" — which IS in `INDUSTRIES` — rather
    # than a typed string. Until then an alum choosing Other loses that answer.
    _Field(
        "employment.current_industry",
        "Industry",
        "employment",
        "current_industry",
        "choice",
        options=INDUSTRIES,
        # Same reason as marital status: an alum whose stored industry is off-list
        # sees a control with no matching option, and if leaving it alone wiped
        # the column the survey would destroy the very legacy value `choice` is
        # here to preserve.
        blankable=False,
    ),
    _Field(
        "profile.employment_status",
        "Employment status",
        "alumni",
        "employment_status",
        "choice",
        options=SURVEY_EMPLOYMENT_STATUSES,
        blankable=False,
    ),
    _Field(
        "employment.current_employer",
        "Company",
        "employment",
        "current_employer",
        "text",
        max_length=255,
        validate=_valid_free_text,
    ),
    _Field(
        "employment.current_title",
        "Title",
        "employment",
        "current_title",
        "text",
        max_length=255,
        validate=_valid_free_text,
    ),
    # Secondary industry stays FREE TEXT, unlike the primary above. It is free
    # text on the staff path too (`EmploymentCreate` bounds its length and runs no
    # `validate_industry` on it), and that consistency is deliberate — this is the
    # blank where "Education" or "Non-profit" goes. It still gets the character
    # rule and the column width.
    _Field(
        "employment.current_industry_secondary",
        "Secondary industry",
        "employment",
        "current_industry_secondary",
        "text",
        max_length=255,
        validate=_valid_free_text,
    ),
    _Field(
        "employment.current_city",
        "Employment city",
        "employment",
        "current_city",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "employment.current_state",
        "Employment state",
        "employment",
        "current_state",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "employment.current_country",
        "Employment country",
        "employment",
        "current_country",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "employment.current_zip",
        "Company ZIP",
        "employment",
        "current_zip",
        "text",
        max_length=20,
        validate=_valid_free_text,
    ),
    _Field(
        "contact.city",
        "Residence city",
        "contact",
        "city",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "contact.state",
        "Residence state",
        "contact",
        "state",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "contact.country",
        "Residence country",
        "contact",
        "country",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    # Spouse names carry the same rule as the alum's own four below: they are
    # names, and `_validate_name` is the staff rule for them.
    _Field(
        "profile.spouse_first_name",
        "Spouse first name",
        "alumni",
        "spouse_first_name",
        "text",
        max_length=100,
        validate=_valid_name,
    ),
    _Field(
        "profile.spouse_last_name",
        "Spouse last name",
        "alumni",
        "spouse_last_name",
        "text",
        max_length=100,
        validate=_valid_name,
    ),
    # "Personal email", not "Permanent email" (#392): the profile UI, the intake
    # sheet and staff all call this column the personal email, and the survey
    # calling it something else made "it says I have no personal email" hard to
    # reconcile with a form that plainly showed one. One name everywhere.
    #
    # Both email columns carry `_valid_email` (#418). They are the ONLY fields on
    # this whitelist whose stored value is later handed to an outbound sender, so
    # an unvalidated one is not merely bad data — a second address smuggled in
    # here silently redirects or copies every future mail we send that alumnus.
    _Field(
        "contact.personal_email",
        "Personal email",
        "contact",
        "personal_email",
        "text",
        max_length=255,
        validate=_valid_email,
    ),
    _Field(
        "contact.work_email",
        "Work email",
        "contact",
        "work_email",
        "text",
        max_length=255,
        validate=_valid_email,
    ),
    _Field(
        "contact.phone",
        "Phone",
        "contact",
        "phone",
        "text",
        max_length=50,
        validate=_valid_phone,
    ),
    # `_validate_linkedin_url` on the staff schemas never ran on this path (#418):
    # `apply_response` writes with `setattr`, so no Pydantic validator fires. The
    # column renders as an `href` on staff pages, which makes an unvalidated
    # public write a link a signed-in reviewer clicks. Same rule, restated where
    # the public writes. See `_valid_linkedin_url`.
    _Field(
        "profile.linkedin_url",
        "LinkedIn",
        "alumni",
        "linkedin_url",
        "text",
        max_length=500,
        validate=_valid_linkedin_url,
    ),
    _Field(
        "profile.graduate_degree",
        "Graduate program",
        "alumni",
        "graduate_degree",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "profile.graduate_school",
        "Graduate school",
        "alumni",
        "graduate_school",
        "text",
        max_length=255,
        validate=_valid_free_text,
    ),
    # THE ONLY `int` field on the whitelist, and the one that poisoned the review
    # queue (#426). `_coerce` did a bare `int()`, so `"9" * 20` became a 20-digit
    # Python integer that sailed through submit and staged fine — and then failed
    # at APPLY, where Postgres raises 22003 on an `int` column. The transaction
    # rolls back, the response never leaves `pending`, and a reviewer who clicks
    # Approve gets a 500 every time, forever. Anyone holding a survey link could
    # do it, repeatedly. The year range in `_coerce`'s `int` branch is what stops
    # it; see the note there for why it is on the KIND, not on the field.
    _Field(
        "profile.graduate_graduation_year",
        "Projected graduation year",
        "alumni",
        "graduate_graduation_year",
        "int",
    ),
    # Finance designations (#529). CFA/CFP are their OWN varchar columns on
    # alumni_program_engagement holding the literal marker ('CFA'/'CFP'), not
    # booleans and not free text: the designation filter maps "CFA" ->
    # cfa_designation and matches `holds_designation` — non-NULL and not one of
    # the negatives (`core/dropdowns.py`, `repositories/alumni.py`) — and the
    # exports/import read the same columns. Writing a ticked CFA into
    # `other_designations` instead would drop that alum out of the CFA filter and
    # the designation counts, so the two live apart on purpose.
    #
    # Anything with a survey link can POST to this whitelist, so `designation`
    # deliberately ignores the submitted text and writes the marker from the field
    # definition — a `text` kind here would let a stranger put 100 arbitrary
    # characters into a column the rest of the app reads as "holds the CFA".
    _Field(
        "program.cfa_designation",
        "CFA designation",
        "engagement",
        "cfa_designation",
        "designation",
        "CFA",
    ),
    _Field(
        "program.cfp_designation",
        "CFP designation",
        "engagement",
        "cfp_designation",
        "designation",
        "CFP",
    ),
    # CPA joined the list after CFA/CFP (Jake, 2026-08-03). It has always had a
    # column and a filter but NO intake-sheet mapping, so nothing ever populated
    # it — a CPA typed into an "Other" blank landed in free text and stayed
    # invisible to the CPA filter.
    _Field(
        "program.cpa_designation",
        "CPA designation",
        "engagement",
        "cpa_designation",
        "designation",
        "CPA",
    ),
    # Everything else the alum holds stays free text (the survey collects it in
    # three "Other" blanks and joins them with ", ").
    #
    # The cap matters more here than anywhere else on the whitelist and is the
    # reason `max_length` exists (#426): this is the one column that is `text`
    # rather than a varchar, so the DATABASE imposes no ceiling, and it carries a
    # trigram GIN index that every alumni search reads. A multi-megabyte answer
    # was previously accepted, stored and indexed. 10000 mirrors the staff cap
    # (`_OTHER_DESIGNATIONS_MAX` in schemas/alumni.py), which is already generous
    # for "Series 7, Series 63".
    _Field(
        "profile.other_designations",
        "Other designations",
        "alumni",
        "other_designations",
        "text",
        max_length=10000,
        validate=_valid_survey_text,
    ),
    # Personal & family (alumni table). Columns already exist — no migration.
    #
    # NAME BLOCK (#646). The four name columns on `alumni`, at the head of the
    # personal group so the confirm page reads as one "Name" question rather than
    # four fields scattered through the form. Until #646 an alum could not correct
    # their own name at all — a marriage rename had to go to staff.
    #
    # "Middle or Maiden name" is the LABEL on purpose, and it is a product call,
    # not a guess: staff have been entering maiden names in `middle_name` for
    # years, so the label is being changed to match the data rather than the data
    # migrated to match a label.
    #
    # `alumni.birth_name` STAYS IN THE SCHEMA AND STAYS UNUSED. It is the column
    # you would expect a maiden name to live in, which is exactly why this note is
    # here — the next person to read this will go looking for it. It is not
    # surveyed, not written, not repurposed and not dropped: the real maiden names
    # are in `middle_name`, and pointing the survey at `birth_name` instead would
    # split one fact across two columns and leave every existing record's maiden
    # name invisible to it.
    #
    # `blankable=False` on all four: `survey_email.get_respondent` pre-fills every
    # name box, so a blank one means the box was cleared or never rendered — never
    # "this alumnus has no first name". Anything holding a survey link can POST to
    # this whitelist, and NULLing an identity column that search, the duplicate
    # check and every export key off is not an edit a public form should be able to
    # stage. Staff can still clear a name from the profile editor.
    #
    # `validate=_valid_name` on all four (#426): until now the ONE rule the staff
    # create/edit path applies to a name — no control characters, no `;=<>|`, no
    # leading formula character — did not run on the only path the public can
    # reach, so `=HYPERLINK("http://evil","click")` wrote cleanly into
    # `first_name`. Same rule, restated where the public writes.
    _Field(
        "profile.first_name",
        "First name",
        "alumni",
        "first_name",
        "text",
        max_length=100,
        validate=_valid_name,
        blankable=False,
    ),
    _Field(
        "profile.middle_name",
        "Middle or Maiden name",
        "alumni",
        "middle_name",
        "text",
        max_length=100,
        validate=_valid_name,
        blankable=False,
    ),
    _Field(
        "profile.last_name",
        "Last name",
        "alumni",
        "last_name",
        "text",
        max_length=100,
        validate=_valid_name,
        blankable=False,
    ),
    _Field(
        "profile.preferred_first_name",
        "Preferred first name",
        "alumni",
        "preferred_first_name",
        "text",
        max_length=100,
        validate=_valid_name,
        blankable=False,
    ),
    # Gender is NOT a name — `_valid_free_text`, not `_valid_name`, because the
    # staff path runs only a length check on it and a digits-only rejection has no
    # meaning here.
    _Field(
        "profile.gender",
        "Gender",
        "alumni",
        "gender",
        "text",
        max_length=30,
        validate=_valid_free_text,
    ),
    # Marital status became a fixed four-option choice in #647. It was free text
    # here (and the column still is a plain varchar(50) — see
    # `core.dropdowns.MARITAL_STATUSES` for why the constraint is not on the
    # column), so the `choice` kind is what stops a public submit from putting
    # arbitrary text into it while leaving every off-list value already on file
    # readable, displayable and untouched.
    _Field(
        "profile.marital_status",
        "Marital status",
        "alumni",
        "marital_status",
        "choice",
        options=MARITAL_STATUSES,
        # A blank is not an answer here either: an alumnus whose stored status is
        # off-list ("Separated") sees a dropdown with no matching option, and if
        # leaving it alone wiped the column the survey would destroy exactly the
        # legacy value #647 requires be preserved.
        blankable=False,
    ),
    # Bounds live on the `date` KIND, not here — see `_coerce`. Until #426 the
    # survey accepted 0001-01-01 and 9999-12-31 while staff were held to
    # 1900-and-not-future.
    _Field("profile.birth_date", "Birthday", "alumni", "birth_date", "date"),
    _Field(
        "profile.citizenship",
        "Citizenship",
        "alumni",
        "citizenship",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "profile.home_country",
        "Home country",
        "alumni",
        "home_country",
        "text",
        max_length=100,
        validate=_valid_free_text,
    ),
    _Field(
        "program.mentor_willing",
        "Willing to mentor students",
        "engagement",
        "mentor_willing",
        "bool",
    ),
    _Field(
        "program.women_in_finance_mentor_willing",
        "Willing to mentor for Women in Finance",
        "engagement",
        "women_in_finance_mentor_willing",
        "bool",
    ),
    _Field(
        "program.guest_speaker_willing",
        "Willing to be a guest speaker",
        "engagement",
        "guest_speaker_willing",
        "bool",
    ),
    _Field(
        "program.help_at_event_willing",
        "Willing to help at an event",
        "engagement",
        "help_at_event_willing",
        "bool",
    ),
    _Field(
        "program.nettrek_host_willing",
        "Willing to host a NetTrek visit",
        "engagement",
        "nettrek_host_willing",
        "bool",
    ),
    _Field(
        "program.finance_conference_willing",
        "Willing to take part in the finance conference",
        "engagement",
        "finance_conference_willing",
        "bool",
    ),
    _Field(
        "program.company_event_sponsor_willing",
        "Willing to sponsor a company event",
        "engagement",
        "company_event_sponsor_willing",
        "bool",
    ),
    _Field(
        "program.case_competition_host_willing",
        "Willing to host a case competition",
        "engagement",
        "case_competition_host_willing",
        "bool",
    ),
    _Field("program.piff_donor", "Pay It Forward donor", "engagement", "piff_donor", "bool"),
)
_FIELD_BY_KEY = {f.key: f for f in _FIELDS}

# The `alumni` columns the fuzzy duplicate check keys off (with graduation_year,
# which the survey cannot write). Named here so the apply path and
# `hygiene.detect_duplicates` cannot drift about which columns make a rename a
# rename — middle/preferred names are not part of the dedup identity.
_DEDUP_NAME_COLUMNS = frozenset({"first_name", "last_name"})

# A survey field's `group` -> the audit `field_name` prefix (#45).
#
# TWO NAMESPACES MEET HERE, and they had to be reconciled deliberately or the
# same column would appear in one history stream under two different names.
#
#   * A survey payload KEY (`_Field.key`) is `profile.linkedin_url`,
#     `employment.current_employer`, `program.piff_donor`. That namespace exists
#     for the FORM: it groups the questions into the cards the alum sees, and
#     `profile.` covers the alumni core row.
#   * An audit `field_name` written by the staff edit path is the BARE column for
#     the alumni row itself and `<section>.<column>` for a nested row, where the
#     section is one of `alumni_service.SECTION_KEYS` -- so `linkedin_url`,
#     `career.current_employer`, `engagement.piff_donor`.
#
# The audit convention wins, because the audit table is where the two paths meet:
# staff edits, CSV imports and survey applies all land in one trail that version
# history and a later restore read as ONE stream. Writing the survey's form
# namespace into it would give `employment.current_employer` and
# `career.current_employer` -- the same column of the same table -- and every
# per-field comparison, grouping and restore would silently treat them as two
# different fields. Provenance is what `source` is for; the field name says WHICH
# COLUMN, and there is only one answer to that.
#
# Mapped off `group` (the table) rather than off the key's prefix, since `group`
# is already the thing that decides which row gets the `setattr` just below.
_AUDIT_SECTION_BY_GROUP = {
    # The alumni core row: no prefix, exactly as `update_alumni` writes it.
    "alumni": None,
    "contact": "contact",  # AlumniContactInfo
    # NOT "employment": `update_alumni` calls the CurrentEmployment section
    # "career", and this is that same row.
    "employment": "career",
    "engagement": "engagement",  # AlumniProgramEngagement
}

_TRUE = frozenset({"yes", "true", "1"})

# How much of an UNRECOGNIZED payload key reaches the log, and how many of them.
# See `_log_safe_keys`.
_LOG_KEY_MAX = 60
_LOG_KEYS_MAX = 20


def _log_safe_keys(keys: list[str]) -> str:
    """Unrecognized payload keys, rendered so they cannot forge log lines (#426).

    The nearby comment in `apply_response` — "field KEYS only ever appear in the
    log, never a submitted value" — is true of the whitelist keys and misses the
    case this handles: a key that is NOT on the whitelist is itself submitted
    text, chosen by whoever POSTed, and it was being joined into a `log.warning`
    verbatim. A newline in one forges a second, entirely attacker-authored log
    line, which is how a real entry gets buried or an investigation misled.

    `repr` is what fixes it — it escapes CR, LF, TAB and every other control
    character and quotes the result, so a key can no longer break out of its own
    line. Truncating and capping the count handles the other half: the payload is
    staged JSON with no key-count limit, so a submission carrying thousands of
    junk keys would otherwise write a single enormous log record.
    """
    shown = sorted(keys)[:_LOG_KEYS_MAX]
    rendered = ", ".join(repr(k[:_LOG_KEY_MAX]) for k in shown)
    if len(keys) > _LOG_KEYS_MAX:
        rendered += f", ... ({len(keys) - _LOG_KEYS_MAX} more)"
    return rendered


def _audit_field_name(field: _Field) -> str:
    """The audit `field_name` for *field* -- see `_AUDIT_SECTION_BY_GROUP`.

    `profile.linkedin_url` -> `linkedin_url`;
    `employment.current_employer` -> `career.current_employer`.

    A group with no mapping falls back to prefixing with the group name itself,
    which is wrong-but-recorded rather than crash-mid-apply: adding a group is a
    code change, and losing an alum's approved answer to a KeyError is far worse
    than an oddly-named audit row. A parity test pins every group in `_FIELDS` to
    a real `SECTION_KEYS` section, so the fallback cannot be reached quietly.
    """
    section = _AUDIT_SECTION_BY_GROUP.get(field.group, field.group)
    return field.column if section is None else f"{section}.{field.column}"


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _current(field: _Field, obj: object | None) -> str:
    """The on-file value as a display string ('Yes'/'No' for booleans). A
    designation column is a held/not-held fact to the alum, so its marker string
    reads as 'Yes' too — the reviewer's diff should say what changed, not which
    literal we store."""
    raw = getattr(obj, field.column, None) if obj is not None else None
    # A MISSING side row is not "unknown" for a yes/no question: these columns are
    # NOT NULL with a false/NULL default, so an alum with no engagement row simply
    # holds none of them. Reading that as "" made every such alum's honest "No"
    # show up in the review queue as a change ("" -> "No") the reviewer then
    # "applied" for nothing. Text fields keep returning "" — a blank column and a
    # blank answer really are the same thing there.
    if obj is None and field.kind not in ("bool", "designation"):
        return ""
    if field.kind == "designation":
        # Presence is NOT the question: a column imported as the literal "No" is
        # a stored (truthy) value that must still read as "No" here, or the
        # reviewer's before/after diff would claim they already held it.
        return "Yes" if holds_designation(raw) else "No"
    if field.kind == "bool":
        return "Yes" if raw else "No"
    # `choice` deliberately falls through to the verbatim stored text: a value
    # that is no longer (or never was) one of the options — "Separated" in
    # marital_status — must still READ back exactly as stored, here and on the
    # confirm page. The option list constrains what can be WRITTEN, never what can
    # be displayed (#647).
    return _text(raw)


def _after(field: _Field, raw: object) -> str:
    """The submitted value as a display string, normalized to match `_current`."""
    value = _text(raw)
    if field.kind in ("bool", "designation"):
        return "Yes" if value.lower() in _TRUE else "No"
    if field.kind == "choice":
        # Show the CANONICAL option, not what was typed, so the reviewer's "after"
        # is the string that will actually be stored. Nothing off-list ever reaches
        # here — `_coerce` returns `_IGNORE` for those and both the submit and the
        # diff drop the field before this is called — but resolving it again here
        # keeps the displayed value and the written value derived from one rule.
        return _choice(field, raw) or ""
    return value


def _choice(field: _Field, raw: object) -> str | None:
    """The canonical option matching *raw*, or ``None`` when it isn't one.

    Case-insensitive and whitespace-trimmed: the alum's browser sends whatever the
    form put in the option's value attribute, and a stored value being re-submitted
    unchanged can carry the casing drift already on the record.
    """
    value = _text(raw)
    if not value:
        return None
    folded = value.lower()
    for option in field.options or ():
        if option.lower() == folded:
            return option
    return None


def _coerce(field: _Field, raw: object):
    """The submitted value coerced to the column's Python type for writing.

    Returns the module-level `_IGNORE` sentinel when the server refuses to write
    the value at all: an off-list `choice`, a blank on a non-`blankable` field, a
    `text` value longer than its column or rejected by its `validate` rule, an
    out-of-range year, or an out-of-range birthday. Callers must check for it
    BEFORE writing — `None` means "store NULL" and is a different instruction.

    Every refusal above is the SAME disposition on purpose: the column keeps what
    it already held. Refusing is safe here in a way that rejecting is not, because
    the alum is never told — so the rules are written to err towards accepting an
    odd-looking real answer rather than dropping one.
    """
    value = _text(raw)
    if field.kind == "bool":
        return value.lower() in _TRUE
    if field.kind == "designation":
        # The payload only says whether the box was ticked; WHAT gets stored comes
        # from the field definition, so a public submit can never write anything
        # other than the canonical marker (or NULL) into a filtered column.
        return field.marker if value.lower() in _TRUE else None
    if not value and not field.blankable:
        # A blank on a field that cannot be blanked is not an instruction, it's a
        # missing answer — leave what's on file alone.
        return _IGNORE
    if field.kind == "choice":
        # Same principle as `designation`: the server decides what may be written,
        # not the payload. Anything holding a survey link can POST to this
        # whitelist, so an unrecognized answer is ignored outright rather than
        # stored as free text (which is what this field used to be) or written as
        # NULL (which would destroy a legitimate off-list value already on file).
        return _choice(field, raw) or _IGNORE
    if field.kind == "int":
        # QUEUE POISONING (#426). This was a bare `int()`, which happily builds a
        # 20-digit Python integer out of `"9" * 20`. Nothing here rejected it,
        # nothing at submit rejected it, and the reviewer's diff showed an
        # ordinary-looking number — but the column is a Postgres `int`, so the
        # write raised 22003, the transaction rolled back, and the response was
        # stuck `pending` FOREVER: every future Approve on it 500s. Anyone holding
        # a survey link could do that, repeatedly, to any number of responses.
        #
        # The range is the STAFF rule (`AlumniBase._validate_year`, 1900 .. this
        # year + 10) reached the same way `_valid_linkedin_url` reaches its own —
        # one definition of "a plausible year", not two. It happens to also be far
        # inside int4, so the crash cannot recur.
        #
        # It is on the KIND rather than on the field because `graduate_graduation_year`
        # is the only `int` on the whitelist and a YEAR is the only thing this kind
        # has ever meant. If a non-year integer is ever added, give it its own kind
        # instead of widening this — a range that fits both is a range that fits
        # neither.
        #
        # Disposition: `_IGNORE`, exactly like an off-list `choice`. NOT `None`,
        # which would mean "store NULL" and would let a bad answer wipe a good year
        # off the record. This also unwedges any response already staged with a
        # poisoned value: `_coerce` runs off the STORED payload at apply time, so
        # the field is now skipped and the response applies (or rejects) normally
        # instead of failing on every attempt.
        if not value:
            return None
        try:
            number = int(value)
        except ValueError:
            # Not a number at all ("abc"), or so many digits that CPython refuses
            # to parse it. Long-standing behaviour, unchanged.
            return None
        try:
            AlumniBase._validate_year(number)
        except ValueError:
            return _IGNORE
        return number
    if field.kind == "date":
        # Expect an ISO "YYYY-MM-DD" string (the survey's <input type="date">).
        if not value:
            return None
        try:
            parsed = datetime.date.fromisoformat(value)
        except ValueError:
            return None
        # Bounds, added #426: the survey took 0001-01-01 and 9999-12-31 while the
        # staff path enforced 1900-and-not-future. `birth_date` is the only `date`
        # field on the whitelist and the staff rule for it is
        # `AlumniBase._validate_birth_date`, reused here rather than restated. A
        # future-dated or year-0001 birthday is not a typo we should keep: it
        # lands in age/cohort reporting and in exports. Ignored, not NULLed — the
        # column may already hold a good date.
        #
        # If a non-birthday date field is ever added, it needs its OWN kind; these
        # bounds are not general to dates.
        try:
            AlumniBase._validate_birth_date(parsed)
        except ValueError:
            return _IGNORE
        return parsed
    # `text` is the only kind whose stored value comes VERBATIM from the payload
    # (`bool`, `designation` and `choice` all resolve to something the server
    # chose; `int`/`date` are parsed into typed values), so it is the only kind a
    # `validate` rule can meaningfully guard — and the only one that needs it.
    #
    # A blank skips the rules below on purpose: it is not a hostile value, it is
    # the "clear this column" instruction, already gated by `blankable` above.
    # Running an email or URL rule against "" would turn every legitimate clear
    # into a silently-ignored answer.
    #
    # The width check comes FIRST, and is separate from `validate`, because it is
    # about the column rather than the value's shape — see `_Field.max_length`.
    # Refused rather than truncated: a silently shortened employer name is a wrong
    # answer presented as a right one, and half a value is not what the alum said.
    if value and field.max_length is not None and len(value) > field.max_length:
        return _IGNORE
    if value and field.validate is not None and not field.validate(value):
        # Same disposition as an off-list `choice`, and for the same reason: the
        # server decides what may be written, and refusing is safer than either
        # storing the value or NULLing a column that may hold a good one. The
        # caller counts this in `ignored` and logs the KEY, never the value.
        return _IGNORE
    return value or None


# --------------------------------------------------------------- submit ------


async def _sent_cycle_and_stage(
    session: AsyncSession, alum: Alumni, alumni_id: int
) -> tuple[int | None, int | None]:
    """The campaign cycle + stage to stamp on a new response (#497), or
    ``(None, None)`` if it cannot be determined.

    A thin guard around :func:`survey_email.sent_cycle_and_stage`. The stamp is
    BOOKKEEPING: the alum's answers are the thing that must survive this request,
    and a report the console does not yet draw is not worth losing a submission
    over. So anything this lookup can raise is caught, logged and turned into
    NULL — which is already a first-class value here (see the model), not an
    error state.

    It runs BEFORE the row is built and before any write, so a failure cannot
    leave a half-written response, and it asks one indexed question of a table
    the submit path would otherwise not touch.
    """
    try:
        return await sent_cycle_and_stage(session, alum.graduation_year, alumni_id)
    except Exception:
        # alumni_id only: never the payload, and nothing that identifies the
        # person beyond the id the rest of this module already logs.
        log.warning(
            "survey submit: could not resolve campaign cycle for alumni_id=%s; "
            "storing NULL",
            alumni_id,
            exc_info=True,
        )
        return (None, None)


async def _live_reply(
    session: AsyncSession,
    alumni_id: int,
    statuses: tuple[str, ...],
    *,
    for_update: bool = False,
) -> SurveyResponse | None:
    """The alum's most recent reply that STILL COUNTS, in one of ``statuses`` --
    or ``None``.

    "Still counts" is the project's one definition of a live reply, not a looser
    local one: inside the 365-day re-survey window, and not superseded by an
    engineer reset (#395). It is the same pair of predicates the sender's
    exclusion (:func:`survey_email._replied_recently_exists`) and every console
    tally apply, imported rather than restated -- if this drifted, an alum could
    be "already replied" to the submit path and "never responded" to the sender
    on the same day.

    Used for exactly two questions on the public submit path:

      * *has this alum already replied?* -- the idempotency guard for a repeated
        confirmation (``RESPONDED_STATUSES``);
      * *is there a confirmation to UPGRADE?* -- when real edits arrive after one
        (``(STATUS_CONFIRMED,)``).

    A reset between the confirmation and the edit makes the confirmation stop
    counting, so it is deliberately NOT found here and the edit stages a fresh
    row: the alum was re-asked, and this is a new answer.

    ``limit(1)`` is what makes ``scalar_one_or_none`` safe -- an alum legitimately
    has several rows in the window (a submission, then another after spotting a
    typo), and the NEWEST is the one both questions are about.

    ``for_update`` LOCKS the row it returns, and the caller that is going to
    MUTATE what it finds must pass it. This is the same reasoning, and the same
    fix, as :func:`_get_pending` on the staff review path: without the lock, two
    requests on one token -- a double-click, a mobile retry, or two submissions
    fired before the first response comes back -- both read the row, both decide
    to upgrade it, and the second commit silently overwrites the first alum's
    answers with no error to either caller. That is data loss on the one write
    path with no login and no reviewer ahead of it.

    Under READ COMMITTED the second request blocks on the lock until the first
    commits, and then Postgres RE-CHECKS the locked row against this WHERE. The
    row it was waiting for is no longer ``confirmed`` by then, so it drops out
    and this returns ``None`` -- the caller falls through and stages a SECOND
    row, which is exactly what two concurrent submissions did before
    confirmations existed, and both answers reach the reviewer. That re-check is
    the point of taking the lock here rather than re-reading the status by hand.

    The lock only helps a caller that mutates an EXISTING row. A row-level lock
    cannot stop a concurrent INSERT, so two confirmations racing each other can
    still both be recorded (see :func:`_record_confirmation`); that one is
    cosmetic and deliberately not worth serialising every submit over.
    """
    stmt = (
        select(SurveyResponse)
        .where(
            SurveyResponse.alumni_id == alumni_id,
            SurveyResponse.submitted_at >= _resurvey_cutoff(),
            SurveyResponse.status.in_(statuses),
            response_not_superseded(),
        )
        .order_by(SurveyResponse.submitted_at.desc())
        .limit(1)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def _record_confirmation(
    session: AsyncSession, alum: Alumni, alumni_id: int
) -> SurveySubmitResult:
    """Record "yes, everything is correct" (#755) -- a REPLY that changed nothing.

    Before this, pressing that button was a client-side state change that posted
    NOTHING, so the alumni who answer fastest -- the ones with nothing to correct
    -- were the only ones invisible to every reply tally, kept getting both
    reminders, and stayed on the manual-follow-up call sheet for good.

    The row is an ordinary ``survey_responses`` row with an EMPTY payload and
    ``status='confirmed'``. That status is a reply
    (:data:`survey_email.RESPONDED_STATUSES`) and is not a review outcome, so the
    confirmation counts toward the response rate and stops the reminders while
    never entering the staff review queue with nothing to review. It carries the
    same ``cycle_seq``/``stage`` stamp a field submission does -- read from the
    send log, NEVER derived from a date (#357).

    IDEMPOTENT, and deliberately blind to which kind of reply already exists: if
    the alum has ANY live reply, this records nothing further and reports that
    one. A double-click, a reload, or a stale tab confirming after real edits
    were already submitted must not manufacture a second row -- and must
    ABSOLUTELY not overwrite a pending submission with an empty one, which is the
    one way this could destroy an alum's answers. ``staged`` is still True: a
    reply is on record, which is what the caller asked about.

    NOTHING HERE MUTATES AN EXISTING ROW, which is why this read takes no lock
    (unlike the upgrade branch in :func:`submit_response`). The residual race is
    therefore benign in the only way that matters: two confirmations landing at
    the same instant can both insert, leaving a duplicate row that reads as two
    "Confirmed" entries on the profile's Surveys tab. It cannot lose an answer,
    it cannot double-count anything -- every console figure counts DISTINCT
    ALUMNI -- and the submit limiter bounds it. Serialising every public submit
    behind a per-alumnus lock to tidy that up would cost more than it buys.
    """
    existing = await _live_reply(session, alumni_id, RESPONDED_STATUSES)
    if existing is not None:
        return SurveySubmitResult(
            staged=True,
            change_count=0,
            survey_response_id=existing.survey_response_id,
            confirmed=existing.status == STATUS_CONFIRMED,
        )

    cycle_seq, stage = await _sent_cycle_and_stage(session, alum, alumni_id)
    response = SurveyResponse(
        alumni_id=alumni_id,
        graduation_year=alum.graduation_year,
        payload={},
        status=STATUS_CONFIRMED,
        cycle_seq=cycle_seq,
        stage=stage,
    )
    session.add(response)
    # Capture the identity BEFORE the commit expires the row, exactly as the
    # field-submit path does.
    await session.flush()
    new_id = response.survey_response_id
    await session.commit()
    return SurveySubmitResult(
        staged=True, change_count=0, survey_response_id=new_id, confirmed=True
    )


async def submit_response(
    session: AsyncSession,
    token: str,
    fields: dict[str, str],
    has_photo: bool = False,
    confirmed_only: bool = False,
) -> SurveySubmitResult:
    """Stage an alum's submission (token-gated, public). Keeps only recognized
    fields; nothing is applied to the record here.

    A photo-only submission (empty `fields` but `has_photo=True`) still creates a
    pending response so the page has an id to attach the photo to. Only a true
    no-op (no recognized fields AND no photo) returns early with a null id.

    ``confirmed_only`` (#755) records "yes, everything is correct" -- see
    :func:`_record_confirmation`. It is honoured ONLY for a submission that
    carries nothing else: real content always wins, because a client that sends
    both is describing a submission WITH changes and treating it as a
    confirmation would discard them. It is not a separate endpoint on purpose --
    the confirm press and the edit submit are the same alum answering the same
    survey through the same signed token, so they share the token check, the
    abuse budget (``SURVEY_SUBMIT_LIMITER``) and the payload caps by construction
    rather than through a second copy that can drift.
    """
    alumni_id = verify_survey_token(token)
    if alumni_id is None:
        raise NotFoundError(LINK_DEAD_MESSAGE)
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == alumni_id))
    ).scalar_one_or_none()
    if alum is None or alum.archived:
        raise NotFoundError(LINK_DEAD_MESSAGE)

    # Recognized keys only — and, for the kinds where the server decides what may
    # be written, only values it would actually write. Staging an answer the apply
    # path is going to ignore would put a change in the review queue that silently
    # does nothing when a reviewer approves it, which is the same class of bug as
    # the dropped-key warning in `apply_response`. Rejecting it here is also what
    # makes the four marital-status options a real constraint rather than a UI
    # convention: the endpoint is public (token-gated), so anyone with a link can
    # POST arbitrary text at this whitelist.
    payload = {
        k: _text(v)
        for k, v in (fields or {}).items()
        if k in _FIELD_BY_KEY and _coerce(_FIELD_BY_KEY[k], v) is not _IGNORE
    }
    if not payload and not has_photo:
        if confirmed_only:
            return await _record_confirmation(session, alum, alumni_id)
        return SurveySubmitResult(staged=False, change_count=0)

    cycle_seq, stage = await _sent_cycle_and_stage(session, alum, alumni_id)

    # CONFIRM, THEN CHANGE YOUR MIND (#755). An alum can press "Yes, everything
    # is correct", then "I need to make changes", then submit real edits -- or
    # answer the involvement questions on the page the confirmation leads to,
    # which are ordinary survey fields. They must NEVER be blocked by having
    # confirmed first, and the two acts are ONE reply, so the confirmation is
    # UPGRADED IN PLACE rather than joined by a second row.
    #
    # Upgrading beats the alternatives. A duplicate row would read as two answers
    # on the profile's Surveys tab -- the cycle counts are DISTINCT ALUMNI, so
    # the response rate would stay right while the history looked wrong -- and
    # refusing the edit would lock an alum out of correcting their own record for
    # having pressed the wrong button first, which is unacceptable on a public
    # page they reached from an email.
    #
    # Only a `confirmed` row is ever touched. A live `pending` submission is left
    # alone and a second one is staged beside it, exactly as before: those are
    # two real submissions and a reviewer must see both.
    # LOCKED, because this branch WRITES to the row it finds. Two submissions
    # racing on one token would otherwise both upgrade the same confirmation and
    # the second would silently discard the first alum's answers -- see
    # `_live_reply`, and `_get_pending` for the same fix on the review path. The
    # lock is held to the commit at the end of this function.
    upgrade = await _live_reply(
        session, alumni_id, (STATUS_CONFIRMED,), for_update=True
    )
    if upgrade is not None:
        upgrade.payload = payload
        upgrade.status = STATUS_PENDING
        # Re-stamped to when the FIELDS were submitted, not when the alum first
        # confirmed. The row is now a pending submission, and `submitted_at` is
        # the date the review queue shows and the 365-day window measures. It can
        # only move FORWARD, so it cannot resurrect a reply a reset superseded --
        # and it could not have been one anyway, `_live_reply` excludes those.
        # `func.now()` keeps the stamp Postgres-clocked like the column default;
        # the reset comparison is against an API-stamped `reset_at`, so mixing
        # clocks here is exactly what every existing row already does.
        upgrade.submitted_at = func.now()
        # The stamp records WHICH campaign email prompted the reply, and the
        # confirmation already answered that. Only fill a gap -- never overwrite
        # an observed value with a freshly-resolved one (#497).
        if upgrade.cycle_seq is None:
            upgrade.cycle_seq = cycle_seq
        if upgrade.stage is None:
            upgrade.stage = stage
        # Read the id BEFORE the commit expires the row.
        upgraded_id = upgrade.survey_response_id
        await session.commit()
        return SurveySubmitResult(
            staged=True, change_count=len(payload), survey_response_id=upgraded_id
        )

    response = SurveyResponse(
        alumni_id=alumni_id,
        graduation_year=alum.graduation_year,
        payload=payload,
        status="pending",
        # WHICH campaign asked (#497). Capture only — nothing reads it yet.
        cycle_seq=cycle_seq,
        stage=stage,
    )
    session.add(response)
    # Flush so the identity is assigned; capture it BEFORE commit expires the row,
    # so the public page can attach an optional photo to this exact response.
    await session.flush()
    new_id = response.survey_response_id
    await session.commit()
    return SurveySubmitResult(
        staged=True, change_count=len(payload), survey_response_id=new_id
    )


async def stage_photo(
    session: AsyncSession,
    token: str,
    survey_response_id: int,
    data: bytes,
    content_type: str,
) -> None:
    """Attach an already-validated NEW profile photo to a pending response
    (token-gated, public). The token proves which alum is calling; the response
    must belong to that alum AND still be pending, else it's a 404. The image is
    uploaded to the private headshots bucket under a `survey-pending/<id>` key
    (never an alum's live headshot) and recorded on the row for admin review."""
    alumni_id = verify_survey_token(token)
    if alumni_id is None:
        raise NotFoundError(LINK_DEAD_MESSAGE)
    resp = (
        await session.execute(
            select(SurveyResponse).where(
                SurveyResponse.survey_response_id == survey_response_id
            )
        )
    ).scalar_one_or_none()
    # A foreign response (belongs to another alum) or one already reviewed is
    # indistinguishable from "not found" to the caller — never leak which.
    if resp is None or resp.alumni_id != alumni_id or resp.status != "pending":
        raise NotFoundError("Survey response not found.")
    path = _staged_photo_path(survey_response_id)
    await supabase_storage.upload_object(_HEADSHOT_BUCKET, path, data, content_type)
    resp.staged_photo_path = path
    await session.commit()


# ------------------------------------------------------------- review --------


async def _load_side_rows(session: AsyncSession, ids: list[int]):
    async def by_alum(model):
        rows = (
            (await session.execute(select(model).where(model.alumni_id.in_(ids)))).scalars().all()
        )
        return {r.alumni_id: r for r in rows}

    return (
        await by_alum(AlumniContactInfo),
        await by_alum(CurrentEmployment),
        await by_alum(AlumniProgramEngagement),
    )


async def list_pending(session: AsyncSession, graduation_year: int) -> list[SurveyResponseItem]:
    """Pending responses for a grad year, each with its before/after diff
    (unchanged fields are dropped)."""
    responses = (
        (
            await session.execute(
                select(SurveyResponse)
                .where(
                    SurveyResponse.status == "pending",
                    SurveyResponse.graduation_year == graduation_year,
                )
                .order_by(SurveyResponse.submitted_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not responses:
        return []

    ids = [r.alumni_id for r in responses]
    alumni = {
        a.alumni_id: a
        for a in (await session.execute(select(Alumni).where(Alumni.alumni_id.in_(ids))))
        .scalars()
        .all()
    }
    contacts, jobs, engs = await _load_side_rows(session, ids)

    items: list[SurveyResponseItem] = []
    for r in responses:
        alum = alumni.get(r.alumni_id)
        if alum is None:
            continue
        by_group = {
            "alumni": alum,
            "contact": contacts.get(r.alumni_id),
            "employment": jobs.get(r.alumni_id),
            "engagement": engs.get(r.alumni_id),
        }
        changes: list[SurveyChange] = []
        for key, raw in (r.payload or {}).items():
            field = _FIELD_BY_KEY.get(key)
            if field is None:
                continue
            # A value the apply path will not write is not a change. `submit_response`
            # already drops these, so in practice this only catches rows staged
            # BEFORE the field gained its constraint (#647) — those must not sit in
            # the queue advertising an edit that approving them wouldn't make.
            if _coerce(field, raw) is _IGNORE:
                continue
            before = _current(field, by_group.get(field.group))
            after = _after(field, raw)
            if before == after:
                continue
            changes.append(
                SurveyChange(field_key=key, label=field.label, before=before, after=after)
            )
        name = (
            " ".join(p for p in (alum.first_name, alum.last_name) if p).strip()
            or alum.preferred_first_name
            or "Alum"
        )
        # Mint a short-lived signed URL so the reviewer can preview a submitted
        # photo (the bucket is private). None when no photo was staged.
        photo_preview_url = None
        if r.staged_photo_path:
            photo_preview_url = await supabase_storage.create_signed_url(
                _HEADSHOT_BUCKET, r.staged_photo_path
            )
        items.append(
            SurveyResponseItem(
                survey_response_id=r.survey_response_id,
                alumni_id=r.alumni_id,
                name=name,
                submitted_at=r.submitted_at.isoformat(),
                changes=changes,
                photo_preview_url=photo_preview_url,
            )
        )
    return items


async def _get_pending(session: AsyncSession, response_id: int) -> SurveyResponse:
    """The response, locked, if and only if it is still pending (#421).

    `FOR UPDATE` is what makes the status check above mean anything. Without it
    two reviewers — or one double-click, or a retried request — both read
    `status == 'pending'`, both pass, and then both proceed: one writes the
    staged fields to the alum's record and promotes their photo while the other
    marks the response REJECTED. The record has changed and the audit trail says
    it was refused, which is the worst of both outcomes because nothing looks
    wrong afterwards. `apply_response` also promotes a staged photo and then
    DELETES the staged object, so the loser of the race can additionally 500 on a
    key that no longer exists.

    Taking the lock here rather than re-checking the status just before commit is
    deliberate, and is the same pattern (and the same reasoning) as
    `survey_reset._load_alum`: under READ COMMITTED the second transaction blocks
    on the lock until the first commits, then re-reads the row it was waiting for
    and sees `applied`/`rejected` — so it raises the ordinary "already reviewed"
    error instead of doing half the work. The lock is row-level and scoped to the
    single response, so it never blocks review of anybody else's.

    This only holds because the lock and the commit share one transaction: both
    `apply_response` and `reject_response` call this at the top and commit at the
    end of the SAME session. Do not move the read out of that transaction.
    """
    resp = (
        await session.execute(
            select(SurveyResponse)
            .where(SurveyResponse.survey_response_id == response_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if resp is None:
        raise NotFoundError("Survey response not found.")
    if resp.status != "pending":
        raise InvalidRequestError("This response has already been reviewed.")
    return resp


@dataclass(frozen=True)
class ApplyOutcome:
    """What an apply has to tell the reviewer AFTER the fact.

    Both members describe something the reviewer could not have known before
    clicking, and NEITHER blocks: the write is already committed by the time this
    is returned. Anything that should stop an apply has to stop it before the
    commit, not be reported here.
    """

    # Non-empty when the response RENAMED the alumnus into a collision with a live
    # record (same first + last name and graduation year) — see #646/#627.
    duplicate_warnings: list[dict]
    # True when a photo WAS staged but could not be decoded, so the field changes
    # were applied and the photo was discarded. The reviewer must be shown this:
    # they approved a submission that visibly carried a photo, and their profile
    # still shows the old one.
    photo_dropped: bool


async def apply_response(
    session: AsyncSession, response_id: int, actor_user_id: int | None
) -> ApplyOutcome:
    """Write the staged changes to the alum's record and mark applied.

    Returns an `ApplyOutcome`: the soft duplicate warnings a NAME change raised
    (#646/#627), and whether a staged photo had to be dropped. Neither blocks: the
    response is already applied and committed by the time they are returned,
    exactly as on the staff rename path, because two alumni genuinely can share a
    name and a graduation year and a marriage rename into a real collision is
    sometimes correct. The point is that the person who approved it is told.

    AUDIT (#45). This path writes with a raw `setattr` rather than going through
    `alumni_service.update_alumni` — deliberately, because the whitelist and the
    server-chosen values above it are the load-bearing part of a PUBLIC write
    surface — so it never inherited the field-level capture the staff and import
    paths have. It recorded one summary row saying how many fields moved, and
    nothing about WHICH, or from what. Every survey approval ever made is
    therefore unreconstructable, and that is permanent: old values exist only at
    the instant they are overwritten.

    So capture happens HERE, where the writes happen: one audit row per field that
    ACTUALLY changed, carrying its real old and new value, all sharing one
    `change_set_id` with the summary row so the whole approval reads as a single
    version, and all stamped `source='survey'` so a restore can tell an alum's own
    answer from a staff edit or a spreadsheet correction.

    The old values are PII (an alum's phone, home city, employer, birthday). They
    go to `audit_logs` and nowhere else — nothing below logs a value, only counts
    and (already-safe) field keys.

    EMPLOYMENT ARCHIVING (#446). When this apply moves the alum to a DIFFERENT
    named employer, the outgoing `current_employment` row is copied down into
    `employment_history` first, through the same `archive_current_role` the staff
    edit and CSV import paths use, so a demoted role looks identical whichever
    path produced it. The staff path asks a human ("this is a new role"); nobody
    is present here to ask, and the product owner decided on 2026-08-18 that the
    survey should infer from the employer rather than never archive at all —
    over both "no archiving" and "ask the reviewer at apply time". The accepted
    cost is that a corrected typo or a company rename produces a prior role the
    alum never left; `alumni_service.employer_changed` documents how narrowly the
    comparison is drawn to limit it.
    """
    resp = await _get_pending(session, response_id)
    alum = (
        await session.execute(select(Alumni).where(Alumni.alumni_id == resp.alumni_id))
    ).scalar_one_or_none()
    if alum is None:
        raise NotFoundError("Alum not found.")
    contacts, jobs, engs = await _load_side_rows(session, [resp.alumni_id])
    contact = contacts.get(resp.alumni_id)
    job = jobs.get(resp.alumni_id)
    eng = engs.get(resp.alumni_id)

    # Promote a staged photo (if any) into the alum's real headshot: download the
    # staged copy, re-encode it, upload OUR bytes under the headshot key (net_id,
    # or alumni_id when no net_id), then remove the staged copy so the pending
    # prefix stays clean.
    had_photo = bool(resp.staged_photo_path)
    photo_dropped = False
    if resp.staged_photo_path:
        data = await supabase_storage.download_object(
            _HEADSHOT_BUCKET, resp.staged_photo_path
        )
        # ⚠️ THE AUTHORITATIVE GATE — and the reason normalising at the public
        # upload route is not enough on its own. This is the only code path that
        # can put bytes onto a real profile, and objects staged BEFORE the route
        # started normalising are still sitting in the bucket right now, waiting
        # for a reviewer to click Apply. Re-encoding here means an alum's headshot
        # is always our own JPEG, whatever was staged and whenever it was staged.
        # It is cheap (one decode per approval) and it is the last chance.
        try:
            data = normalise_headshot(data)
        except InvalidRequestError:
            # ⚠️ AN UNREADABLE PHOTO MUST NOT WEDGE THE WHOLE APPROVAL.
            #
            # The reviewer is approving a submission that usually also carries
            # perfectly good field changes. Raising here would leave the response
            # `pending` FOREVER: every retry downloads the same undecodable object
            # and fails the same way, so the only escape would be to REJECT it and
            # throw the alum's answers away. That is exactly the "one bad value
            # makes a response permanently un-approvable" bug this codebase has
            # already had once, and re-introducing it in the name of security
            # would be worse than the hostile photo it was meant to stop.
            #
            # So: apply the fields, discard the photo, and leave the alum's
            # EXISTING headshot alone — a photo we cannot read is not a reason to
            # replace a good one with nothing. The reviewer is told via the
            # returned `photo_dropped` and the audit row, so nobody walks away
            # believing a new photo was attached when it was not.
            #
            # Only `InvalidRequestError` is caught, and only around the decode: a
            # `ServiceError` from storage means the object could not be FETCHED,
            # which is a transient outage that should fail the apply so it can be
            # retried — not a reason to silently bin a real photo.
            photo_dropped = True

        if photo_dropped:
            log.warning(
                "Survey response %s: the staged photo for alumni %s could not be "
                "decoded and was DISCARDED; the field changes were still applied "
                "and the existing headshot is unchanged.",
                response_id,
                alum.alumni_id,
            )
            # Cleanup only — the object is unreadable and must not linger, but a
            # storage failure while deleting it must not take the apply down with
            # it, or the response is un-approvable again by another route.
            with contextlib.suppress(ServiceError):
                await supabase_storage.delete_object(
                    _HEADSHOT_BUCKET, resp.staged_photo_path
                )
        else:
            await supabase_storage.upload_object(
                _HEADSHOT_BUCKET, _headshot_key(alum), data, _PROMOTED_CONTENT_TYPE
            )
            # This alumnus HAS a headshot as of now — the "no photo" report
            # (#775) answers from a cached listing of the bucket, so drop it.
            # Best-effort: it clears this instance only, and the index's TTL
            # remains the real staleness bound.
            headshot_index.reset_cache()
            await supabase_storage.delete_object(
                _HEADSHOT_BUCKET, resp.staged_photo_path
            )
        # Clear the pointer with the object — on BOTH paths. Leaving it set left
        # the row naming a key that no longer exists, so every later reader — the
        # review queue's signed-URL preview, the engineer survey-state screen, the
        # profile's "+ photo" note — was working from a path that resolves to
        # nothing.
        resp.staged_photo_path = None

    # An apply that writes NOTHING used to report success: a payload key missing
    # from `_FIELDS` was skipped silently, no log, no error, and the response
    # still flipped to "applied". Any future rename on either side of the wire
    # would therefore lose alumni answers invisibly, so count what was written
    # and what was dropped, warn on the drops, and put both in the audit row.
    # Field KEYS only ever appear in the log — never a submitted value. Note that
    # an UNRECOGNIZED key is itself submitter-chosen text, so `dropped` goes
    # through `_log_safe_keys` before it reaches the log (#426); `ignored` holds
    # whitelist keys, which are ours.
    written = 0
    dropped: list[str] = []
    # Values the server refused to write (an off-list `choice`, a blank on a
    # non-blankable name). Counted apart from `dropped`, which means "we don't know
    # this key at all": an ignored value is a KNOWN field whose answer we declined,
    # and the column keeps whatever it already held.
    ignored: list[str] = []
    # True when this apply moves first_name or last_name — the only two columns the
    # fuzzy duplicate check keys off (with graduation_year). Nothing else the survey
    # can write affects it, so the extra query below runs on renames only.
    name_changed = False
    # (audit field_name, old, new) for every field this apply ACTUALLY moved (#45).
    # Collected during the loop because the old value exists only until the
    # `setattr` on the next line overwrites it, and emitted as audit rows at the
    # bottom so the summary row stays the FIRST row of the change set.
    #
    # "Actually moved" is `alumni_service._unchanged` — imported, not restated,
    # so the survey path and the staff path agree on what counts as a change.
    # It matters here for the same reason it matters there: a survey re-confirms
    # the whole form, so most applies resubmit values identical to what is on
    # file, and a naive `!=` would fill the trail with "" -> None non-changes and
    # bury the two fields the alum really corrected.
    field_changes: list[tuple[str, object, object]] = []
    # Side rows this apply CREATED (an alum who had no contact / employment /
    # engagement row). On one of those every column reads back None, so the
    # brand-new-row rule from `_upsert_section` applies: record the fields that
    # carry something, skip the ones that are blank or False. "Nothing became No"
    # is not history, it is noise that would bury the real answers.
    created_rows: set[str] = set()
    # #446 — the outgoing current role, snapshotted BEFORE the write loop, which
    # overwrites `job` in place. `job is None` (an alum with no employment row
    # yet) means there is nothing to demote.
    #
    # The survey has NO checkbox and, by the product owner's decision of
    # 2026-08-18, does not get one: an alum re-confirming their details cannot be
    # asked "is this a new role?", and asking the reviewer instead was considered
    # and rejected. So this path archives when the EMPLOYER moves — see
    # `alumni_service.employer_changed` for exactly what counts and for the
    # tradeoff that was accepted to get it.
    outgoing_role = career_snapshot(job) if job is not None else None

    def _capture(field: _Field, old: object, new: object) -> None:
        if _unchanged(old, new):
            return
        if field.group in created_rows and new is False:
            return
        field_changes.append((_audit_field_name(field), old, new))

    for key, raw in (resp.payload or {}).items():
        field = _FIELD_BY_KEY.get(key)
        if field is None:
            dropped.append(key)
            continue
        value = _coerce(field, raw)
        if value is _IGNORE:
            ignored.append(key)
            continue
        written += 1
        # The old value is read immediately before each `setattr`, which is the
        # only moment it exists. Nothing about the WRITE changes below.
        if field.group == "alumni":
            old = getattr(alum, field.column, None)
            if field.column in _DEDUP_NAME_COLUMNS and old != value:
                name_changed = True
            setattr(alum, field.column, value)
            _capture(field, old, value)
        elif field.group == "contact":
            if contact is None:
                contact = AlumniContactInfo(alumni_id=alum.alumni_id)
                session.add(contact)
                created_rows.add("contact")
            old = getattr(contact, field.column, None)
            setattr(contact, field.column, value)
            _capture(field, old, value)
        elif field.group == "employment":
            if job is None:
                job = CurrentEmployment(alumni_id=alum.alumni_id)
                session.add(job)
                created_rows.add("employment")
            old = getattr(job, field.column, None)
            setattr(job, field.column, value)
            _capture(field, old, value)
        elif field.group == "engagement":
            if eng is None:
                eng = AlumniProgramEngagement(alumni_id=alum.alumni_id)
                session.add(eng)
                created_rows.add("engagement")
            old = getattr(eng, field.column, None)
            setattr(eng, field.column, value)
            _capture(field, old, value)

    # #446 — demote the outgoing role once the loop has written the new one, so
    # the comparison is against what this apply actually stored. Done here rather
    # than inside the loop because the employer and the rest of the role arrive as
    # separate payload keys in no guaranteed order: archiving mid-loop would
    # snapshot a half-written row.
    archived_role = None
    if outgoing_role is not None and employer_changed(
        outgoing_role.get("current_employer"),
        getattr(job, "current_employer", None),
    ):
        archived_role = await archive_current_role(
            session, alum.alumni_id, outgoing_role
        )

    if dropped:
        log.warning(
            "Survey response %s: %d submitted field key(s) are not in the apply "
            "whitelist and wrote NOTHING to alumni %s: %s",
            response_id,
            len(dropped),
            alum.alumni_id,
            _log_safe_keys(dropped),
        )
    if ignored:
        log.info(
            "Survey response %s: %d submitted value(s) were not writable and left "
            "alumni %s unchanged: %s",
            response_id,
            len(ignored),
            alum.alumni_id,
            ", ".join(sorted(ignored)),
        )

    # A rename can collide with an existing alumnus, and until now the survey was
    # the one write path where that happened in total silence — #627 fixed the
    # staff create/edit path and the survey did not exist on it. Same check, same
    # rule, reached by handing `detect_duplicates` the identity this apply
    # produces (the values are already on `alum` in memory).
    #
    # ONLY first/last/graduation_year are passed, and the `blockers` half is
    # deliberately discarded rather than raised. The survey cannot write byu_id or
    # net_id — they aren't in `_FIELDS` — so the exact-collision branches have
    # nothing to fire on that this apply caused; passing the stored ids anyway
    # would let a pre-existing data problem surface here as if this approval had
    # created it, and could turn an unrelated archived-ghost warning into noise on
    # every name change.
    duplicate_warnings: list[dict] = []
    if name_changed:
        _blockers, duplicate_warnings = await hygiene.detect_duplicates(
            session,
            {
                "first_name": alum.first_name,
                "last_name": alum.last_name,
                "graduation_year": alum.graduation_year,
            },
            exclude_alumni_id=alum.alumni_id,
        )

    resp.status = "applied"
    resp.reviewed_by_user_id = actor_user_id
    resp.reviewed_at = datetime.datetime.now(datetime.UTC)

    # One approval = one version (#45), shared by the summary row and every field
    # row below, so the whole apply reads as a single change set. Minted
    # unconditionally rather than only when something changed, because the summary
    # row is written either way and an ungrouped summary row could not be tied to
    # the field rows that belong with it.
    change_set_id = new_change_set_id()
    # `source` reaches the rows through the request-scoped contextvar, the same
    # mechanism `import_csv.commit_update` uses, rather than a literal typed in at
    # each `AuditLog(...)`: the value is registered once in `app.core.audit_context`
    # and anything else this block ends up writing is labelled with it too.
    with audit_source_scope(AUDIT_SOURCE_SURVEY):
        # KEPT, not replaced. The summary row is the only place the counts live —
        # how many keys the payload carried, how many were written, how many were
        # DROPPED (unknown key) and how many IGNORED (known field, refused value).
        # None of that is a field change, so no per-field row can express it, and
        # dropping it would lose the one signal that a rename on either side of
        # the wire is silently eating alumni answers. It now also carries the
        # change set and the source, so it reads as the header of this approval
        # rather than as an unrelated row.
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type="apply_survey_response",
                entity_type="alumni",
                entity_id=alum.alumni_id,
                new_value=(
                    f"survey_response={response_id} fields={len(resp.payload or {})} "
                    f"written={written} dropped={len(dropped)} ignored={len(ignored)}"
                    + (f" duplicate_warnings={len(duplicate_warnings)}" if name_changed else "")
                    # The audit row is the record that outlives the reviewer's
                    # screen: a dropped photo has to be visible here even if
                    # nobody was watching the response body when it happened.
                    + (
                        f" photo={'dropped' if photo_dropped else 'applied'}"
                        if had_photo
                        else ""
                    )
                ),
                change_set_id=change_set_id,
                source=audit_source(),
            )
        )
        # ADDED: the field-level history this path never had. `action_type` is
        # "update" — the same action the staff and import paths record, because
        # this IS an update to the alumni record and version history must find it
        # with one filter; `source` is what says the update came from a survey.
        # Values are stringified exactly as `alumni_service._audit` does it, so
        # rows from the two paths compare directly.
        for field_name, old, new in field_changes:
            session.add(
                AuditLog(
                    user_id=actor_user_id,
                    action_type="update",
                    entity_type="alumni",
                    entity_id=alum.alumni_id,
                    field_name=field_name,
                    old_value=None if old is None else str(old),
                    new_value=None if new is None else str(new),
                    change_set_id=change_set_id,
                    source=audit_source(),
                )
            )
        # #446 — the demotion, inside this scope so it is stamped `survey` like
        # everything else this approval wrote, and sharing the approval's change
        # set so the moved-down role and the new one read as one version.
        if archived_role is not None:
            audit_role_archive(
                session,
                actor_user_id,
                alum.alumni_id,
                outgoing=outgoing_role or {},
                archived=archived_role,
                change_set_id=change_set_id,
            )
    await session.commit()
    return ApplyOutcome(
        duplicate_warnings=duplicate_warnings, photo_dropped=photo_dropped
    )


async def reject_response(
    session: AsyncSession, response_id: int, actor_user_id: int | None
) -> None:
    """Mark a staged response rejected — nothing is written to the record."""
    resp = await _get_pending(session, response_id)
    # Discard any staged photo so rejected uploads don't linger in storage, and
    # clear the pointer with it — a row naming a deleted key is a path every
    # later reader (preview URLs, the engineer state screen) tries to resolve.
    if resp.staged_photo_path:
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, resp.staged_photo_path)
        resp.staged_photo_path = None
    resp.status = "rejected"
    resp.reviewed_by_user_id = actor_user_id
    resp.reviewed_at = datetime.datetime.now(datetime.UTC)
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type="reject_survey_response",
            entity_type="alumni",
            entity_id=resp.alumni_id,
            new_value=f"survey_response={response_id}",
        )
    )
    await session.commit()

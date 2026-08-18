"""Opportunity-link schemas + the URL rule the whole feature is gated on (#441).

The URL validator lives HERE, at module level, rather than being buried in a
Pydantic field validator, for one reason: the survey path must be able to call
exactly the same function. That is the lesson this codebase has already paid for
once — the survey's apply path writes with a raw ``setattr``, so no Pydantic
validator fires, and a rule that existed only on the staff schema was simply
absent on the one path the public actually writes through (the prior
High-severity finding). Here the rule is a plain function; the Pydantic models
below call it, and ``app.services.opportunity_links`` calls it again on every
write regardless of which schema (if any) produced the value.

Mirrors ``app/services/survey_responses._valid_linkedin_url``, which delegates to
the staff rule for the same reason: two copies of "what is an acceptable URL"
drift, and the drift is invisible until someone notices the public path accepting
what the staff path rejects.
"""

from __future__ import annotations

import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.opportunity_link import (
    CITY_MAX,
    COMPANY_NAME_MAX,
    DETAILS_MAX,
    STATE_MAX,
    URL_MAX,
)
from app.schemas.alumni import (
    _NAME_DISALLOWED,
    _has_control_chars,
    _has_invisible_chars_strict,
)

# How many links one PUBLIC submit may create. This is an abuse bound, not a
# product rule: the route is unauthenticated (the signed token is the whole
# credential), so without it a single call is an unbounded row-creation
# primitive. Ten is far above what any real alum types in one sitting, and the
# per-token rate limiter bounds the number of calls on top of it.
MAX_LINKS_PER_SUBMIT = 10

# How many links ONE bulk-delete call may remove. Unlike the submit cap above
# this is not about an unauthenticated writer — the route needs `links.delete`,
# which is super_admin + engineer only — it is about blast radius: a bulk delete
# is the one call in this feature that destroys rows, and an uncapped id list
# turns a single mis-click (or a single stolen super-admin session) into "delete
# the whole table". 100 comfortably covers selecting every row of a full 50-row
# page twice over, and a caller with more to remove pages through it, which is
# also what makes the audit trail land in reviewable batches.
MAX_LINKS_PER_BULK_DELETE = 100

RoleType = Literal["internship", "full_time", "both"]
LinkStatus = Literal["pending", "approved", "rejected"]
LinkSource = Literal["survey", "staff"]

# NOTE: ``RoleType`` above is what FastAPI validates against; ``ROLE_TYPES`` on
# the model is what the DB CHECK mirrors. They must agree, and
# ``tests/test_opportunity_links.py`` pins that they do — a Literal cannot be
# built from a tuple without losing the OpenAPI enum, so the two are kept in step
# by a test rather than by construction.


def validate_opportunity_url(value: str) -> str:
    """The ONE rule for an opportunity URL. Returns the trimmed value or raises
    ``ValueError``.

    ⚠️ WHAT THIS CAN AND CANNOT DEFEND, stated up front because the gap is real.

    The stored value is rendered as a clickable ``href`` on a staff page, so the
    person who follows it is a signed-in staff member with access to the whole
    alumni directory. Two different attacks ride on that:

      1. ``javascript:``/``data:`` — a script the reviewer executes by clicking,
         in their own authenticated session. This IS defeated here: scheme gating
         is a complete defence against it, because the scheme is a closed set.
      2. A perfectly well-formed ``https://`` link to a credential-harvest or
         malware page. This is NOT defeated here, and nothing in this codebase
         defeats it. ``linkedin_url`` is safe from it only because of a hostname
         ALLOW-LIST, and an allow-list cannot exist for this field — the entire
         point is that these links go to arbitrary employer sites.

    So the controls that remain for (2) are: this scheme gate, the length caps,
    and human approval before the link is treated as real. **Human approval does
    not reliably catch a phishing URL.** This project's own ``_valid_linkedin_url``
    docstring says it plainly — "a human eyeballing the queue is not a control
    that catches this" — and it is just as true of a reviewer looking at
    ``https://careers-acme-jobs.example/apply``. That residual risk is accepted
    and OWNED by the moderation step, not eliminated by it. Do not let a later
    reader mistake the approve button for a security control. If this ever grows
    a wider audience than staff, that decision needs its own threat model.

    ⚠️ PARSER DIFFERENTIAL — the backslash and whitespace checks run BEFORE
    parsing, and must stay that way.

    Whatever this function decides, the thing that ultimately follows the link is
    a BROWSER, and Python and browsers do not agree on what the host is. Python's
    ``urlsplit`` is RFC 3986 and treats ``\\`` as an ordinary character; the
    WHATWG URL Standard every browser implements treats it as ``/``. So::

        https://evil.example\\@acme.example/jobs
          urlsplit()  -> host "acme.example"   (what a naive check sees)
          new URL()   -> host "evil.example"   (where the staff member GOES)

    The same class of gap exists for raw tab/CR/LF: WHATWG STRIPS them from a URL
    before parsing, ``urlsplit`` does not, so ``https://evil.example\\t.acme.example``
    reads as two different hosts to the two parsers.

    This field has no hostname allow-list, so neither differential can be used to
    smuggle a "trusted" host past a check — but both are still refused, because a
    URL that means one thing to this process and another to the browser is a
    value we cannot reason about at all, and because the ``details``/company
    fields around it ARE rendered next to the host as context for the reviewer.
    Refusing them outright closes the gap without needing a WHATWG parser here.
    Verified against both parsers on the LinkedIn field, 2026-08-07.
    """
    if not isinstance(value, str):
        raise ValueError("Must be a string.")
    value = value.strip()
    if not value:
        raise ValueError("A link is required.")
    # Length first — the cheapest check, and the one that bounds every check
    # after it. Mirrors the column width, so a value we accept is one the column
    # can hold.
    if len(value) > URL_MAX:
        raise ValueError(f"Must be at most {URL_MAX} characters.")
    # No invisible character belongs in a URL, joiners included: a zero-width
    # character in the host is another way to make a link read as one site while
    # resolving at another. Also covers control characters (category Cc), which
    # is what catches the tab/CR/LF stripping differential described above.
    if _has_invisible_chars_strict(value):
        raise ValueError("Must be an http(s) URL.")
    # Any whitespace at all. A real URL percent-encodes it; a raw space is either
    # a mistake or an attempt to shift where a parser thinks the URL ends.
    if any(ch.isspace() for ch in value):
        raise ValueError("Must be an http(s) URL.")
    # The RFC-3986-vs-WHATWG backslash differential. Checked case-insensitively
    # because %5C and %5c are the same byte once decoded.
    if "\\" in value or "%5c" in value.lower():
        raise ValueError("Must be an http(s) URL.")
    parts = urlsplit(value)
    # `urlsplit` lowercases the scheme, so `JaVaScRiPt:` is caught here too.
    if parts.scheme not in ("http", "https"):
        raise ValueError("Must be an http(s) URL.")
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError("Must be an http(s) URL.")
    # Embedded credentials. `https://acme.example@evil.example/jobs` resolves at
    # evil.example while READING as acme.example to a human scanning the queue —
    # the exact deception this field cannot otherwise defend against. No job
    # posting has a userinfo section, so refusing them costs nothing.
    if parts.username is not None or parts.password is not None:
        raise ValueError("Must be an http(s) URL.")
    # A bare label ("localhost", "intranet") is never an employer's careers page,
    # and a hostname is the only part of this value a reviewer can sanity-check.
    if "." not in host.strip("."):
        raise ValueError("Must be an http(s) URL.")
    return value


def _validate_short_text(value: str, *, field: str, max_length: int) -> str:
    """The free-text rule for company / city / state.

    Built from the same two constants ``AlumniBase._validate_name`` is built from
    — imported, not re-typed — so "which characters are disallowed" keeps one
    definition. It refuses:

      * control and invisible characters (invisible in a diff, different string
        to every exact-match comparison);
      * ``;=<>|`` (meaningful to a SQL/HTML parser, meaningless in a company name);
      * a LEADING ``= + - @``, which turns the cell into a live formula when the
        staff list is exported to CSV — the same defence ``_valid_name`` carries,
        and this table is attacker-supplied text destined for a staff export.

    DISPOSITION DIFFERS FROM THE SURVEY FIELD PATH ON PURPOSE. There, a bad value
    is silently IGNORED so a column keeps the good value already on file. Here
    there is nothing on file — the row does not exist yet — so ignoring a field
    would persist a link with no company or no location. The submission is
    refused instead, and the submitter (an alum at their keyboard, or a staff
    member) gets told.
    """
    value = value.strip()
    if not value:
        raise ValueError(f"{field} cannot be blank.")
    if len(value) > max_length:
        raise ValueError(f"Must be at most {max_length} characters.")
    if _has_control_chars(value):
        raise ValueError(f"{field} contains characters that are not allowed.")
    if _NAME_DISALLOWED & set(value):
        raise ValueError(f"{field} contains characters that are not allowed.")
    if value[0] in "=+-@":
        raise ValueError(f"{field} cannot start with =, +, - or @.")
    return value


def validate_details(value: str) -> str:
    """The rule for the ``details`` blob.

    Deliberately LOOSER on characters than ``_validate_short_text``: this is a
    description of a job, so ``>=`` in a salary line and ``<`` in a date range are
    ordinary English, and refusing them would silently reject real submissions.
    What it keeps is what actually matters for a value nobody indexes and
    everybody renders: control/invisible characters, the CSV formula lead, and a
    hard length cap (the column is ``text``, so the cap IS the storage bound on a
    public write — the same lesson as ``other_designations``).
    """
    value = value.strip()
    if len(value) > DETAILS_MAX:
        raise ValueError(f"Must be at most {DETAILS_MAX} characters.")
    if _has_control_chars(value.replace("\n", "").replace("\r", "")):
        raise ValueError("Details contain characters that are not allowed.")
    if value and value[0] in "=+-@":
        raise ValueError("Details cannot start with =, +, - or @.")
    return value


class OpportunityLinkBase(BaseModel):
    """The fields a submitter (alum or staff) supplies. ``extra='forbid'`` so an
    unknown key is a 422 rather than something silently dropped — the caller
    should know their field did not land."""

    model_config = ConfigDict(extra="forbid")

    is_own_company: bool = False
    company_name: str | None = None
    url: str
    location_city: str | None = None
    location_state: str | None = None
    role_type: RoleType
    application_deadline: datetime.date | None = None
    details: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return validate_opportunity_url(value)

    @field_validator("company_name")
    @classmethod
    def _check_company(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _validate_short_text(
            value, field="Company name", max_length=COMPANY_NAME_MAX
        )

    @field_validator("location_city")
    @classmethod
    def _check_city(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _validate_short_text(value, field="City", max_length=CITY_MAX)

    @field_validator("location_state")
    @classmethod
    def _check_state(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _validate_short_text(value, field="State", max_length=STATE_MAX)

    @field_validator("details")
    @classmethod
    def _check_details(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return validate_details(value)

    @model_validator(mode="after")
    def _company_identity(self) -> OpportunityLinkBase:
        """Exactly one company identity, mirroring the DB CHECK.

        ``is_own_company`` means "look it up from my employment record", so a
        typed name alongside it is ambiguous rather than redundant — refuse it
        instead of silently picking one.
        """
        if self.is_own_company:
            if self.company_name:
                raise ValueError(
                    "Leave the company name blank when the opportunity is at "
                    "your own company."
                )
        elif not self.company_name:
            raise ValueError("A company name is required.")
        return self


class OpportunityLinkCreate(OpportunityLinkBase):
    """Staff manual entry. ``alumni_id`` is explicit — staff are recording an
    opportunity ON BEHALF of an alum, so provenance has to be stated. On the
    survey path it comes from the signed token instead and is never client-supplied."""

    alumni_id: int


class OpportunitySurveyLinkSubmit(OpportunityLinkBase):
    """One link submitted through the PUBLIC survey. Identical field rules to the
    staff shape by inheritance — that sameness is the point, and it is what the
    prior High-severity finding was about. ``alumni_id`` is deliberately absent:
    it comes from the signed token, so a submitter cannot post links onto someone
    else's record."""


class OpportunityLinkUpdate(BaseModel):
    """A staff edit. Every field optional; only what is sent is changed.

    ``model_fields_set`` is what distinguishes "not sent" from "sent as null", so
    clearing a deadline is expressible and omitting it is not a clear.
    """

    model_config = ConfigDict(extra="forbid")

    is_own_company: bool | None = None
    company_name: str | None = None
    url: str | None = None
    location_city: str | None = None
    location_state: str | None = None
    role_type: RoleType | None = None
    application_deadline: datetime.date | None = None
    details: str | None = None

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_opportunity_url(value)

    @field_validator("company_name")
    @classmethod
    def _check_company(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _validate_short_text(
            value, field="Company name", max_length=COMPANY_NAME_MAX
        )

    @field_validator("location_city")
    @classmethod
    def _check_city(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _validate_short_text(value, field="City", max_length=CITY_MAX)

    @field_validator("location_state")
    @classmethod
    def _check_state(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _validate_short_text(value, field="State", max_length=STATE_MAX)

    @field_validator("details")
    @classmethod
    def _check_details(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return validate_details(value)


class OpportunityLinkSubmitRequest(BaseModel):
    """The public survey body: a batch of links from one alum.

    Batched because the survey page collects them together, and capped because
    this is an unauthenticated write — ``_MAX_LINKS_PER_SUBMIT`` bounds how many
    rows one call can create, the same way the field-submit route caps payload
    bytes. The whole batch is refused or accepted together, so a submitter never
    has to guess which of their entries landed.
    """

    model_config = ConfigDict(extra="forbid")

    links: list[OpportunitySurveyLinkSubmit] = Field(
        min_length=1, max_length=MAX_LINKS_PER_SUBMIT
    )


class OpportunityLinkRead(BaseModel):
    """One link as the staff list sees it.

    ``company_name`` here is the RESOLVED display name: the alum's current
    employer when ``is_own_company`` is set (looked up at read time so it follows
    a job change), otherwise the typed name. It can be ``None`` when an alum
    ticked "my company" and has no employer on file — the list shows a dash
    rather than inventing one.
    """

    opportunity_link_id: int
    alumni_id: int
    submitted_by: str | None = None
    is_own_company: bool
    company_name: str | None = None
    url: str
    location_city: str | None = None
    location_state: str | None = None
    role_type: RoleType
    application_deadline: datetime.date | None = None
    details: str | None = None
    status: LinkStatus
    source: LinkSource
    submitted_at: datetime.datetime
    reviewed_by: str | None = None
    reviewed_at: datetime.datetime | None = None


class OpportunityLinkPage(BaseModel):
    """A page of links: the ``{items, total, limit, offset}`` envelope the other
    paginated list endpoints return."""

    items: list[OpportunityLinkRead]
    total: int
    limit: int
    offset: int


class OpportunityLinkBulkDeleteRequest(BaseModel):
    """The ids a staff member multi-selected in the Links tab and asked to delete.

    Capped at :data:`MAX_LINKS_PER_BULK_DELETE`; duplicates in the list are
    collapsed by the service, so sending the same id twice deletes one row and
    reports it once.
    """

    model_config = ConfigDict(extra="forbid")

    opportunity_link_ids: list[int] = Field(
        min_length=1,
        max_length=MAX_LINKS_PER_BULK_DELETE,
        description=(
            "Ids of the links to delete. At most "
            f"{MAX_LINKS_PER_BULK_DELETE} per call."
        ),
    )

    @field_validator("opportunity_link_ids")
    @classmethod
    def _check_ids(cls, value: list[int]) -> list[int]:
        # Mirrors `IdPath`'s ge=1: a zero or negative id can never match a row,
        # so it is a malformed request rather than a miss to report back.
        if any(v < 1 for v in value):
            raise ValueError("Link ids must be positive.")
        return value


class OpportunityLinkBulkDeleteResult(BaseModel):
    """What a bulk delete actually did — per id, not just a count.

    BEST-EFFORT, NOT ALL-OR-NOTHING, and the response is shaped to make that
    safe. The ids come from a list the browser rendered some seconds ago, so an
    id can be stale for the most ordinary reason there is: somebody else already
    deleted it. Failing the whole batch over a row that is already in the state
    the caller asked for would mean the more links you select the more likely the
    button does nothing — and the caller would have to diff the list by hand to
    find out which. So every id that resolves is deleted, in ONE transaction, and
    the ids that did not resolve are named in ``missing_ids`` rather than
    silently folded into a smaller count. Nothing is guessed at and nothing is
    hidden: ``len(deleted_ids) + len(missing_ids) == requested``.
    """

    requested: int
    deleted_ids: list[int]
    missing_ids: list[int]


class OpportunityLinkSubmitResult(BaseModel):
    """Outcome of a public submit — how many links were staged for review.

    Mirrors ``SurveySubmitResult``: the alum is told their entries are pending,
    never that they are live.
    """

    staged: bool
    link_count: int

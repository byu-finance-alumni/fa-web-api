"""Request/response schemas for conference-attendee matching (#612).

Mirror the EXACT dict shapes ``app/services/attendee_match.py`` returns so the
routes can carry a concrete ``response_model`` and stay covered by the OpenAPI
type-contract drift guard. They must not change the response data.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from app.services.attendee_match import (
    MAX_APPROVALS_PER_REQUEST,
    MAX_FRIENDS_PER_REQUEST,
)

_STATUS_MAX = 100
_NOTES_MAX = 10000


# --- Preview -----------------------------------------------------------------


class AttendeeMatchEventEcho(BaseModel):
    """The event the upload is scoped to — there is always an obvious
    "attending what?" answer (Jake, 2026-08-04)."""

    event_id: int
    event_name: str
    event_date: str | None = None


class AttendeeMatchAttendee(BaseModel):
    """The attendee AS THE FILE DESCRIBES THEM, echoed beside the candidates so
    the reviewer compares like with like."""

    name: str
    first_name: str | None = None
    last_name: str | None = None
    maiden_name: str | None = None
    email: str | None = None
    company: str | None = None
    title: str | None = None
    graduation_year: int | None = None


class AttendeeMatchCandidate(BaseModel):
    """One proposed alumnus for one attendee row.

    Carries enough context to DECIDE (name, grad year, employer, title, work
    city/state, net id, emails) plus ``evidence`` — the human-readable reasons
    this record was proposed, including the ones that argue against it (an
    employer that differs is listed too). ``score``/``confidence`` rank
    candidates; they never authorise an automatic write."""

    alumni_id: int
    name: str
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    preferred_first_name: str | None = None
    birth_name: str | None = None
    net_id: str | None = None
    graduation_year: int | None = None
    is_alumni: bool = True
    employer: str | None = None
    title: str | None = None
    city: str | None = None
    state: str | None = None
    personal_email: str | None = None
    work_email: str | None = None
    tier: str
    score: int
    confidence: str
    evidence: list[str] = []
    already_attending: bool = False


class AttendeeMatchRow(BaseModel):
    """One row of the uploaded attendee list and what was proposed for it.

    ``status``:
      * ``matched``    — exactly ONE plausible record. Still a proposal: it is
        written only when a human approves that specific ``alumni_id``.
      * ``ambiguous``  — several plausible records. ALL of them are in
        ``candidates``; the top-scoring one is never silently chosen.
      * ``no_match``   — nothing plausible. Eligible for friend creation.
    ``friend_fields`` lists the DB fields a friend record built from this row
    would carry, so "create a friend" is not a black box."""

    row: int
    status: str
    attendee: AttendeeMatchAttendee
    match_key: str
    candidates: list[AttendeeMatchCandidate] = []
    warnings: list[str] = []
    friend_fields: list[str] = []


class AttendeeMatchSummary(BaseModel):
    total_rows: int
    matched: int
    ambiguous: int
    no_match: int
    already_attending: int


class AttendeeMatchPreview(BaseModel):
    """``POST /events/{event_id}/attendees/match/preview`` — a DRY RUN.

    ``ignored_columns`` are the file's columns that map to no DB field. They are
    dropped, reported, and never an error (Jake, 2026-08-04)."""

    columns_ok: bool
    header_errors: list[str] = []
    ignored_columns: list[str] = []
    event: AttendeeMatchEventEcho | None = None
    summary: AttendeeMatchSummary
    rows: list[AttendeeMatchRow] = []
    warnings: list[dict] = []


# --- Approve -----------------------------------------------------------------


class AttendeeApproval(BaseModel):
    """One human-approved match. ``alumni_id`` is the record the reviewer PICKED
    — for an ambiguous row that is a real choice between candidates, and the
    server re-validates it (exists, not archived) before writing."""

    model_config = ConfigDict(extra="forbid")

    alumni_id: int
    row: int | None = None
    attendance_status: str | None = None
    notes: str | None = None

    @field_validator("attendance_status")
    @classmethod
    def _status_capped(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > _STATUS_MAX:
            raise ValueError(f"must be at most {_STATUS_MAX} characters.")
        return stripped

    @field_validator("notes")
    @classmethod
    def _notes_capped(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        if len(stripped) > _NOTES_MAX:
            raise ValueError(f"must be at most {_NOTES_MAX} characters.")
        return stripped


class AttendeeApprovalRequest(BaseModel):
    """``POST /events/{event_id}/attendees/match/approve`` body.

    There is deliberately no "approve everything above X% confidence" option:
    the client can only send ids a human ticked."""

    model_config = ConfigDict(extra="forbid")

    approvals: list[AttendeeApproval]

    @field_validator("approvals")
    @classmethod
    def _capped(cls, value: list[AttendeeApproval]) -> list[AttendeeApproval]:
        if len(value) > MAX_APPROVALS_PER_REQUEST:
            raise ValueError(
                f"at most {MAX_APPROVALS_PER_REQUEST} approvals per request."
            )
        return value


class AttendeeApplyItem(BaseModel):
    """Per-approval outcome. ``status`` is ``added``, ``already_attending``
    (idempotent no-op — re-running the same file never double-adds), or
    ``not_found`` (unknown or archived alumnus)."""

    alumni_id: int
    row: int | None = None
    status: str
    name: str | None = None
    message: str | None = None


class AttendeeApplyResult(BaseModel):
    """``POST /events/{event_id}/attendees/match/approve`` result."""

    event_id: int
    added: int
    already_attending: int
    not_found: int
    items: list[AttendeeApplyItem] = []


# --- Friend creation ---------------------------------------------------------


class AttendeeFriendItem(BaseModel):
    """Per-row outcome of creating a friend from a no-match row. ``status`` is
    ``created``, ``skipped`` (row not in the file / already had a match) or
    ``rejected`` (the create path refused it — e.g. an exact duplicate)."""

    row: int
    name: str
    status: str
    alumni_id: int | None = None
    message: str | None = None


class AttendeeFriendResult(BaseModel):
    """``POST /events/{event_id}/attendees/match/friends`` result. Every created
    friend is ALSO attached to the event, so the operator never has to make two
    passes."""

    event_id: int
    created: int
    attached: int
    rejected: int
    items: list[AttendeeFriendItem] = []
    header_errors: list[str] = []


MAX_FRIEND_ROWS = MAX_FRIENDS_PER_REQUEST

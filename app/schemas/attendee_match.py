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
    net_id: str | None = None
    company: str | None = None
    title: str | None = None
    graduation_year: int | None = None


class AttendeeMatchCandidate(BaseModel):
    """One proposed alumnus for one attendee row.

    Carries enough context to DECIDE (name, grad year, employer, title, work
    city/state, net id, emails) plus ``evidence`` — the human-readable reasons
    this record was proposed, including the ones that argue against it (an
    employer that differs is listed too). ``score``/``confidence`` rank
    candidates; they never authorise an automatic write.

    ``tier`` is ``netid`` (#537, exact identifier; ``confidence`` is then
    ``certain``), ``email``, ``name`` or ``name_company``. ``corroborated`` is
    only meaningful on a ``netid`` candidate: whether the file's email or name
    ALSO agrees with the record."""

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
    corroborated: bool = False
    evidence: list[str] = []
    already_attending: bool = False


class AttendeeMatchRow(BaseModel):
    """One row of the uploaded attendee list and what was proposed for it.

    ``status``:
      * ``matched``    — exactly ONE plausible record. A proposal (written only
        when a human approves that specific ``alumni_id``) UNLESS
        ``auto_confirmed`` is true: then it is an exact Net ID hit (#537) that
        the client applies through the same ``/approve`` call without a human
        click, sending the row's ``net_id`` so the server re-verifies it.
      * ``ambiguous``  — several plausible records, OR a Net ID that matches one
        record while the email / name matches a different one. ALL of them are
        in ``candidates``; the top-scoring one is never silently chosen.
      * ``no_match``   — nothing plausible on any tier. Eligible for friend
        creation.
      * ``not_reviewed`` — the review hit its aggregate disclosure budget before
        reaching this row. NOT the same as ``no_match``: re-upload the remaining
        rows as a smaller file rather than creating friends for them.
    ``match_key`` is the key that decided the row: ``netid``, ``email`` or
    ``name``. ``reason`` is the one-line Net ID verdict when there is one
    (confirmed / contradicted / unknown Net ID fell through to email + name).
    ``friend_eligible`` is true only when the row failed EVERY tier — never
    merely because it lacks a Net ID (that would duplicate an alum matched on
    email or name). ``friend_fields`` lists the DB fields a friend record built
    from this row would carry, so "create a friend" is not a black box."""

    row: int
    status: str
    attendee: AttendeeMatchAttendee
    match_key: str
    auto_confirmed: bool = False
    reason: str | None = None
    candidates: list[AttendeeMatchCandidate] = []
    warnings: list[str] = []
    friend_eligible: bool = False
    friend_fields: list[str] = []


class AttendeeMatchSummary(BaseModel):
    """``not_reviewed`` counts rows the review deliberately stopped short of:
    one preview may surface at most ``MAX_CANDIDATES_TOTAL`` alumni records, and
    saying "not reviewed" is honest where "no match" would read as "she isn't in
    the database" and invite a duplicate friend record."""

    total_rows: int
    matched: int
    # Subset of ``matched``: rows confirmed by Net ID that need no approval.
    auto_confirmed: int = 0
    ambiguous: int
    no_match: int
    not_reviewed: int = 0
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
    """One match to record. ``alumni_id`` is the record the reviewer PICKED —
    for an ambiguous row that is a real choice between candidates, and the
    server re-validates it (exists, not archived) before writing.

    ``net_id`` is set ONLY for a Net ID row the preview reported
    ``auto_confirmed`` (#537): the server re-verifies that the record's Net ID
    equals it (normalised) before writing and labels the audit entry as a Net
    ID match instead of a human approval; a mismatch is reported
    ``net_id_mismatch`` and nothing is written. It is never a way to approve a
    row that the preview only proposed."""

    model_config = ConfigDict(extra="forbid")

    alumni_id: int
    row: int | None = None
    net_id: str | None = None
    attendance_status: str | None = None
    notes: str | None = None

    @field_validator("net_id")
    @classmethod
    def _net_id_normalised(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip().lower()
        if not stripped:
            return None
        if len(stripped) > 50:
            raise ValueError("must be at most 50 characters.")
        return stripped

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
    the client can only send ids a human ticked, plus the ``auto_confirmed``
    Net ID rows the preview reported (each with its ``net_id``)."""

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
    (idempotent no-op — re-running the same file never double-adds),
    ``not_found`` (unknown or archived alumnus), or ``net_id_mismatch`` (the
    approval carried a ``net_id`` the record does not have — nothing written)."""

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
    net_id_mismatch: int = 0
    items: list[AttendeeApplyItem] = []


# --- Friend creation ---------------------------------------------------------


class AttendeeFriendItem(BaseModel):
    """Per-row outcome of creating a friend from a no-match row. ``status`` is
    one of:

    * ``created`` — a new friend record; ``alumni_id`` + ``friend_id`` are its
      ids.
    * ``reused`` — an EXISTING friend (from any event) matched this row on
      email, or on name + employer when the row has no email (#538), and was
      attached to this event instead of a twin being created; ``alumni_id`` +
      ``friend_id`` name the record so the UI can say "linked existing friend
      FRIEND-00042".
    * ``skipped`` — nothing to do: the person is already on this event's
      roster (a re-post of the same file, or a second row for the same person
      in one file).
    * ``existing_alumnus`` — the row's email belongs to a real ALUMNUS, so no
      friend was created and nothing was attached; ``is_existing_alumnus`` is
      true and ``alumni_id`` is the alumnus. Staff should match the row
      instead.
    * ``rejected`` — the create path refused it (e.g. an exact duplicate).
    """

    row: int
    name: str
    status: str
    alumni_id: int | None = None
    # Visible friend id (``FRIEND-00042``) of the created / reused record; None
    # for every other outcome, including ``existing_alumnus``.
    friend_id: str | None = None
    is_existing_alumnus: bool = False
    message: str | None = None


class AttendeeFriendResult(BaseModel):
    """``POST /events/{event_id}/attendees/match/friends`` result. Every created
    or reused friend is ALSO attached to the event, so the operator never has to
    make two passes (``attached`` = created + reused)."""

    event_id: int
    created: int
    attached: int
    rejected: int
    skipped: int = 0
    # Existing friends linked to this event rather than created again (#538).
    reused: int = 0
    # Rows refused because the email belongs to an alumnus (#538).
    existing_alumni: int = 0
    items: list[AttendeeFriendItem] = []
    header_errors: list[str] = []


MAX_FRIEND_ROWS = MAX_FRIENDS_PER_REQUEST

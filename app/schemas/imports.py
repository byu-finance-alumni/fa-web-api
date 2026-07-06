"""Response schemas for the bulk-import preview/commit routes and the
single-record data-hygiene preview routes.

These mirror the EXACT dict shapes the import/hygiene services already return
(``app/services/import_csv.py``, ``import_events.py``, ``import_donations.py``,
``hygiene.build_preview``) so the routes can carry a concrete ``response_model``
and be covered by the OpenAPI type-contract drift guard (#187). They must not
change the response data.

Heterogeneous per-row issue lists (``warnings`` / ``blockers`` / ``event_errors``)
are typed as free-form ``dict`` objects on purpose: their keys vary by issue
code (a blocker adds ``field``; a warning may omit it), so a strict model would
risk dropping keys. Typing them as objects keeps every key while still giving
the route a concrete top-level response type.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ImportChange(BaseModel):
    """One field the cleaning step normalized: ``before`` -> ``after``."""

    section: str
    field: str
    label: str
    before: Any = None
    after: Any = None


# --- Single-record data-hygiene preview (POST /alumni/preview + /{id}/preview)


class AlumniHygienePreview(BaseModel):
    """``hygiene.build_preview`` output: the cleaned payload, the per-field
    changes, soft warnings, and exact-duplicate blockers."""

    cleaned: dict
    changes: list[ImportChange]
    warnings: list[dict]
    blockers: list[dict]


# --- Alumni bulk CSV import ---------------------------------------------------


class AlumniImportSummary(BaseModel):
    total: int
    importable: int
    rejected: int
    with_warnings: int
    cleaned: int


class AlumniImportRowReport(BaseModel):
    row: int
    name: str | None = None
    status: str
    changes: list[ImportChange] = []
    warnings: list[dict] = []
    blockers: list[dict] = []
    error: str | None = None


class AlumniImportPreview(BaseModel):
    """``POST /alumni/import/preview`` dry-run report."""

    columns_ok: bool
    header_errors: list[str]
    summary: AlumniImportSummary
    rows: list[AlumniImportRowReport]


class ImportReject(BaseModel):
    """One skipped row in a commit result."""

    row: int
    name: str | None = None
    reason: str


class AlumniImportResult(BaseModel):
    """``POST /alumni/import`` commit result."""

    imported: int
    skipped: int
    created_ids: list[int]
    rejects: list[ImportReject]


# --- Events (single-event attendee roster) bulk CSV import -------------------


class EventImportEventMeta(BaseModel):
    """The event identity entered in the wizard (echoed back in the report)."""

    event_name: str | None = None
    event_date: str | None = None
    event_type: str | None = None
    event_location: str | None = None
    event_notes: str | None = None


class EventImportSummary(BaseModel):
    total_rows: int
    attendees_matched: int
    attendees_unmatched: int


class EventImportAttendee(BaseModel):
    row: int
    net_id: str | None = None
    name: str | None = None
    notes: str | None = None
    matched: bool
    alumni_id: int | None = None


class EventImportPreview(BaseModel):
    """``POST /events/import/preview`` dry-run report."""

    columns_ok: bool
    header_errors: list[str]
    event: EventImportEventMeta
    importable: bool
    event_errors: list[dict]
    summary: EventImportSummary
    attendees: list[EventImportAttendee]
    warnings: list[dict]


class EventImportUnmatched(BaseModel):
    row: int
    net_id: str | None = None
    name: str | None = None


class EventImportResult(BaseModel):
    """``POST /events/import`` commit result."""

    imported: bool
    event_id: int | None = None
    imported_attendees: int
    unmatched: list[EventImportUnmatched]
    event_error: str | None = None


# --- Donations bulk CSV import -----------------------------------------------


class DonationImportSummary(BaseModel):
    total: int
    importable: int
    rejected: int


class DonationImportRowReport(BaseModel):
    row: int
    mstid: str | None = None
    name: str | None = None
    match_method: str | None = None
    month: int | None = None
    year: int | None = None
    amount: float | None = None
    alumni_id: int | None = None
    status: str
    blockers: list[dict] = []
    warnings: list[dict] = []


class DonationImportPreview(BaseModel):
    """``POST /donations/import/preview`` dry-run report."""

    columns_ok: bool
    header_errors: list[str]
    summary: DonationImportSummary
    rows: list[DonationImportRowReport]


class DonationImportResult(BaseModel):
    """``POST /donations/import`` commit result."""

    imported: int
    skipped: int
    rejects: list[ImportReject]

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


# --- Alumni bulk UPDATE ("round-trip" edit) ----------------------------------


class AlumniUpdateSummary(BaseModel):
    total: int
    matched: int
    unmatched: int
    with_changes: int
    errors: int
    #: How many rows carry ``overwrites_manual_edit`` (#420) — i.e. would change
    #: a field on a record hand-edited within the last
    #: ``import_csv.MANUAL_EDIT_WARNING_WINDOW_DAYS`` days. Counted here so the
    #: UI can lead with "3 of 2,000 rows overwrite a recent edit" without walking
    #: every row. Defaults to 0 because the header-error early return in the
    #: route builds a summary of zeroes by hand.
    overwrites_manual_edit: int = 0


class AlumniUpdateFieldChange(BaseModel):
    """One field a matched row would change: ``old`` -> ``new``. ``old``/``new``
    are free-form (``Any``) since a cell can hold a string, int, bool, or date."""

    field: str
    section: str = "core"
    old: Any = None
    new: Any = None


class AlumniUpdateManualEditWarning(BaseModel):
    """Set on a preview row that would overwrite a RECENT manual edit (#420).

    A staffer's hand correction stamps ``alumni.manually_edited_at``; a cohort
    file built from an older export would silently revert it, looking like any
    other field change in the preview. This surfaces those rows so the operator
    can check them specifically. It is a WARNING ONLY — the commit still applies
    the row unchanged, there is no block, skip, or per-row override.

    ``edited_by`` follows the profile's "Profile updated by ..." display rule
    rather than a second mechanism of its own, and ``edited_by_source`` says
    which rule produced it:
      * ``user``    — the app user behind ``profile_updated_by_user_id``, shown
        as "First Last" (or their email when the name columns are empty);
      * ``sheet``   — no linked user, so the intake sheet's free-text
        "Profile Updated By" name;
      * ``unknown`` — neither is recorded (an older row, or an edit that came
        from an import). ``edited_by`` is null; say we don't know, don't guess.
    """

    #: ISO-8601 timestamp of the manual edit this row would overwrite.
    manually_edited_at: str
    edited_by: str | None = None
    edited_by_source: str = "unknown"


class AlumniUpdateRowReport(BaseModel):
    """Per-row detail in an update preview.

    ``status`` is one of ``update`` (matched, has changes), ``no_changes``
    (matched, nothing differs), ``unmatched`` (no active match — not created),
    ``unmatched_archived`` (matches only an archived record — not updated), or
    ``error`` (mapping/validation failure). ``message`` explains an unmatched
    row; ``error`` carries a mapping/validation message."""

    row: int
    name: str | None = None
    alumni_id: int | None = None
    status: str
    changes: list[AlumniUpdateFieldChange] = []
    error: str | None = None
    message: str | None = None
    #: Present only on a row that would CHANGE a field on a recently hand-edited
    #: record (#420); null everywhere else, including a matched row whose values
    #: all already agree (it overwrites nothing).
    overwrites_manual_edit: AlumniUpdateManualEditWarning | None = None


class AlumniUpdatePreview(BaseModel):
    """``POST /alumni/import/update/preview`` dry-run report."""

    columns_ok: bool
    header_errors: list[str]
    #: Columns in the uploaded file that don't correspond to any field we can
    #: update. They are skipped (the rest of the row still applies) and listed
    #: here so the preview can say so out loud rather than dropping them
    #: silently.
    ignored_columns: list[str] = []
    summary: AlumniUpdateSummary
    rows: list[AlumniUpdateRowReport]


class AlumniUpdateRowResult(BaseModel):
    """Per-row outcome in an update commit. ``status`` is ``updated``,
    ``unchanged``, ``unmatched``, ``unmatched_archived``, or ``error``."""

    row: int
    name: str | None = None
    alumni_id: int | None = None
    status: str
    message: str | None = None


class AlumniUpdateResult(BaseModel):
    """``POST /alumni/import/update`` commit result."""

    updated: int
    unchanged: int
    unmatched: int
    errors: int
    updated_ids: list[int]
    results: list[AlumniUpdateRowResult]


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


# --- Bulk headshot import ----------------------------------------------------


class HeadshotBulkItem(BaseModel):
    """Per-file outcome in a bulk headshot import (#401).

    ``status`` is one of:
      * ``matched``  — net_id resolved to an alumnus and the image was uploaded;
      * ``no_match`` — no alumnus has that net_id (nothing uploaded);
      * ``invalid``  — bad MIME type, empty file, or over the per-file size cap;
      * ``error``    — storage upload failed (transient / service error).
    ``net_id`` is the value derived from the file name (basename minus extension),
    echoed even when unmatched so the caller can reconcile."""

    filename: str
    net_id: str | None = None
    status: str
    message: str


class HeadshotBulkResult(BaseModel):
    """``POST /alumni/headshots/bulk/confirm`` per-file report + tallies."""

    total: int
    matched: int
    no_match: int
    invalid: int
    errors: int
    items: list[HeadshotBulkItem]


class HeadshotBulkUploadRequest(BaseModel):
    """Filenames the browser wants signed upload URLs for (#595). METADATA ONLY
    — image bytes never travel through the function, which is what broke the old
    multipart route on Vercel's ~4.5 MB request-body cap."""

    filenames: list[str]


class HeadshotBulkUploadTarget(BaseModel):
    """Per-filename outcome of minting bulk upload URLs.

    ``status`` is one of:
      * ``ready``    — the net_id matched an alumnus; PUT the image to
        ``upload_url`` (which is scoped SERVER-SIDE to that alumnus's object
        key — the browser never chooses a key);
      * ``no_match`` — no alumnus has that net_id; nothing to upload;
      * ``invalid``  — the file name has no usable net_id or isn't a
        JPEG/PNG/WebP by extension.
    Only ``ready`` carries an ``upload_url``."""

    filename: str
    net_id: str | None = None
    status: str
    message: str
    upload_url: str | None = None


class HeadshotBulkUploadUrls(BaseModel):
    """``POST /alumni/headshots/bulk/upload-urls`` response."""

    targets: list[HeadshotBulkUploadTarget]


class HeadshotBulkConfirmFile(BaseModel):
    """One file's client-reported upload outcome. ``uploaded`` is the browser's
    claim that its direct PUT succeeded — the server re-derives the net_id,
    re-resolves the alumnus, and re-validates the landed object regardless, so
    this only decides whether an object is worth probing."""

    filename: str
    uploaded: bool = False
    message: str | None = None


class HeadshotBulkConfirmRequest(BaseModel):
    """``POST /alumni/headshots/bulk/confirm`` request: every file in the batch
    with the browser's per-file upload outcome."""

    files: list[HeadshotBulkConfirmFile]

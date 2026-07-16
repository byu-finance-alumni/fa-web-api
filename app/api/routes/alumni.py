"""Alumni CRUD routes.

Reads require view access (any role). Editing an EXISTING alumnus and their
nested records (interactions, employment, education, leadership, tags, status
labels, tasks, event attendance) requires edit access (``student`` and up, via
``RequireAlumniEdit``). Creating a new alumnus, archiving/restoring, and CSV
import require ``full_access`` and up (``RequireFullAccess``) — ``student`` is
deliberately excluded from those. ``DELETE`` on an alumnus is a soft-delete
(archive), never a hard delete — audit history depends on retained records.
"""

import datetime
import io
import posixpath
import zipfile
import zlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    PermissionConfig,
    RequireAlumniEdit,
    RequireFullAccess,
    RequireViewAccess,
)
from app.api.params import IdPath
from app.core.capabilities import Capability, effective_capabilities
from app.core.database import get_session
from app.core.errors import InvalidRequestError, NotFoundError, ServiceError
from app.core.rate_limit import (
    BulkHeadshotRateLimit,
    EmploymentWriteRateLimit,
    HeadshotWriteRateLimit,
    InteractionWriteRateLimit,
    TaskWriteRateLimit,
)
from app.core.security import AuthorizationError
from app.models.alumni import Alumni
from app.repositories.alumni import SURVEY_CADENCE
from app.schemas.alumni import (
    _YEAR_MAX as _GRAD_YEAR_MAX,
)
from app.schemas.alumni import (
    _YEAR_MIN as _GRAD_YEAR_MIN,
)
from app.schemas.alumni import (
    AlumniCreateFull,
    AlumniListItem,
    AlumniLocation,
    AlumniPage,
    AlumniRead,
    AlumniUpdateFull,
    minimize_alumni_read,
)
from app.schemas.alumni_export import AlumniExportRequest, ExportColumnCatalog
from app.schemas.filters import FilterOptions
from app.schemas.imports import (
    AlumniHygienePreview,
    AlumniImportPreview,
    AlumniImportResult,
    AlumniUpdatePreview,
    AlumniUpdateResult,
    HeadshotBulkItem,
    HeadshotBulkResult,
)
from app.schemas.profile import (
    EducationCreate,
    EducationRead,
    EducationUpdate,
    EmploymentHistoryCreate,
    EmploymentHistoryRead,
    EmploymentHistoryUpdate,
    EventAttendanceCreate,
    EventAttendedRead,
    InteractionCreate,
    InteractionRead,
    InteractionUpdate,
    LeadershipCreate,
    LeadershipRead,
    LeadershipUpdate,
    ProfileRead,
    StatusLabelCreate,
    TagCreate,
    TaskCompleteUpdate,
    TaskCreate,
    TaskRead,
)
from app.services import alumni as service
from app.services import (
    alumni_export,
    geo_search,
    hygiene,
    import_csv,
    supabase_storage,
)
from app.services import profile as profile_service

router = APIRouter(prefix="/alumni", tags=["alumni"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Professional designations accepted by the ``designations`` list filter (#404).
_VALID_DESIGNATIONS = ("CFP", "CFA", "CPA")


def _parse_designations(values: list[str] | None) -> list[str]:
    """Normalize the repeatable/CSV ``designations`` query param (#404).

    Splits comma-separated values, upper-cases + de-dupes, and validates every
    token against CFP / CFA / CPA. An unknown value raises a 422 rather than
    being silently dropped, so a typo surfaces instead of returning everyone."""
    if not values:
        return []
    valid = set(_VALID_DESIGNATIONS)
    out: list[str] = []
    for raw in values:
        for piece in raw.split(","):
            token = piece.strip().upper()
            if not token:
                continue
            if token not in valid:
                raise InvalidRequestError(
                    f"Unknown designation '{piece.strip()}'. "
                    "Valid values: CFP, CFA, CPA."
                )
            if token not in out:
                out.append(token)
    return out


@router.get("", response_model=AlumniPage)
async def list_alumni(
    user: RequireViewAccess,
    session: SessionDep,
    q: Annotated[
        str | None,
        Query(description="Search names and external ids (case-insensitive)."),
    ] = None,
    net_id: Annotated[
        str | None,
        Query(description="Net ID — case-insensitive partial match."),
    ] = None,
    first_name: Annotated[
        str | None,
        Query(description="First name — case-insensitive partial match."),
    ] = None,
    last_name: Annotated[
        str | None,
        Query(description="Last name — case-insensitive partial match."),
    ] = None,
    preferred_name: Annotated[
        str | None,
        Query(description="Preferred first name — case-insensitive partial match."),
    ] = None,
    email: Annotated[
        str | None,
        Query(description="Email (personal or work) — case-insensitive partial match."),
    ] = None,
    graduation_year: int | None = None,
    grad_year_min: int | None = None,
    grad_year_max: int | None = None,
    deceased: Annotated[bool | None, Query(description="Filter by deceased flag.")] = None,
    gender: Annotated[
        Literal["M", "F"] | None,
        Query(
            description=(
                "Gender facet (#360): 'M' or 'F'. Combinable with the industry "
                "filter (and every other filter). Matches on the first letter of "
                "the stored gender value, so 'Male'/'M' and 'Female'/'F' both match."
            )
        ),
    ] = None,
    industry_group: Annotated[
        Literal["unknown", "other"] | None,
        Query(
            description=(
                "Industry-bucket facet (#351/#352) for the dashboard drill-downs: "
                "'unknown' — alumni with a blank/missing primary industry; 'other' "
                "— alumni whose primary industry is NOT one of the canonical "
                "finance industries (the 'Other' bucket). Distinct from the exact "
                "'industry' facet, which matches a specific industry name."
            )
        ),
    ] = None,
    employer: Annotated[
        list[str] | None,
        Query(description="Current employer(s) — repeatable (OR), exact match."),
    ] = None,
    past_employer: Annotated[
        list[str] | None,
        Query(description="Prior employer(s) from employment history — repeatable."),
    ] = None,
    industry: Annotated[
        list[str] | None,
        Query(description="Industry / work area (primary or secondary) — repeatable."),
    ] = None,
    title: Annotated[
        list[str] | None,
        Query(description="Current job title(s) — repeatable, exact match."),
    ] = None,
    seniority: Annotated[
        list[str] | None,
        Query(description="Seniority level(s) — repeatable, exact match."),
    ] = None,
    city: Annotated[
        list[str] | None,
        Query(description="Current city/cities — repeatable, exact match."),
    ] = None,
    state: Annotated[
        list[str] | None,
        Query(description="Current state(s) — repeatable, exact match."),
    ] = None,
    tag: Annotated[
        list[str] | None,
        Query(description="Engagement tag(s) — repeatable, exact match."),
    ] = None,
    status_label: Annotated[
        list[str] | None,
        Query(description="Status label(s) — repeatable, exact match."),
    ] = None,
    leadership_role: Annotated[
        list[str] | None,
        Query(description="Finance Society leadership role(s) — repeatable."),
    ] = None,
    survey_status: Annotated[
        list[str] | None,
        Query(description="Survey status value(s) — repeatable, exact match."),
    ] = None,
    needs_survey: Annotated[
        bool,
        Query(
            description=(
                "'Needs surveying' view (admin tier and up only): alumni DUE for "
                "the biennial survey — never completed one, or whose most-recent "
                "completion is older than 2 years. The 2-year threshold is "
                "computed server-side. Forbidden for student / view_only roles "
                "(403)."
            )
        ),
    ] = False,
    contacted_after: Annotated[
        datetime.date | None,
        Query(description="Only alumni with an interaction on/after this date."),
    ] = None,
    contacted_before: Annotated[
        datetime.date | None,
        Query(description="Only alumni NOT contacted since this date (stale)."),
    ] = None,
    never_contacted: Annotated[
        bool, Query(description="Only alumni with no logged interactions.")
    ] = False,
    attended_event: Annotated[
        bool, Query(description="Only alumni who attended at least one event.")
    ] = False,
    spoke_after: Annotated[
        datetime.date | None,
        Query(
            description=(
                "Only alumni who served as a guest speaker at an event held "
                "on/after this date (matches the dashboard 'Guest speakers this "
                "month' KPI)."
            )
        ),
    ] = None,
    spoke_before: Annotated[
        datetime.date | None,
        Query(
            description=(
                "Only alumni who served as a guest speaker at an event held "
                "on/before this date."
            )
        ),
    ] = None,
    donor: Annotated[bool, Query(description="Only PIFF donors.")] = False,
    mentor_willing: Annotated[bool, Query(description="Only alumni willing to mentor.")] = False,
    guest_speaker_willing: Annotated[
        bool, Query(description="Only alumni willing to guest speak.")
    ] = False,
    cfa: Annotated[
        bool, Query(description="Only alumni holding the CFA designation.")
    ] = False,
    cpa: Annotated[
        bool, Query(description="Only alumni holding the CPA designation.")
    ] = False,
    designations: Annotated[
        list[str] | None,
        Query(
            description=(
                "Professional-designation filter (#404): repeatable or "
                "comma-separated, values among CFP, CFA, CPA (case-insensitive). "
                "Returns alumni holding ANY of the requested designations (OR). "
                "An unknown value is a 422."
            )
        ),
    ] = None,
    graduate_degree: Annotated[
        bool, Query(description="Only alumni with a graduate degree recorded.")
    ] = False,
    missing_email: Annotated[
        bool,
        Query(description="Only alumni with no contact-info email on file."),
    ] = False,
    missing_employer: Annotated[
        bool,
        Query(description="Only alumni with no current employer on file."),
    ] = False,
    missing_phone: Annotated[
        bool,
        Query(description="Only alumni with no phone number on file."),
    ] = False,
    duplicate: Annotated[
        bool,
        Query(description="Only alumni flagged as duplicate candidates."),
    ] = False,
    include_archived: bool = False,
    kind: Annotated[
        Literal["alumni", "friend", "all"],
        Query(
            description=(
                "Which records to return (#218): 'alumni' (default) — only "
                "graduates (is_alumni=true); 'friend' — only friends of the "
                "program (is_alumni=false); 'all' — both. Defaults to 'alumni' so "
                "the Alumni page is unchanged."
            )
        ),
    ] = "alumni",
    near: Annotated[
        str | None,
        Query(
            description=(
                "Natural-language location search (#358): a place phrase such as "
                "'near Los Angeles, CA', 'within 50 miles of Provo', or a region "
                "alias like 'Bay Area'. Resolved to a set of nearby cities via the "
                "geocoding module; results are restricted to alumni located there. "
                "An unresolvable phrase falls back to the normal (non-location) "
                "search and the response's 'location.resolved' is false."
            )
        ),
    ] = None,
    radius: Annotated[
        float | None,
        Query(
            ge=1,
            le=3000,
            description=(
                "Optional radius override (miles) for the 'near' location search. "
                "When provided it overrides the radius inferred from the phrase."
            ),
        ),
    ] = None,
    sort: Annotated[
        Literal["name", "grad_desc", "grad_asc", "industry", "city", "state"],
        Query(
            description=(
                "Sort order: name | grad_desc | grad_asc | industry | city | state."
            )
        ),
    ] = "name",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AlumniPage:
    # Archived rows are full_access-and-up only: a view_only / student caller
    # passing ``include_archived=true`` must NOT receive soft-deleted records.
    has_full_access = user.is_full_access or user.is_super_admin or user.is_engineer
    effective_include_archived = include_archived and has_full_access
    # The exact-email filter is a contact-PII enumeration oracle: a low-privilege
    # (view_only / student) caller could confirm an email belongs to a specific
    # alumnus even though no response body exposes that email to them. Gate it to
    # full_access-and-up; below that it's silently ignored (AND'd away like
    # include_archived) rather than 422'd, so a stray param can't leak.
    email = email if has_full_access else None
    # "Needs surveying" is an admin-tier view (engineer / super_admin /
    # full_access = "admin"). student and view_only ("professor") are denied
    # server-side — a 403, not a silent ignore, so the access decision is
    # explicit and audit-visible rather than relying on the UI to hide the tile.
    survey_due_before: datetime.datetime | None = None
    if needs_survey:
        if not has_full_access:
            raise AuthorizationError(
                "The 'needs surveying' view is restricted to admin users."
            )
        survey_due_before = datetime.datetime.now(datetime.UTC) - SURVEY_CADENCE
    # Map the friends/alumni split (#218) to the repository's tri-state filter:
    # alumni-only (True), friends-only (False), or both (None).
    is_alumni_filter = {"alumni": True, "friend": False, "all": None}[kind]
    # Designation facet (#404): validate + normalize CFP/CFA/CPA tokens (422 on
    # an unknown value) before they reach the query builder.
    designation_filter = _parse_designations(designations)
    # Natural-language location search (#358). When a ``near`` phrase is present we
    # ask the geocoding module to resolve it to a center + a set of nearby cities;
    # a ``radius`` override is folded into the phrase so the resolved city set (and
    # the human label) reflect it consistently. A resolved query yields a match-
    # predicate we AND into the list filters; an unresolvable phrase falls back to
    # the normal search, flagged ``resolved=false`` so the UI can show a soft note.
    location_filter = None
    location_envelope: dict | None = None
    if near and near.strip():
        near_text = near.strip()
        # A ``radius`` override is encoded into the phrase so the geo module's
        # resolved city set AND label reflect it. If that phrasing doesn't parse
        # (e.g. an odd region alias), fall back to the raw phrase so a valid place
        # still resolves; the override radius is then only echoed in the envelope.
        resolved = None
        if radius is not None:
            resolved = await geo_search.resolve_location_query(
                session, f"within {radius:g} miles of {near_text}"
            )
        if resolved is None:
            resolved = await geo_search.resolve_location_query(session, near_text)
        if resolved is not None:
            match, keys = resolved
            location_filter = geo_search.alumni_location_filter(keys)
            location_envelope = {
                "label": match.label,
                "radius_miles": (
                    radius if radius is not None else getattr(match, "radius_miles", None)
                ),
                "resolved": True,
            }
        else:
            # Couldn't pinpoint the place — don't filter (normal search still runs)
            # but tell the UI so it can surface a "couldn't pinpoint" note.
            location_envelope = {"label": near_text, "resolved": False}
    items, total = await service.list_alumni(
        session,
        limit=limit,
        offset=offset,
        q=q,
        net_id=net_id,
        first_name=first_name,
        last_name=last_name,
        preferred_name=preferred_name,
        email=email,
        graduation_year=graduation_year,
        grad_year_min=grad_year_min,
        grad_year_max=grad_year_max,
        deceased=deceased,
        gender=gender,
        industry_group=industry_group,
        location_filter=location_filter,
        employer=employer,
        past_employer=past_employer,
        industry=industry,
        title=title,
        seniority=seniority,
        city=city,
        state=state,
        tag=tag,
        status_label=status_label,
        leadership_role=leadership_role,
        survey_status=survey_status,
        needs_survey=needs_survey,
        survey_due_before=survey_due_before,
        contacted_after=contacted_after,
        contacted_before=contacted_before,
        never_contacted=never_contacted,
        attended_event=attended_event,
        spoke_after=spoke_after,
        spoke_before=spoke_before,
        donor=donor,
        mentor_willing=mentor_willing,
        guest_speaker_willing=guest_speaker_willing,
        cfa=cfa,
        cpa=cpa,
        designations=designation_filter,
        graduate_degree=graduate_degree,
        missing_email=missing_email,
        missing_employer=missing_employer,
        missing_phone=missing_phone,
        duplicate=duplicate,
        is_alumni=is_alumni_filter,
        include_archived=effective_include_archived,
        sort=sort,
    )
    # Search/disclosure audit: record the actor + a short filter summary (never
    # the result payload). One lightweight row per search.
    await service.log_search(
        session,
        actor_user_id=user.user_id,
        filters={
            "q": q,
            "net_id": net_id,
            "first_name": first_name,
            "last_name": last_name,
            "preferred_name": preferred_name,
            "email": email,
            "graduation_year": graduation_year,
            "grad_year_min": grad_year_min,
            "grad_year_max": grad_year_max,
            "gender": gender,
            "industry_group": industry_group,
            # Record the interpreted location phrase (never the resolved city set).
            "near": (location_envelope.get("label") if location_envelope else None),
            "employer": "|".join(employer) if employer else None,
            "past_employer": "|".join(past_employer) if past_employer else None,
            "industry": "|".join(industry) if industry else None,
            "title": "|".join(title) if title else None,
            "seniority": "|".join(seniority) if seniority else None,
            "city": "|".join(city) if city else None,
            "state": "|".join(state) if state else None,
            "tag": "|".join(tag) if tag else None,
            "status_label": "|".join(status_label) if status_label else None,
            "leadership_role": "|".join(leadership_role) if leadership_role else None,
            "survey_status": "|".join(survey_status) if survey_status else None,
            "needs_survey": needs_survey or None,
            "contacted_after": contacted_after.isoformat() if contacted_after else None,
            "contacted_before": (contacted_before.isoformat() if contacted_before else None),
            "never_contacted": never_contacted or None,
            "cfa": cfa or None,
            "cpa": cpa or None,
            "designations": "|".join(designation_filter) if designation_filter else None,
            "graduate_degree": graduate_degree or None,
            "spoke_after": spoke_after.isoformat() if spoke_after else None,
            "spoke_before": spoke_before.isoformat() if spoke_before else None,
            # Only record the friends/alumni split when it deviates from the
            # default alumni-only view (keeps the audit summary terse).
            "kind": kind if kind != "alumni" else None,
            "limit": limit,
            "offset": offset,
            "sort": sort,
        },
    )
    rows = [
        minimize_alumni_read(AlumniListItem.model_validate(item), can_edit=user.can_edit_alumni)
        for item in items
    ]
    location = (
        AlumniLocation.model_validate(location_envelope) if location_envelope else None
    )
    return AlumniPage(
        items=rows, total=total, limit=limit, offset=offset, location=location
    )


@router.get("/filter-options", response_model=FilterOptions)
async def alumni_filter_options(_: RequireViewAccess, session: SessionDep) -> FilterOptions:
    """Distinct option lists for the advanced-filter panel's multi-selects.

    Declared before the ``/{alumni_id}`` routes so the literal path always wins
    (``alumni_id`` is int-typed, so a non-numeric segment can't match it anyway).
    """
    return FilterOptions.model_validate(await service.filter_options(session))


# --- Bulk CSV import (full_access) -------------------------------------------
#
# Declared BEFORE the ``/{alumni_id}`` routes so the literal ``/import/...``
# paths win over the ``/{alumni_id}`` / ``/{alumni_id}/preview`` patterns (route
# matching is declaration-ordered).


async def _read_capped(file: UploadFile, cap: int = import_csv.MAX_UPLOAD_BYTES) -> bytes | None:
    """Read an upload, capped at ``cap`` bytes (default ``MAX_UPLOAD_BYTES``).

    Reads one byte past the cap so we can tell "exactly at the limit" from "over".
    Returns the bytes, or ``None`` if the file exceeds the cap (the caller turns
    that into a 413 response). Bounds memory before any parsing happens (DoS).
    """
    data = await file.read(cap + 1)
    if len(data) > cap:
        return None
    return data


def _too_large_response(cap: int = import_csv.MAX_UPLOAD_BYTES) -> JSONResponse:
    mib = cap // (1024 * 1024)
    return JSONResponse(
        status_code=413,  # Content Too Large
        content={
            "error": {
                "code": "payload_too_large",
                "message": (f"File exceeds the {mib} MB upload limit. Split into smaller batches."),
            }
        },
    )


# --- Headshots ---------------------------------------------------------------

_HEADSHOT_BUCKET = "headshots"
# We gate the type here too (the bucket enforces the same allow-list) so a bad
# type never reaches storage.
_HEADSHOT_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
# Headshots get a larger cap than CSV imports — high-res phone photos are easily
# several MB. Independent of ``MAX_UPLOAD_BYTES`` so it doesn't loosen imports.
_HEADSHOT_MAX_BYTES = 20 * 1024 * 1024  # 20 MiB

# Bulk headshot import (#401). Extension -> MIME for images pulled out of a ZIP
# (zip entries carry no content-type of their own). Keys are the canonical
# allow-list; anything else is rejected as ``invalid``.
_HEADSHOT_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# Content types (or a .zip name) that mark the single-file archive path.
_HEADSHOT_ZIP_MIME_TYPES = frozenset(
    {"application/zip", "application/x-zip-compressed", "application/octet-stream"}
)
# Batch caps: bound the number of images and the TOTAL bytes (uncompressed for a
# zip) so one request can't exhaust memory. Per-file cap reuses _HEADSHOT_MAX_BYTES.
_HEADSHOT_BULK_MAX_FILES = 1000
_HEADSHOT_BULK_MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MiB
# Hard ceiling on the number of central-directory records we'll even scan. Junk
# entries (dirs / dotfiles / __MACOSX) are skipped before the file-count cap
# applies, so without this a zip padded with millions of zero-byte members would
# force a multi-million-iteration loop. This bounds that scan up front; it's well
# above _HEADSHOT_BULK_MAX_FILES so a legitimate batch (plus its dir/metadata
# members) never trips it.
_HEADSHOT_BULK_MAX_DIR_ENTRIES = 10_000
# Chunk size for streaming a single zip entry out of the archive. We decompress
# in bounded pieces and track ACTUAL bytes so a lying central-directory size
# can't drive an unbounded read.
_ZIP_READ_CHUNK = 1024 * 1024  # 1 MiB


def _net_id_from_filename(name: str) -> str:
    """Derive the net_id from an image file name: basename minus extension.

    Tolerates both POSIX and Windows separators (zip entries can carry either)
    and trims surrounding whitespace. ``"jdoe12.jpg"`` -> ``"jdoe12"``;
    ``"photos/jdoe12.PNG"`` -> ``"jdoe12"``."""
    base = posixpath.basename(name.replace("\\", "/"))
    stem = posixpath.splitext(base)[0]
    return stem.strip()


def _headshot_mime_for_ext(name: str) -> str | None:
    """Return the allow-listed MIME for an image file name by extension, or None
    when the extension isn't an accepted image type."""
    ext = posixpath.splitext(name.replace("\\", "/"))[1].lower()
    return _HEADSHOT_EXT_MIME.get(ext)


def _sniff_image_mime(data: bytes) -> str | None:
    """Return the image MIME implied by the leading magic bytes of ``data``
    (JPEG / PNG / WebP), or None when the bytes are not a recognised image.

    This NEVER trusts a caller-supplied file extension or ``Content-Type``: those
    are attacker-controlled labels, so a ``.jpg`` name or an ``image/jpeg`` header
    means nothing until the actual bytes are checked here."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_content_error(data: bytes, expected_mime: str) -> str | None:
    """Validate that ``data`` really is a JPEG/PNG/WebP image whose true type
    matches ``expected_mime`` (the type claimed by the extension / Content-Type).
    Return an error message when the content is not an image or contradicts its
    claimed type, or None when it checks out."""
    sniffed = _sniff_image_mime(data)
    if sniffed is None:
        return "File content is not a JPEG, PNG, or WebP image."
    if sniffed != expected_mime:
        return "File content does not match its declared image type."
    return None


def _read_zip_entry_capped(
    zf: zipfile.ZipFile, info: zipfile.ZipInfo, cap: int
) -> bytes | None:
    """Stream-decompress a single zip entry, aborting the instant the ACTUAL
    decompressed size exceeds ``cap``.

    We deliberately never gate on ``info.file_size``: that value comes from the
    archive's central directory and is fully attacker-controlled, so it can lie
    (small, to slip past a size check, or huge, to trigger a giant read). Instead
    we open the entry and read it in bounded chunks, counting real bytes, and bail
    out with None once we cross ``cap``. Returns None (rather than raising) for an
    over-cap entry OR a corrupt/undecompressable stream (e.g. CRC mismatch from a
    forged size), so the caller reports it as one invalid item instead of failing
    the whole request."""
    total = 0
    chunks: list[bytes] = []
    try:
        with zf.open(info) as fh:
            while True:
                chunk = fh.read(_ZIP_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > cap:
                    return None
                chunks.append(chunk)
    except (zipfile.BadZipFile, zlib.error, OSError, EOFError):
        return None
    return b"".join(chunks)


async def _alumnus_net_id(session: AsyncSession, alumni_id: int) -> str:
    """Return the alumnus's net_id (the headshot object key), or raise 404/400."""
    alumnus = await session.scalar(select(Alumni).where(Alumni.alumni_id == alumni_id))
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    net_id = (alumnus.net_id or "").strip()
    if not net_id:
        raise InvalidRequestError(
            "This alumnus has no net ID; a headshot is stored under the net ID."
        )
    return net_id


@router.put("/{alumni_id}/headshot", status_code=204)
async def upload_headshot(
    alumni_id: IdPath,
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> Response:
    """Upload or replace an alumnus's headshot (full_access and up).

    Stored PRIVATELY in the ``headshots`` bucket, keyed by the alumnus's net ID,
    overwriting any existing image. Only JPEG/PNG/WebP within the upload cap are
    accepted; the image is only ever served back via a short-lived signed URL.
    """
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in _HEADSHOT_MIME_TYPES:
        raise InvalidRequestError("Headshot must be a JPEG, PNG, or WebP image.")
    data = await _read_capped(file, _HEADSHOT_MAX_BYTES)
    if data is None:
        return _too_large_response(_HEADSHOT_MAX_BYTES)
    if not data:
        raise InvalidRequestError("The uploaded image is empty.")
    # The Content-Type above is just a client-supplied label; verify the actual
    # bytes really are the image type they claim before anything reaches storage.
    content_error = _image_content_error(data, content_type)
    if content_error is not None:
        raise InvalidRequestError(content_error)
    net_id = await _alumnus_net_id(session, alumni_id)
    await supabase_storage.upload_object(_HEADSHOT_BUCKET, net_id, data, content_type)
    service._audit(session, user.user_id, "upload_headshot", alumni_id, new_value=net_id)
    await session.commit()
    return Response(status_code=204)


@router.post("/{alumni_id}/headshot/upload-url")
async def create_headshot_upload_url(
    alumni_id: IdPath,
    user: HeadshotWriteRateLimit,
    session: SessionDep,
) -> dict:
    """Mint a short-lived signed URL the browser PUTs the image to DIRECTLY
    (full_access and up). This bypasses the ~4.5 MB request-body cap on our
    serverless functions, so headshots up to the bucket limit (20 MB) work.

    The token is scoped to exactly this alumnus's object key (their net ID), so
    the browser can only write that one path and never sees the service key.
    Supabase enforces the bucket's size + JPEG/PNG/WebP allow-list on the PUT.

    We log an ``upload_headshot_started`` audit HERE: minting is the necessary
    precondition for any image change and is fully attributable, so the FERPA
    trail can't be lost if the browser never reaches confirm (dropped connection
    / closed tab). Confirm writes the terminal ``upload_headshot`` (success) or
    ``upload_headshot_rejected`` once the object is validated, so this "started"
    row never masquerades as a completed, conforming upload.
    """
    net_id = await _alumnus_net_id(session, alumni_id)
    upload_url = await supabase_storage.create_signed_upload_url(_HEADSHOT_BUCKET, net_id)
    service._audit(
        session, user.user_id, "upload_headshot_started", alumni_id, new_value=net_id
    )
    await session.commit()
    return {"upload_url": upload_url, "object_key": net_id}


@router.post("/{alumni_id}/headshot/confirm", status_code=204)
async def confirm_headshot_upload(
    alumni_id: IdPath,
    user: HeadshotWriteRateLimit,
    session: SessionDep,
) -> Response:
    """Validate + record the outcome of a direct-to-storage headshot upload
    (full_access and up). Writes the terminal audit so the trail reflects reality:
    ``upload_headshot`` when a conforming object is present, or
    ``upload_headshot_rejected`` when the uploaded object violates the contract
    (the attribution for the attempt is already on the mint's
    ``upload_headshot_started`` row).

    Defense-in-depth: the bucket's own allow-list/size-limit is the primary guard
    on the direct PUT, but we re-check the object's type + size here and delete
    anything outside the contract, so a bucket misconfig can't silently let a bad
    file through. The probe FAILS OPEN — if it can't read the object we fall back
    to a plain existence check rather than reject a legitimate upload."""
    net_id = await _alumnus_net_id(session, alumni_id)
    content_type, size = await supabase_storage.probe_object(_HEADSHOT_BUCKET, net_id)
    if content_type is None and size is None:
        # Couldn't read metadata — confirm the object at least exists so a
        # never-uploaded key can't be "confirmed", then record completion.
        if await supabase_storage.create_signed_url(_HEADSHOT_BUCKET, net_id) is None:
            raise InvalidRequestError("No uploaded image was found to confirm.")
    elif content_type is not None and content_type not in _HEADSHOT_MIME_TYPES:
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, net_id)
        service._audit(
            session,
            user.user_id,
            "upload_headshot_rejected",
            alumni_id,
            field_name="content_type",
            new_value=content_type,
        )
        await session.commit()
        raise InvalidRequestError("Headshot must be a JPEG, PNG, or WebP image.")
    elif size is not None and size > _HEADSHOT_MAX_BYTES:
        await supabase_storage.delete_object(_HEADSHOT_BUCKET, net_id)
        service._audit(
            session,
            user.user_id,
            "upload_headshot_rejected",
            alumni_id,
            field_name="size",
            new_value=size,
        )
        await session.commit()
        return _too_large_response(_HEADSHOT_MAX_BYTES)
    service._audit(session, user.user_id, "upload_headshot", alumni_id, new_value=net_id)
    await session.commit()
    return Response(status_code=204)


@router.get("/{alumni_id}/headshot")
async def get_headshot(
    alumni_id: IdPath,
    user: RequireViewAccess,
    session: SessionDep,
) -> dict:
    """Return a short-lived signed URL for the alumnus's headshot, or
    ``{"url": null}`` when none is set. Any authenticated view role may fetch it
    (the headshot shows on the profile); the bucket is private so the signed URL
    is the only way to view the image and it expires within the hour."""
    alumnus = await session.scalar(select(Alumni).where(Alumni.alumni_id == alumni_id))
    if alumnus is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    net_id = (alumnus.net_id or "").strip()
    if not net_id:
        return {"url": None}
    return {"url": await supabase_storage.create_signed_url(_HEADSHOT_BUCKET, net_id)}


@router.delete("/{alumni_id}/headshot", status_code=204)
async def delete_headshot(
    alumni_id: IdPath,
    user: RequireFullAccess,
    session: SessionDep,
) -> Response:
    """Remove an alumnus's headshot (full_access and up). A missing image is a
    no-op (still 204)."""
    net_id = await _alumnus_net_id(session, alumni_id)
    await supabase_storage.delete_object(_HEADSHOT_BUCKET, net_id)
    service._audit(session, user.user_id, "delete_headshot", alumni_id, old_value=net_id)
    await session.commit()
    return Response(status_code=204)


# One prepared image awaiting a net_id match + upload. ``error`` is set when the
# file failed pre-upload validation (bad MIME / empty / too large), in which case
# ``data`` is None and it is reported as ``invalid`` without a DB lookup.
class _HeadshotEntry:
    __slots__ = ("filename", "net_id", "data", "content_type", "error")

    def __init__(
        self,
        filename: str,
        *,
        net_id: str | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
        error: str | None = None,
    ) -> None:
        self.filename = filename
        self.net_id = net_id
        self.data = data
        self.content_type = content_type
        self.error = error


def _prepare_zip_entries(archive: bytes) -> list[_HeadshotEntry] | JSONResponse:
    """Expand a bulk-import ZIP into per-image entries (validated, capped).

    Skips directories and macOS metadata (``__MACOSX`` / dotfiles). Guards against
    zip bombs WITHOUT ever trusting the archive's self-declared sizes: each entry
    is stream-decompressed in bounded chunks and aborted the instant its ACTUAL
    bytes cross the per-file cap, and the running total of real bytes is held under
    the batch cap. A file over the per-file cap, undecompressable, of a non-image
    extension, or whose real content doesn't match its extension is kept as an
    ``invalid`` entry (so it shows up in the report) rather than silently dropped
    or crashing the request. Returns a 413 response if the archive holds too many
    records / images or its real contents exceed the total cap, or 400 if it isn't
    a readable ZIP."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The uploaded file is not a valid ZIP archive.",
                }
            },
        )
    entries: list[_HeadshotEntry] = []
    total = 0
    with zf:
        # Bound the central-directory scan up front: junk entries are skipped
        # below before the file-count cap applies, so a zip padded with millions
        # of zero-byte members would otherwise force a giant loop. Reject the
        # whole archive if it declares more records than we'll ever scan.
        if len(zf.infolist()) > _HEADSHOT_BULK_MAX_DIR_ENTRIES:
            return _bulk_too_many_response()
        for info in zf.infolist():
            name = info.filename
            base = posixpath.basename(name.replace("\\", "/"))
            if info.is_dir() or not base:
                continue
            if base.startswith(".") or name.startswith("__MACOSX"):
                continue
            if len(entries) >= _HEADSHOT_BULK_MAX_FILES:
                return _bulk_too_many_response()
            net_id = _net_id_from_filename(name)
            content_type = _headshot_mime_for_ext(name)
            if content_type is None:
                entries.append(
                    _HeadshotEntry(
                        base,
                        net_id=net_id or None,
                        error="File must be a JPEG, PNG, or WebP image.",
                    )
                )
                continue
            # Stream the entry with a hard per-file cap on REAL bytes; None means
            # it blew the cap or couldn't be decompressed (e.g. a forged size /
            # bad CRC) — either way it's one invalid item, never a crash.
            data = _read_zip_entry_capped(zf, info, _HEADSHOT_MAX_BYTES)
            if data is None:
                entries.append(
                    _HeadshotEntry(
                        base,
                        net_id=net_id or None,
                        error="Image exceeds the per-file size limit or is unreadable.",
                    )
                )
                continue
            if not data:
                entries.append(
                    _HeadshotEntry(
                        base, net_id=net_id or None, error="The image is empty."
                    )
                )
                continue
            total += len(data)
            if total > _HEADSHOT_BULK_MAX_TOTAL_BYTES:
                return _bulk_too_large_response()
            # Zip entries carry no content-type; the extension is just a label, so
            # verify the real bytes are that image type before trusting them.
            content_error = _image_content_error(data, content_type)
            if content_error is not None:
                entries.append(
                    _HeadshotEntry(base, net_id=net_id or None, error=content_error)
                )
                continue
            entries.append(
                _HeadshotEntry(
                    base, net_id=net_id or None, data=data, content_type=content_type
                )
            )
    return entries


def _bulk_too_large_response() -> JSONResponse:
    mib = _HEADSHOT_BULK_MAX_TOTAL_BYTES // (1024 * 1024)
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": (
                    f"The images exceed the {mib} MB total upload limit. "
                    "Split into smaller batches."
                ),
            }
        },
    )


def _bulk_too_many_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": (
                    f"Too many images in one request (limit "
                    f"{_HEADSHOT_BULK_MAX_FILES}). Split into smaller batches."
                ),
            }
        },
    )


@router.post("/headshots/bulk", response_model=HeadshotBulkResult)
async def bulk_upload_headshots(
    user: BulkHeadshotRateLimit,
    session: SessionDep,
    files: Annotated[
        list[UploadFile],
        File(
            description=(
                "Either a single .zip of images OR multiple image files. Each "
                "image's net_id is its file name minus extension."
            )
        ),
    ],
) -> HeadshotBulkResult | JSONResponse:
    """Bulk-upload alumni headshots (full_access and up, #401).

    Accepts EITHER a single ``.zip`` of images OR several image files in one
    multipart request. For each image the net_id is the file name minus its
    extension; the alumnus is looked up by net_id (case-insensitive) and, when
    matched, the JPEG/PNG/WebP is stored in the private ``headshots`` bucket under
    the net_id key (overwriting any existing image) — the SAME validation, bucket,
    and key as the single-headshot upload. Returns a per-file report; a matched
    upload is audited (``upload_headshot``). Per-file (20 MB) and total (200 MB)
    size caps apply; an oversized batch or bad archive is a 413 / 400."""
    if not files:
        raise InvalidRequestError("No files were uploaded.")

    is_zip = len(files) == 1 and (
        (files[0].filename or "").lower().endswith(".zip")
        or (files[0].content_type or "").split(";")[0].strip().lower()
        in _HEADSHOT_ZIP_MIME_TYPES
    )

    entries: list[_HeadshotEntry]
    if is_zip:
        archive = await _read_capped(files[0], _HEADSHOT_BULK_MAX_TOTAL_BYTES)
        if archive is None:
            return _bulk_too_large_response()
        prepared = _prepare_zip_entries(archive)
        if isinstance(prepared, JSONResponse):
            return prepared
        entries = prepared
    else:
        if len(files) > _HEADSHOT_BULK_MAX_FILES:
            return _bulk_too_many_response()
        entries = []
        total = 0
        for upload in files:
            filename = upload.filename or "(unnamed)"
            net_id = _net_id_from_filename(filename)
            content_type = (upload.content_type or "").split(";")[0].strip().lower()
            if content_type not in _HEADSHOT_MIME_TYPES:
                entries.append(
                    _HeadshotEntry(
                        filename,
                        net_id=net_id or None,
                        error="File must be a JPEG, PNG, or WebP image.",
                    )
                )
                continue
            data = await _read_capped(upload, _HEADSHOT_MAX_BYTES)
            if data is None:
                entries.append(
                    _HeadshotEntry(
                        filename,
                        net_id=net_id or None,
                        error="Image exceeds the per-file size limit.",
                    )
                )
                continue
            if not data:
                entries.append(
                    _HeadshotEntry(
                        filename, net_id=net_id or None, error="The image is empty."
                    )
                )
                continue
            total += len(data)
            if total > _HEADSHOT_BULK_MAX_TOTAL_BYTES:
                return _bulk_too_large_response()
            # The multipart Content-Type is a client-supplied label; verify the
            # real bytes are that image type before trusting/storing them.
            content_error = _image_content_error(data, content_type)
            if content_error is not None:
                entries.append(
                    _HeadshotEntry(
                        filename, net_id=net_id or None, error=content_error
                    )
                )
                continue
            entries.append(
                _HeadshotEntry(
                    filename,
                    net_id=net_id or None,
                    data=data,
                    content_type=content_type,
                )
            )

    # Batch-resolve every candidate net_id to its alumnus in one query (no N+1).
    wanted = {e.net_id.lower() for e in entries if e.data is not None and e.net_id}
    matches: dict[str, Alumni] = {}
    if wanted:
        rows = (
            await session.scalars(
                select(Alumni).where(func.lower(Alumni.net_id).in_(wanted))
            )
        ).all()
        for alumnus in rows:
            key = (alumnus.net_id or "").strip().lower()
            if key:
                matches[key] = alumnus

    items: list[HeadshotBulkItem] = []
    uploaded = 0
    for entry in entries:
        if entry.error is not None:
            items.append(
                HeadshotBulkItem(
                    filename=entry.filename,
                    net_id=entry.net_id,
                    status="invalid",
                    message=entry.error,
                )
            )
            continue
        if not entry.net_id:
            items.append(
                HeadshotBulkItem(
                    filename=entry.filename,
                    net_id=None,
                    status="invalid",
                    message="Could not derive a net ID from the file name.",
                )
            )
            continue
        alumnus = matches.get(entry.net_id.lower())
        if alumnus is None:
            items.append(
                HeadshotBulkItem(
                    filename=entry.filename,
                    net_id=entry.net_id,
                    status="no_match",
                    message="No alumnus has this net ID.",
                )
            )
            continue
        key = (alumnus.net_id or "").strip()
        try:
            await supabase_storage.upload_object(
                _HEADSHOT_BUCKET, key, entry.data, entry.content_type
            )
        except ServiceError:
            items.append(
                HeadshotBulkItem(
                    filename=entry.filename,
                    net_id=entry.net_id,
                    status="error",
                    message="The file storage service rejected the upload.",
                )
            )
            continue
        service._audit(
            session, user.user_id, "upload_headshot", alumnus.alumni_id, new_value=key
        )
        uploaded += 1
        items.append(
            HeadshotBulkItem(
                filename=entry.filename,
                net_id=entry.net_id,
                status="matched",
                message="Headshot uploaded.",
            )
        )

    if uploaded:
        await session.commit()

    return HeadshotBulkResult(
        total=len(items),
        matched=sum(1 for i in items if i.status == "matched"),
        no_match=sum(1 for i in items if i.status == "no_match"),
        invalid=sum(1 for i in items if i.status == "invalid"),
        errors=sum(1 for i in items if i.status == "error"),
        items=items,
    )


@router.get("/import/template")
async def alumni_import_template(
    _: RequireFullAccess,
    kind: Annotated[Literal["alumni", "friend"], Query()] = "alumni",
) -> Response:
    """Download the bulk-import CSV template (full_access). ``kind=alumni`` (the
    default) returns the full Alumni columns; ``kind=friend`` returns the curated
    friend (non-alumni contact) column set (#294). Same column source as the xlsx
    intake template."""
    friend = kind == "friend"
    filename = "friend_import_template.csv" if friend else "alumni_import_template.csv"
    return Response(
        content=import_csv.build_template_csv(friend=friend),
        media_type="text/csv",
        headers={"Content-Disposition": (f'attachment; filename="{filename}"')},
    )


@router.post("/import/preview", response_model=AlumniImportPreview)
async def preview_import_alumni(
    _: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["alumni", "friend"], Query()] = "alumni",
) -> dict | JSONResponse:
    """Dry-run a bulk CSV import (full_access, NO writes).

    Parses + maps the uploaded CSV against the template columns for ``kind``
    (``alumni`` default, or ``friend`` for non-alumni contacts, #294), then
    evaluates every row (clean + duplicate-detect against the DB and earlier
    rows in the file + completeness warnings). Returns the full preview report;
    a bad header set surfaces as ``columns_ok: false`` with ``header_errors``."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS, friend=kind == "friend"
    )
    if header_errors:
        return {
            "columns_ok": False,
            "header_errors": header_errors,
            "summary": {
                "total": 0,
                "importable": 0,
                "rejected": 0,
                "with_warnings": 0,
                "cleaned": 0,
            },
            "rows": [],
        }
    return await import_csv.evaluate(session, rows)


@router.post("/import", response_model=AlumniImportResult)
async def import_alumni(
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["alumni", "friend"], Query()] = "alumni",
) -> dict | JSONResponse:
    """Commit a bulk CSV import (full_access). ``kind=friend`` imports non-alumni
    contacts (``is_alumni=false``, #294); ``kind=alumni`` (default) imports
    alumni. Re-evaluates and inserts every importable row in one transaction
    (audit logging fires per row); rejected rows are skipped and reported. A bad
    header set imports nothing."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS, friend=kind == "friend"
    )
    if header_errors:
        return {
            "imported": 0,
            "skipped": 0,
            "created_ids": [],
            "rejects": [{"row": 0, "name": "(header)", "reason": msg} for msg in header_errors],
        }
    return await import_csv.commit_import(session, rows, actor_user_id=user.user_id)


# --- Bulk UPDATE ("round-trip" edit, full_access) ----------------------------
#
# Staff export a cohort to CSV (the SAME 64-column intake template as the create
# import — ``GET /alumni/import/template``), edit cells, and upload it back to
# mass-UPDATE the existing profiles. Distinct from the CREATE-ONLY import above:
# rows are matched to existing alumni (BYU ID, then Net ID; active only), blank
# cells are left unchanged, and unmatched rows are reported, never created.


@router.post("/import/update/preview", response_model=AlumniUpdatePreview)
async def preview_update_import_alumni(
    _: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Dry-run a bulk UPDATE from a round-trip CSV (full_access, NO writes).

    Parses + maps the uploaded CSV against the alumni template columns, then for
    each row resolves the match (BYU ID -> Net ID, active only) and computes a
    per-field diff against the CURRENT stored values. Returns the structured
    preview; a bad header set surfaces as ``columns_ok: false``."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS
    )
    if header_errors:
        return {
            "columns_ok": False,
            "header_errors": header_errors,
            "summary": {
                "total": 0,
                "matched": 0,
                "unmatched": 0,
                "with_changes": 0,
                "errors": 0,
            },
            "rows": [],
        }
    return await import_csv.evaluate_update(session, rows)


@router.post("/import/update", response_model=AlumniUpdateResult)
async def update_import_alumni(
    user: RequireFullAccess,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Commit a bulk UPDATE from a round-trip CSV (full_access).

    Re-evaluates and applies every matched, changed row in one transaction, each
    through the single-record edit path (so cleaning + provenance + per-field
    audit fire). Blank cells are left unchanged; unmatched rows are reported,
    never created; rows with no effective change are reported ``unchanged``. A bad
    header set updates nothing."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_csv.parse_and_map(
        file_bytes, max_rows=import_csv.MAX_IMPORT_ROWS
    )
    if header_errors:
        return {
            "updated": 0,
            "unchanged": 0,
            "unmatched": 0,
            "errors": len(header_errors),
            "updated_ids": [],
            "results": [
                {
                    "row": 0,
                    "name": "(header)",
                    "alumni_id": None,
                    "status": "error",
                    "message": msg,
                }
                for msg in header_errors
            ],
        }
    return await import_csv.commit_update(session, rows, actor_user_id=user.user_id)


@router.get("/import/update/export", response_model=None)
async def export_cohort_update_template(
    user: RequireFullAccess,
    session: SessionDep,
    grad_year: Annotated[int, Query(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
) -> Response | JSONResponse:
    """Download an ACTIVE graduation-year cohort as a FILLED intake-template CSV
    (full_access).

    Powers the round-trip: pick a grad year, download that cohort in the EXACT
    import-template column format, edit cells offline, then re-upload through
    ``POST /alumni/import/update`` (which matches by BYU ID / Net ID and applies
    only the changed cells). ``grad_year`` is validated to the same year bounds as
    the alumni schema. A cohort larger than the export cap is a 413 asking the
    caller to narrow it down. Audit-logged (``export_alumni``) like the other
    exports."""
    try:
        csv_text = await import_csv.build_cohort_update_csv(
            session, grad_year, actor_user_id=user.user_id
        )
    except import_csv.CohortTooLargeError as exc:
        return JSONResponse(
            status_code=413,
            content={"error": {"code": "payload_too_large", "message": str(exc)}},
        )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f'attachment; filename="alumni_cohort_{grad_year}.csv"'
            )
        },
    )


# --- Customizable CSV export (full_access) -----------------------------------
#
# Declared BEFORE the ``/{alumni_id}`` routes so the literal ``/export/...`` paths
# win over the ``/{alumni_id}`` pattern (route matching is declaration-ordered).


@router.get("/export/columns", response_model=ExportColumnCatalog)
async def alumni_export_columns(_: RequireFullAccess) -> ExportColumnCatalog:
    """The catalog of exportable columns + the default-checked selection, for the
    export column picker (full_access)."""
    return alumni_export.build_catalog()


@router.post("/export", response_model=None)
async def export_alumni(
    payload: AlumniExportRequest,
    user: RequireFullAccess,
    session: SessionDep,
) -> Response | JSONResponse:
    """Export the filtered alumni list as CSV with the chosen columns
    (full_access). Hits the SAME population the list view shows (same filters).
    An unknown column key is a 422; a result set larger than the export cap is a
    413 asking the caller to narrow filters. Audit-logged (``export_alumni``)."""
    try:
        columns = alumni_export.validate_columns(payload.columns)
    except ValueError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": str(exc)}},
        )
    total = await alumni_export.count_matching(session, payload.filters)
    if total > alumni_export.MAX_EXPORT_ROWS:
        return JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "payload_too_large",
                    "message": (
                        f"This export matches {total:,} alumni, over the "
                        f"{alumni_export.MAX_EXPORT_ROWS:,}-row limit. Narrow the "
                        "filters and try again."
                    ),
                }
            },
        )
    csv_text = await alumni_export.export_csv(
        session,
        columns=columns,
        filters=payload.filters,
        actor_user_id=user.user_id,
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="alumni_export.csv"'},
    )


@router.get("/{alumni_id}", response_model=AlumniRead)
async def get_alumni(alumni_id: IdPath, user: RequireViewAccess, session: SessionDep) -> AlumniRead:
    """Single lightweight alumni core record.

    Archived records 404 (they were removed from the directory). view_only
    ("Professor") callers receive a FERPA-minimized record — sensitive PII,
    notes, and import provenance are nulled. This lightweight read is not
    audit-logged (the full profile aggregate is)."""
    alumnus = await service.get_alumni(session, alumni_id)
    return minimize_alumni_read(AlumniRead.model_validate(alumnus), can_edit=user.can_edit_alumni)


@router.get("/{alumni_id}/profile", response_model=ProfileRead)
async def get_alumni_profile(
    alumni_id: IdPath,
    user: RequireViewAccess,
    config: PermissionConfig,
    session: SessionDep,
) -> ProfileRead:
    """Full profile aggregate (core + contact, career, employment, leadership,
    engagement, surveys, interactions, tasks, attachments, Pay It Forward, audit)
    for the tabs.

    Archived records 404. Follow-up tasks are edit-only: view_only ("Professor")
    users get an empty ``tasks`` list AND a FERPA-minimized aggregate (sensitive
    PII nulled, free-text notes and audit trail stripped) — enforced here, not
    just hidden in the UI. Anyone with edit access — engineer / super_admin /
    full_access / student — sees all. The disclosure is audit-logged
    (``view_profile``).

    The ``pay_it_forward`` roll-up (#403) always includes the donation count and
    last-gift date, but its dollar amounts are gated to amount-viewers
    (``alumni.full`` — full_access+), mirroring the donations endpoints."""
    include_tasks = user.can_edit_alumni
    show_amounts = Capability.ALUMNI_FULL in effective_capabilities(config, user.roles)
    return await profile_service.get_profile(
        session,
        alumni_id,
        include_tasks=include_tasks,
        can_edit=user.can_edit_alumni,
        show_pay_it_forward_amounts=show_amounts,
        actor_user_id=user.user_id,
    )


@router.get(
    "/{alumni_id}/export",
    response_model=ProfileRead,
    # The exported body is the full profile aggregate MINUS the embedded audit
    # trail (the service drops it). Exclude it here too so the declared model
    # matches the actual output exactly and the ``audit`` key is never re-added.
    response_model_exclude={"audit"},
)
async def export_alumni_profile(
    alumni_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> dict:
    """Server-side, audited profile export (full_access).

    Returns the full profile aggregate as a MINIMIZED JSON body: the embedded
    ``audit`` trail is excluded and internal user PKs (interaction ``user_id``,
    task ``assigned_to_user_id``) are never present. Writes an ``export_profile``
    audit row before returning. Archived records 404. The frontend calls this
    instead of doing a client-side export."""
    return await profile_service.export_profile(session, alumni_id, actor_user_id=user.user_id)


@router.post(
    "/{alumni_id}/interactions",
    response_model=InteractionRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_interaction(
    alumni_id: IdPath,
    payload: InteractionCreate,
    user: InteractionWriteRateLimit,
    session: SessionDep,
) -> InteractionRead:
    """Log an interaction on an alumni's timeline.

    Open to every authenticated role, including view_only ("Professor"): adding
    an interaction is the one timeline write a professor may perform (#129). The
    row is stamped with the actor's user id so ownership can later gate edit /
    delete for view_only users."""
    return await profile_service.add_interaction(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/interactions/{interaction_id}",
    response_model=InteractionRead,
)
async def update_interaction(
    alumni_id: IdPath,
    interaction_id: IdPath,
    payload: InteractionUpdate,
    user: InteractionWriteRateLimit,
    session: SessionDep,
) -> InteractionRead:
    """Edit an interaction on an alumni's timeline. 404 if the row is missing or
    belongs to another alumnus.

    Edit-tier roles (engineer / super_admin / full_access / student) may edit ANY
    interaction. A view_only ("Professor") user may edit only the interactions
    they logged themselves; editing another user's interaction is 403 (#129)."""
    return await profile_service.update_interaction(
        session,
        alumni_id,
        interaction_id,
        payload,
        actor_user_id=user.user_id,
        can_edit_others=user.can_edit_alumni,
    )


@router.delete(
    "/{alumni_id}/interactions/{interaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_interaction(
    alumni_id: IdPath,
    interaction_id: IdPath,
    user: InteractionWriteRateLimit,
    session: SessionDep,
) -> None:
    """Delete an interaction from an alumni's timeline. 404 if the row is missing
    or belongs to another alumnus.

    Edit-tier roles (engineer / super_admin / full_access / student) may delete
    ANY interaction. A view_only ("Professor") user may delete only the
    interactions they logged themselves; deleting another user's interaction is
    403 (#129)."""
    await profile_service.delete_interaction(
        session,
        alumni_id,
        interaction_id,
        actor_user_id=user.user_id,
        can_edit_others=user.can_edit_alumni,
    )


@router.post(
    "/{alumni_id}/tasks",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_task(
    alumni_id: IdPath,
    payload: TaskCreate,
    user: TaskWriteRateLimit,
    session: SessionDep,
) -> TaskRead:
    """Create a follow-up task for an alumni (full_access)."""
    return await profile_service.add_task(session, alumni_id, payload, actor_user_id=user.user_id)


@router.patch("/{alumni_id}/tasks/{task_id}", response_model=TaskRead)
async def update_task_completion(
    alumni_id: IdPath,
    task_id: IdPath,
    payload: TaskCompleteUpdate,
    user: TaskWriteRateLimit,
    session: SessionDep,
) -> TaskRead:
    """Toggle a follow-up task's completion state (full_access)."""
    return await profile_service.set_task_completed(
        session, alumni_id, task_id, payload.completed, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/employment",
    response_model=EmploymentHistoryRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_employment(
    alumni_id: IdPath,
    payload: EmploymentHistoryCreate,
    user: EmploymentWriteRateLimit,
    session: SessionDep,
) -> EmploymentHistoryRead:
    """Add a prior role to an alumni's employment history (full_access)."""
    return await profile_service.add_employment(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/employment/{employment_history_id}",
    response_model=EmploymentHistoryRead,
)
async def update_employment(
    alumni_id: IdPath,
    employment_history_id: IdPath,
    payload: EmploymentHistoryUpdate,
    user: EmploymentWriteRateLimit,
    session: SessionDep,
) -> EmploymentHistoryRead:
    """Edit a prior role on an alumni's employment history (full_access). 404 if
    the row is missing or belongs to another alumnus."""
    return await profile_service.update_employment(
        session,
        alumni_id,
        employment_history_id,
        payload,
        actor_user_id=user.user_id,
    )


@router.delete(
    "/{alumni_id}/employment/{employment_history_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_employment(
    alumni_id: IdPath,
    employment_history_id: IdPath,
    user: EmploymentWriteRateLimit,
    session: SessionDep,
) -> None:
    """Delete a prior role from an alumni's employment history (full_access). 404
    if the row is missing or belongs to another alumnus."""
    await profile_service.delete_employment(
        session, alumni_id, employment_history_id, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/education",
    response_model=EducationRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_education(
    alumni_id: IdPath,
    payload: EducationCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> EducationRead:
    """Add an education entry to an alumni's record (full_access)."""
    return await profile_service.add_education(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/education/{education_id}",
    response_model=EducationRead,
)
async def update_education(
    alumni_id: IdPath,
    education_id: IdPath,
    payload: EducationUpdate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> EducationRead:
    """Edit an education entry on an alumni's record (full_access). 404 if the
    row is missing or belongs to another alumnus."""
    return await profile_service.update_education(
        session, alumni_id, education_id, payload, actor_user_id=user.user_id
    )


@router.delete(
    "/{alumni_id}/education/{education_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_education(
    alumni_id: IdPath,
    education_id: IdPath,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> None:
    """Delete an education entry from an alumni's record (full_access). 404 if the
    row is missing or belongs to another alumnus."""
    await profile_service.delete_education(
        session, alumni_id, education_id, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/leadership",
    response_model=LeadershipRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_leadership(
    alumni_id: IdPath,
    payload: LeadershipCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> LeadershipRead:
    """Add a Finance Society leadership entry to an alumni (full_access)."""
    return await profile_service.add_leadership(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.patch(
    "/{alumni_id}/leadership/{finance_society_leadership_id}",
    response_model=LeadershipRead,
)
async def update_leadership(
    alumni_id: IdPath,
    finance_society_leadership_id: IdPath,
    payload: LeadershipUpdate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> LeadershipRead:
    """Edit a Finance Society leadership entry (full_access). 404 if the row is
    missing or belongs to another alumnus."""
    return await profile_service.update_leadership(
        session,
        alumni_id,
        finance_society_leadership_id,
        payload,
        actor_user_id=user.user_id,
    )


@router.delete(
    "/{alumni_id}/leadership/{finance_society_leadership_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_leadership(
    alumni_id: IdPath,
    finance_society_leadership_id: IdPath,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> None:
    """Delete a Finance Society leadership entry (full_access). 404 if the row is
    missing or belongs to another alumnus."""
    await profile_service.delete_leadership(
        session,
        alumni_id,
        finance_society_leadership_id,
        actor_user_id=user.user_id,
    )


@router.post(
    "/{alumni_id}/tags",
    response_model=list[str],
    status_code=status.HTTP_201_CREATED,
)
async def add_tag(
    alumni_id: IdPath,
    payload: TagCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Attach a canonical engagement tag to an alumni (full_access). Returns the
    resulting tag list. Idempotent."""
    return await profile_service.add_tag(session, alumni_id, payload, actor_user_id=user.user_id)


@router.delete("/{alumni_id}/tags/{tag:path}", response_model=list[str])
async def remove_tag(
    alumni_id: IdPath,
    tag: str,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Detach a tag from an alumni (full_access). Returns the resulting tag list.
    404 if the alumni doesn't have that tag."""
    return await profile_service.remove_tag(session, alumni_id, tag, actor_user_id=user.user_id)


@router.post(
    "/{alumni_id}/status-labels",
    response_model=list[str],
    status_code=status.HTTP_201_CREATED,
)
async def add_status_label(
    alumni_id: IdPath,
    payload: StatusLabelCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Attach a canonical status label to an alumni (full_access). Returns the
    resulting label list. Idempotent."""
    return await profile_service.add_status_label(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}/status-labels/{label:path}", response_model=list[str])
async def remove_status_label(
    alumni_id: IdPath,
    label: str,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> list[str]:
    """Detach a status label from an alumni (full_access). Returns the resulting
    label list. 404 if the alumni doesn't have that label."""
    return await profile_service.remove_status_label(
        session, alumni_id, label, actor_user_id=user.user_id
    )


@router.post(
    "/{alumni_id}/events",
    response_model=EventAttendedRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_event_attendance(
    alumni_id: IdPath,
    payload: EventAttendanceCreate,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> EventAttendedRead:
    """Mark an alumni as an attendee of an existing event (full_access). 404 if
    the event/alumni is unknown; 409 if attendance already exists."""
    return await profile_service.add_event_attendance(
        session, alumni_id, payload, actor_user_id=user.user_id
    )


def _drop_manual_updated_date(payload: AlumniCreateFull | AlumniUpdateFull):
    """Strip the hand-typed ``profile_updated_date`` from a CLIENT write (#285).

    "Last updated" is now driven by ``updated_at`` (auto-bumped) + the actor FK,
    so the manual date must not be settable from the app — two dates that can
    disagree is the bug. The column and its existing data are KEPT: they are the
    provenance record of what the intake spreadsheet claimed, and the CSV importer
    (which writes through the service, not this route) still populates them.

    Discarding the key from ``__pydantic_fields_set__`` rather than nulling the
    value is deliberate: the write path dumps with ``exclude_unset=True``, so an
    explicit ``None`` would CLEAR the stored spreadsheet date on every save, while
    an unset field is simply never written.
    """
    payload.__pydantic_fields_set__.discard("profile_updated_date")
    return payload


@router.post("/preview", response_model=AlumniHygienePreview)
async def preview_create_alumni(
    payload: AlumniCreateFull, user: RequireFullAccess, session: SessionDep
) -> dict:
    """Dry-run data-hygiene preview for a NEW alumni (full_access, no writes).

    Returns ``{cleaned, changes, warnings, blockers}`` — the cleaned (normalized)
    payload, the per-field changes cleaning would make, soft warnings (completeness
    + fuzzy possible-duplicates), and exact-duplicate blockers (a non-empty list
    means the real POST would 409). The preview reads stored data, so it is
    audit-logged (``preview``)."""
    result = await hygiene.build_preview(session, _drop_manual_updated_date(payload))
    await service.log_preview(session, actor_user_id=user.user_id)
    return result


@router.post("/{alumni_id}/preview", response_model=AlumniHygienePreview)
async def preview_update_alumni(
    alumni_id: IdPath,
    payload: AlumniUpdateFull,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> dict:
    """Dry-run data-hygiene preview for an EDIT (full_access, no writes).

    Loads the current record (404 if missing/archived) and computes the preview
    against the EFFECTIVE record (the cleaned partial overlaid on the stored
    values) so duplicate + completeness checks reflect the resulting state. The
    preview reads stored data, so it is audit-logged (``preview``)."""
    existing = await service.get_alumni(session, alumni_id, include_archived=True)
    if existing.archived:
        raise NotFoundError(f"Alumni {alumni_id} not found.")
    result = await hygiene.build_preview(
        session,
        _drop_manual_updated_date(payload),
        existing=existing,
        exclude_alumni_id=alumni_id,
    )
    await service.log_preview(session, actor_user_id=user.user_id, alumni_id=alumni_id)
    return result


@router.post("", response_model=AlumniRead, status_code=status.HTTP_201_CREATED)
async def create_alumni(
    payload: AlumniCreateFull, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    return await service.create_alumni(
        session, _drop_manual_updated_date(payload), actor_user_id=user.user_id
    )


@router.patch("/{alumni_id}", response_model=AlumniRead)
async def update_alumni(
    alumni_id: IdPath,
    payload: AlumniUpdateFull,
    user: RequireAlumniEdit,
    session: SessionDep,
) -> AlumniRead:
    return await service.update_alumni(
        session, alumni_id, _drop_manual_updated_date(payload), actor_user_id=user.user_id
    )


@router.delete("/{alumni_id}", response_model=AlumniRead)
async def archive_alumni(
    alumni_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Soft-delete (archive) an alumni record."""
    return await service.archive_alumni(session, alumni_id, actor_user_id=user.user_id)


@router.post("/{alumni_id}/restore", response_model=AlumniRead)
async def restore_alumni(
    alumni_id: IdPath, user: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Restore (unarchive) a previously archived alumni record."""
    return await service.restore_alumni(session, alumni_id, actor_user_id=user.user_id)

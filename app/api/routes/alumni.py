"""Alumni CRUD routes.

Reads require view access (any role). Editing an EXISTING alumnus and their
nested records (interactions, employment, education, leadership, tags, status
labels, tasks, event attendance) requires edit access (``student`` and up, via
``RequireAlumniEdit``). Creating a new alumnus, archiving/restoring, and CSV
import require ``full_access`` and up (``RequireFullAccess``) — ``student`` is
deliberately excluded from those. ``DELETE`` on an alumnus is a soft-delete
(archive), never a hard delete — audit history depends on retained records.
"""

import asyncio
import datetime
import posixpath
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
    HeadshotBulkConfirmRequest,
    HeadshotBulkItem,
    HeadshotBulkResult,
    HeadshotBulkUploadRequest,
    HeadshotBulkUploadTarget,
    HeadshotBulkUploadUrls,
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
        Query(
            description=(
                "PRIMARY industry / work area — repeatable (OR), exact match. "
                "Narrowed to the primary column (#584): it no longer also matches "
                "the secondary industry — use 'secondary_industry' for that."
            )
        ),
    ] = None,
    secondary_industry: Annotated[
        list[str] | None,
        Query(
            description=(
                "SECONDARY industry / work area (#584) — repeatable (OR), exact "
                "match. Combined with 'industry' it AND-s: primary is X AND "
                "secondary is Y."
            )
        ),
    ] = None,
    title: Annotated[
        list[str] | None,
        Query(description="Current job title(s) — repeatable, exact match."),
    ] = None,
    seniority: Annotated[
        list[str] | None,
        Query(description="Seniority level(s) — repeatable, exact match."),
    ] = None,
    employment_status: Annotated[
        list[str] | None,
        Query(
            description=(
                "Employment status(es) (#584) — repeatable (OR), exact match. "
                "Canonical values: Full-time, Part-time, Self-Employed, Graduate "
                "Student, Military, Not in the Labor Force, Unemployed. The column "
                "is free text and also holds off-list legacy values, so anything on "
                "file is accepted; 'filter-options.employment_statuses' lists what "
                "actually exists in the data."
            )
        ),
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
    cfp: Annotated[
        bool, Query(description="Only alumni holding the CFP designation.")
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
        Literal[
            "name",
            "grad_desc",
            "grad_asc",
            "industry",
            "city",
            "state",
            "employer",
            "gender",
            "updated",
        ],
        Query(
            description=(
                "Sort order: name | grad_desc | grad_asc | industry | city | "
                "state | employer | gender | updated."
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
        secondary_industry=secondary_industry,
        title=title,
        seniority=seniority,
        employment_status=employment_status,
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
        cfp=cfp,
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
            "secondary_industry": (
                "|".join(secondary_industry) if secondary_industry else None
            ),
            "title": "|".join(title) if title else None,
            "seniority": "|".join(seniority) if seniority else None,
            "employment_status": (
                "|".join(employment_status) if employment_status else None
            ),
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
            "cfp": cfp or None,
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

# Bulk headshot import (#401, reworked in #595). Extension -> MIME: a bulk file
# name is all we get before the bytes land, so the extension picks the MIME we
# scope the signed upload URL to. It is only ever a LABEL — the real bytes are
# sniffed at confirm time. Keys are the canonical allow-list; anything else is
# reported ``invalid``.
_HEADSHOT_EXT_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
# Per-REQUEST batch cap for the two bulk endpoints. Image bytes go browser ->
# Supabase directly, so the only server cost is one storage round-trip + one
# audit row per file; this bounds a single function invocation's fan-out (and
# therefore its duration). Larger imports are chunked by the client into
# successive requests, so this is NOT a ceiling on how many photos can be
# imported — only on how many ride in one call.
_HEADSHOT_BULK_MAX_PER_REQUEST = 100
# How many storage round-trips (mint / probe) we run at once within a request.
# Bounded so a full batch can't open 100 simultaneous connections to Supabase.
_HEADSHOT_BULK_CONCURRENCY = 8


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


# --- Bulk headshot import: direct-to-storage (#401, reworked in #595) ---------
#
# The original route took the whole batch as ONE multipart body. Vercel rejects
# any request over ~4.5 MB at the edge — before our function runs — and that
# platform error carries no CORS headers, so the browser reported a bogus CORS
# failure for every batch bigger than a couple of photos. Bytes must therefore
# never traverse the function.
#
# The flow is now the same one the single headshot already uses:
#   1. POST /alumni/headshots/bulk/upload-urls — filenames ONLY. The server
#      derives each net ID, resolves the alumnus, and mints a signed upload URL
#      scoped to THAT alumnus's object key. Unmatched / non-image names are
#      reported and get no URL, so they can never be uploaded.
#   2. The browser PUTs each image straight to Supabase Storage.
#   3. POST /alumni/headshots/bulk/confirm — filenames + per-file outcome. The
#      server re-derives every net ID, re-resolves every alumnus, sniffs the
#      landed object's real bytes, deletes anything non-conforming, writes the
#      audit trail, and returns the authoritative per-file report.
#
# Both requests are pure JSON metadata, so a request stays a few KB no matter
# how large the photos are. Batches over _HEADSHOT_BULK_MAX_PER_REQUEST are
# chunked by the client into successive calls.


def _bulk_too_many_response() -> JSONResponse:
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": (
                    f"Too many files in one request (limit "
                    f"{_HEADSHOT_BULK_MAX_PER_REQUEST}). Send them in smaller batches."
                ),
            }
        },
    )


def _clean_client_detail(message: str | None) -> str | None:
    """Sanitize a browser-supplied failure detail before it is echoed back in the
    per-file report: printable characters only, hard length cap. The value never
    reaches the DB or the logs, but it IS reflected to the operator, so it gets
    the same treatment as any other untrusted string."""
    if not message:
        return None
    cleaned = "".join(ch for ch in message if ch.isprintable()).strip()
    if not cleaned:
        return None
    return cleaned[:120]


class _BulkPhoto:
    """One filename in a bulk photo import, resolved SERVER-SIDE.

    ``error`` is set when the name itself is unusable (no net ID, or not a
    JPEG/PNG/WebP by extension); ``alumnus`` is None when no alumnus owns the net
    ID. Both verdicts — and the storage key — are decided here, never by the
    browser. ``upload_url`` is filled in by the minting route."""

    __slots__ = ("filename", "net_id", "content_type", "alumnus", "error", "upload_url")

    def __init__(
        self,
        filename: str,
        *,
        net_id: str | None = None,
        content_type: str | None = None,
        alumnus: Alumni | None = None,
        error: str | None = None,
    ) -> None:
        self.filename = filename
        self.net_id = net_id
        self.content_type = content_type
        self.alumnus = alumnus
        self.error = error
        self.upload_url: str | None = None

    @property
    def object_key(self) -> str:
        """The alumnus's STORED net ID — the object key we scope uploads to.

        Deliberately not the net ID parsed out of the file name: only a value
        that came back from the database may address an object, so a crafted
        file name can never point the upload at another key."""
        return (self.alumnus.net_id or "").strip() if self.alumnus else ""


async def _resolve_bulk_photos(
    session: AsyncSession, filenames: list[str]
) -> list[_BulkPhoto]:
    """Map each file name to its net ID, image type, and alumnus (one query, no
    N+1). Case-insensitive on the net ID, matching the single-headshot path."""
    photos: list[_BulkPhoto] = []
    for raw in filenames:
        filename = (raw or "").strip() or "(unnamed)"
        net_id = _net_id_from_filename(filename)
        content_type = _headshot_mime_for_ext(filename)
        if content_type is None:
            photos.append(
                _BulkPhoto(
                    filename,
                    net_id=net_id or None,
                    error="File must be a JPEG, PNG, or WebP image.",
                )
            )
            continue
        if not net_id:
            photos.append(
                _BulkPhoto(
                    filename,
                    error="Could not derive a net ID from the file name.",
                )
            )
            continue
        photos.append(_BulkPhoto(filename, net_id=net_id, content_type=content_type))

    wanted = {p.net_id.lower() for p in photos if p.error is None and p.net_id}
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
    for photo in photos:
        if photo.error is None and photo.net_id:
            photo.alumnus = matches.get(photo.net_id.lower())
    return photos


@router.post("/headshots/bulk/upload-urls", response_model=HeadshotBulkUploadUrls)
async def create_bulk_headshot_upload_urls(
    user: BulkHeadshotRateLimit,
    session: SessionDep,
    payload: HeadshotBulkUploadRequest,
) -> HeadshotBulkUploadUrls | JSONResponse:
    """Mint per-file signed upload URLs for a bulk photo import (full_access+).

    Takes FILE NAMES ONLY — no image bytes — so the request stays a few KB no
    matter how big the batch is. Each name's net ID is its basename minus the
    extension, matched to an alumnus case-insensitively; a name that matches
    nobody, or isn't a JPEG/PNG/WebP, comes back reported and WITHOUT a URL, so
    it can never be uploaded. Every minted URL is scoped by the server to that
    alumnus's own object key, so the browser never chooses where bytes land and
    never sees the service key.

    Like the single-headshot route, minting writes an ``upload_headshot_started``
    audit row: it is the attributable precondition for an image change, so the
    FERPA trail survives a browser that never reaches confirm. Confirm writes the
    terminal ``upload_headshot`` / ``upload_headshot_rejected``."""
    if not payload.filenames:
        raise InvalidRequestError("No file names were provided.")
    if len(payload.filenames) > _HEADSHOT_BULK_MAX_PER_REQUEST:
        return _bulk_too_many_response()

    photos = await _resolve_bulk_photos(session, payload.filenames)
    ready = [
        p for p in photos if p.error is None and p.alumnus is not None and p.object_key
    ]

    semaphore = asyncio.Semaphore(_HEADSHOT_BULK_CONCURRENCY)

    async def _mint(photo: _BulkPhoto) -> str | None:
        async with semaphore:
            try:
                return await supabase_storage.create_signed_upload_url(
                    _HEADSHOT_BUCKET, photo.object_key
                )
            except ServiceError:
                # One unavailable mint is reported for that file only — it must
                # not fail the whole batch.
                return None

    if ready:
        for photo, url in zip(
            ready, await asyncio.gather(*(_mint(p) for p in ready)), strict=True
        ):
            photo.upload_url = url

    targets: list[HeadshotBulkUploadTarget] = []
    minted = 0
    for photo in photos:
        if photo.error is not None:
            targets.append(
                HeadshotBulkUploadTarget(
                    filename=photo.filename,
                    net_id=photo.net_id,
                    status="invalid",
                    message=photo.error,
                )
            )
            continue
        if photo.alumnus is None or not photo.object_key:
            targets.append(
                HeadshotBulkUploadTarget(
                    filename=photo.filename,
                    net_id=photo.net_id,
                    status="no_match",
                    message="No alumnus has this net ID.",
                )
            )
            continue
        if photo.upload_url is None:
            targets.append(
                HeadshotBulkUploadTarget(
                    filename=photo.filename,
                    net_id=photo.net_id,
                    status="error",
                    message="The file storage service is unavailable — try again.",
                )
            )
            continue
        service._audit(
            session,
            user.user_id,
            "upload_headshot_started",
            photo.alumnus.alumni_id,
            new_value=photo.object_key,
        )
        minted += 1
        targets.append(
            HeadshotBulkUploadTarget(
                filename=photo.filename,
                net_id=photo.net_id,
                status="ready",
                message="Ready to upload.",
                upload_url=photo.upload_url,
            )
        )

    if minted:
        await session.commit()
    return HeadshotBulkUploadUrls(targets=targets)


async def _verify_landed_headshot(key: str) -> tuple[str, str, str | None]:
    """Validate one object that landed via a direct PUT.

    Returns ``(status, message, rejected_field)``; ``rejected_field`` is set only
    when the object must be deleted. Defense in depth: the bucket's own MIME
    allow-list and size limit are the primary guard, but we re-check the type and
    size here AND sniff the real leading bytes — a browser-supplied Content-Type
    on a direct PUT is only a label, so the magic bytes are the sole trustworthy
    signal that the object really is a JPEG/PNG/WebP. FAILS OPEN if the object
    can't be probed at all (falling back to an existence check), so a storage
    hiccup never rejects a legitimate upload."""
    content_type, size, head = await supabase_storage.probe_object_head(
        _HEADSHOT_BUCKET, key
    )
    if content_type is None and size is None and head is None:
        try:
            exists = (
                await supabase_storage.create_signed_url(_HEADSHOT_BUCKET, key)
                is not None
            )
        except ServiceError:
            exists = False
        if not exists:
            return ("error", "No uploaded image was found in storage.", None)
        return ("matched", "Headshot uploaded.", None)
    if content_type is not None and content_type not in _HEADSHOT_MIME_TYPES:
        return (
            "invalid",
            "Headshot must be a JPEG, PNG, or WebP image.",
            "content_type",
        )
    if size is not None and size > _HEADSHOT_MAX_BYTES:
        mib = _HEADSHOT_MAX_BYTES // (1024 * 1024)
        return ("invalid", f"Image exceeds the {mib} MB per-file size limit.", "size")
    if head:
        if content_type is not None:
            content_error = _image_content_error(head, content_type)
        else:
            content_error = (
                None
                if _sniff_image_mime(head) is not None
                else "File content is not a JPEG, PNG, or WebP image."
            )
        if content_error is not None:
            return ("invalid", content_error, "content")
    return ("matched", "Headshot uploaded.", None)


@router.post("/headshots/bulk/confirm", response_model=HeadshotBulkResult)
async def confirm_bulk_headshot_upload(
    user: BulkHeadshotRateLimit,
    session: SessionDep,
    payload: HeadshotBulkConfirmRequest,
) -> HeadshotBulkResult | JSONResponse:
    """Validate + audit the objects a bulk photo import landed (full_access+).

    Takes every file in the batch with the browser's per-file upload outcome and
    returns the authoritative report the wizard renders (``matched`` /
    ``no_match`` / ``invalid`` / ``error`` plus tallies). Nothing the browser
    sends is trusted: net IDs are re-derived, alumni re-resolved, and each landed
    object re-validated (type, size, sniffed magic bytes). A non-conforming
    object is DELETED and audited ``upload_headshot_rejected``; a conforming one
    is audited ``upload_headshot``, exactly like the single-headshot path.

    The per-file ``uploaded`` flag decides only what we REPORT, never whether we
    look. Every matched alumnus's object is probed either way, so a client that
    PUTs a bad image and then claims the upload failed can't skip validation and
    leave that object sitting in the bucket. Conversely, a file the client says
    failed is never audited ``upload_headshot`` even if a conforming object is
    present — that object may be the alumnus's PREVIOUS headshot, and a failed
    upload must not be recorded as a successful one."""
    if not payload.files:
        raise InvalidRequestError("No files were provided.")
    if len(payload.files) > _HEADSHOT_BULK_MAX_PER_REQUEST:
        return _bulk_too_many_response()

    claims = list(payload.files)
    photos = await _resolve_bulk_photos(session, [f.filename for f in claims])

    # Validate each landed object ONCE PER STORAGE KEY. Two files can map to the
    # same net ID (jdoe.jpg + jdoe.png) and the second PUT overwrites the first,
    # so probing per file would judge the surviving object twice — and could
    # delete a good image on the strength of its overwritten twin.
    pending: dict[str, Alumni] = {}
    for photo in photos:
        if photo.error is None and photo.alumnus is not None and photo.object_key:
            pending.setdefault(photo.object_key, photo.alumnus)

    semaphore = asyncio.Semaphore(_HEADSHOT_BULK_CONCURRENCY)

    async def _verify(key: str) -> tuple[str, str, str | None]:
        async with semaphore:
            return await _verify_landed_headshot(key)

    keys = list(pending)
    verdicts: dict[str, tuple[str, str, str | None]] = {}
    if keys:
        for key, verdict in zip(
            keys, await asyncio.gather(*(_verify(k) for k in keys)), strict=True
        ):
            verdicts[key] = verdict

    audited = 0
    # Purge + audit every object that failed validation, once per key.
    for key, (verdict_status, _message, rejected_field) in verdicts.items():
        if verdict_status != "invalid" or rejected_field is None:
            continue
        try:
            await supabase_storage.delete_object(_HEADSHOT_BUCKET, key)
        except ServiceError:
            # The audit below still records the rejection; a stuck object is a
            # storage problem, not a reason to report the file as accepted.
            pass
        service._audit(
            session,
            user.user_id,
            "upload_headshot_rejected",
            pending[key].alumni_id,
            field_name=rejected_field,
            new_value=key,
        )
        audited += 1

    items: list[HeadshotBulkItem] = []
    for photo, claim in zip(photos, claims, strict=True):
        if photo.error is not None:
            items.append(
                HeadshotBulkItem(
                    filename=photo.filename,
                    net_id=photo.net_id,
                    status="invalid",
                    message=photo.error,
                )
            )
            continue
        if photo.alumnus is None or not photo.object_key:
            items.append(
                HeadshotBulkItem(
                    filename=photo.filename,
                    net_id=photo.net_id,
                    status="no_match",
                    message="No alumnus has this net ID.",
                )
            )
            continue
        verdict_status, message, _rejected_field = verdicts[photo.object_key]
        # A rejected object was purged above; report that regardless of what the
        # browser claimed, so lying about the outcome can't hide it.
        if verdict_status != "invalid" and not claim.uploaded:
            detail = _clean_client_detail(claim.message)
            items.append(
                HeadshotBulkItem(
                    filename=photo.filename,
                    net_id=photo.net_id,
                    status="error",
                    message=detail or "The photo could not be uploaded — try again.",
                )
            )
            continue
        if verdict_status == "matched":
            service._audit(
                session,
                user.user_id,
                "upload_headshot",
                photo.alumnus.alumni_id,
                new_value=photo.object_key,
            )
            audited += 1
        items.append(
            HeadshotBulkItem(
                filename=photo.filename,
                net_id=photo.net_id,
                status=verdict_status,
                message=message,
            )
        )

    if audited:
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
    grad_year: Annotated[int | None, Query(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)] = None,
    class_year: Annotated[
        int | None, Query(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)
    ] = None,
) -> Response | JSONResponse:
    """Download an ACTIVE cohort as a FILLED intake-template CSV (full_access).

    Pick the cohort by EITHER ``grad_year`` (the BYU graduation year) OR
    ``class_year`` (the Marriott "Class of" year) — provide exactly one. Powers
    the round-trip: download the cohort in the EXACT import-template column
    format, edit cells offline, then re-upload through ``POST
    /alumni/import/update`` (which matches by BYU ID / Net ID and applies only
    the changed cells). Both years are validated to the alumni-schema bounds. A
    cohort larger than the export cap is a 413 asking the caller to narrow it
    down. Audit-logged (``export_alumni``) like the other exports."""
    if (grad_year is None) == (class_year is None):
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Provide exactly one of grad_year or class_year.",
                }
            },
        )
    try:
        csv_text = await import_csv.build_cohort_update_csv(
            session,
            graduation_year=grad_year,
            graduation_class=class_year,
            actor_user_id=user.user_id,
        )
    except import_csv.CohortTooLargeError as exc:
        return JSONResponse(
            status_code=413,
            content={"error": {"code": "payload_too_large", "message": str(exc)}},
        )
    fname = (
        f"alumni_cohort_gradyear_{grad_year}.csv"
        if grad_year is not None
        else f"alumni_cohort_classof_{class_year}.csv"
    )
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
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

"""Survey campaign routes — sending the annual "confirm your info" emails.

`POST /survey/campaigns/{grad_year}/send` is full-access gated and defaults to a
**dry run** (builds + counts, sends nothing). Pass `?dry_run=false` to actually
send via Resend, `?limit=N` to override the per-call cap.

Sends and responses here are what the profile's Surveys tab reports on, via
`profile._derive_survey_history`. Nothing in this module should write to the
legacy `surveys` table — see `models.crm.Survey`.
"""

import hmac
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireEngineer, RequireFullAccess
from app.api.routes.alumni import (
    _HEADSHOT_MAX_BYTES,
    _HEADSHOT_MIME_TYPES,
    _image_content_error,
    _read_capped,
    _too_large_response,
)
from app.core.config import get_settings
from app.core.database import get_session
from app.core.errors import InvalidRequestError, NotFoundError
from app.core.rate_limit import (
    SURVEY_PHOTO_LIMITER,
    SURVEY_RESPOND_READ_LIMITER,
    SURVEY_SUBMIT_LIMITER,
)
from app.schemas.survey import (
    GraduationYearCount,
    SurveyNewCyclePreview,
    SurveyNewCycleRequest,
    SurveyNonResponder,
    SurveyRespondInfo,
    SurveyResponseItem,
    SurveyScheduleBulkRequest,
    SurveyScheduleCancelAllResult,
    SurveyScheduleCreateRequest,
    SurveyScheduleItem,
    SurveySchedulePauseAllResult,
    SurveyScheduleRunSummary,
    SurveySendConfigItem,
    SurveySendConfigUpdateRequest,
    SurveySendResult,
    SurveySubmitRequest,
    SurveySubmitResult,
    SurveyUsage,
)
from app.services import survey_email, survey_responses, survey_schedule

# The test cohort lives in grad year 1900 (below the normal 1950 floor), so allow
# it explicitly here.
_GRAD_YEAR_MIN = 1900
_GRAD_YEAR_MAX = 2100

SessionDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(prefix="/survey", tags=["survey"])


@router.get(
    "/respond/{token}",
    response_model=SurveyRespondInfo,
    dependencies=[Depends(SURVEY_RESPOND_READ_LIMITER)],
)
async def survey_respond_info(token: str, session: SessionDep) -> SurveyRespondInfo:
    """PUBLIC (token-gated, no login): the alum's current on-file info for the
    confirm page. The signed token is the credential — an invalid or expired one
    404s with the same message either way."""
    info = await survey_email.get_respondent(session, token)
    if info is None:
        raise NotFoundError(survey_email.LINK_DEAD_MESSAGE)
    return info


@router.post(
    "/respond/{token}",
    response_model=SurveySubmitResult,
    dependencies=[Depends(SURVEY_SUBMIT_LIMITER)],
)
async def survey_submit(
    token: str, body: SurveySubmitRequest, session: SessionDep
) -> SurveySubmitResult:
    """PUBLIC (token-gated): stage the alum's submitted changes for admin review.
    Nothing is applied to the record here."""
    return await survey_responses.submit_response(
        session, token, body.fields, body.has_photo
    )


@router.post(
    "/respond/{token}/photo",
    status_code=204,
    dependencies=[Depends(SURVEY_PHOTO_LIMITER)],
)
async def survey_submit_photo(
    token: str,
    session: SessionDep,
    survey_response_id: Annotated[int, Form()],
    photo: Annotated[UploadFile, File()],
) -> Response:
    """PUBLIC (token-gated): attach a NEW profile photo to a just-staged response.

    A separate step from the JSON field-submit so the field submit is unaffected.
    The signed token gates it (no login); the same JPEG/PNG/WebP + size validation
    as the headshot upload runs here before the image is staged for admin review.
    The photo only becomes the alum's headshot if an admin applies the response."""
    content_type = (photo.content_type or "").split(";")[0].strip().lower()
    if content_type not in _HEADSHOT_MIME_TYPES:
        raise InvalidRequestError("Photo must be a JPEG, PNG, or WebP image.")
    data = await _read_capped(photo, _HEADSHOT_MAX_BYTES)
    if data is None:
        return _too_large_response(_HEADSHOT_MAX_BYTES)
    if not data:
        raise InvalidRequestError("The uploaded image is empty.")
    # The Content-Type is just a client label; verify the real bytes match before
    # anything reaches storage.
    content_error = _image_content_error(data, content_type)
    if content_error is not None:
        raise InvalidRequestError(content_error)
    await survey_responses.stage_photo(
        session, token, survey_response_id, data, content_type
    )
    return Response(status_code=204)


@router.get(
    "/campaigns/{grad_year}/responses",
    response_model=list[SurveyResponseItem],
)
async def survey_pending_responses(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireFullAccess,
    session: SessionDep,
) -> list[SurveyResponseItem]:
    """Admin review queue: pending responses for a grad year, each with a diff."""
    return await survey_responses.list_pending(session, grad_year)


@router.post("/responses/{response_id}/apply", status_code=204)
async def survey_apply_response(
    response_id: int, user: RequireFullAccess, session: SessionDep
) -> None:
    """Apply a staged response to the alum's record."""
    await survey_responses.apply_response(session, response_id, user.user_id)


@router.post("/responses/{response_id}/reject", status_code=204)
async def survey_reject_response(
    response_id: int, user: RequireFullAccess, session: SessionDep
) -> None:
    """Reject a staged response — nothing is written to the record."""
    await survey_responses.reject_response(session, response_id, user.user_id)


@router.get("/graduation-years", response_model=list[GraduationYearCount])
async def survey_graduation_years(
    user: RequireFullAccess, session: SessionDep
) -> list[GraduationYearCount]:
    """Distinct graduation years present in the DB (eligible alumni) + counts,
    newest first — powers the console's year picker."""
    return await survey_email.list_graduation_years(session)


@router.get("/usage", response_model=SurveyUsage)
async def survey_send_usage(
    user: RequireFullAccess, session: SessionDep
) -> SurveyUsage:
    """Real Resend send usage (emails actually sent today / this calendar month),
    for the console's daily/monthly tallies against the send caps."""
    return await survey_email.get_send_usage(session)


@router.get("/send-config", response_model=SurveySendConfigItem)
async def get_survey_send_config(
    user: RequireFullAccess, session: SessionDep
) -> SurveySendConfigItem:
    """The account-wide send cap the scheduler paces against — the daily/monthly
    email budget and whether it's enforced."""
    return await survey_schedule.get_send_config(session)


@router.post("/send-config", response_model=SurveySendConfigItem)
async def update_survey_send_config(
    body: SurveySendConfigUpdateRequest,
    user: RequireFullAccess,
    session: SessionDep,
) -> SurveySendConfigItem:
    """Update the send cap. ``enabled=false`` removes the internal cap (e.g. after
    upgrading the Resend plan) — sends are then limited only by Resend itself."""
    return await survey_schedule.update_send_config(
        session,
        enabled=body.enabled,
        daily_limit=body.daily_limit,
        monthly_limit=body.monthly_limit,
        actor_user_id=user.user_id,
    )


@router.post("/campaigns/{grad_year}/send", response_model=SurveySendResult)
async def send_survey_campaign(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireFullAccess,
    session: SessionDep,
    dry_run: Annotated[bool, Query()] = True,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> SurveySendResult:
    return await survey_email.send_campaign(
        session,
        graduation_year=grad_year,
        actor_user_id=user.user_id,
        dry_run=dry_run,
        limit=limit,
    )


# --------------------------------------------------------------- scheduler ----


@router.get("/schedules", response_model=list[SurveyScheduleItem])
async def list_survey_schedules(
    user: RequireFullAccess, session: SessionDep
) -> list[SurveyScheduleItem]:
    """All auto-send schedules (newest cohort first) + per-stage sent counts.

    Also backs the engineer Surveys console (which needs who started each
    campaign and when) — the console reads this rather than a second endpoint,
    since it wants exactly this list. The engineer holds every capability, so
    the full-access gate already admits them."""
    return await survey_schedule.list_schedules(session)


@router.post("/schedules", response_model=SurveyScheduleItem)
async def create_survey_schedule(
    body: SurveyScheduleCreateRequest,
    user: RequireFullAccess,
    session: SessionDep,
) -> SurveyScheduleItem:
    """Create — or replace — the auto-send schedule for a graduation year."""
    if not _GRAD_YEAR_MIN <= body.graduation_year <= _GRAD_YEAR_MAX:
        raise InvalidRequestError("Graduation year is out of range.")
    return await survey_schedule.create_schedule(
        session,
        graduation_year=body.graduation_year,
        start_date=body.start_date,
        actor_user_id=user.user_id,
    )


@router.get(
    "/schedules/{grad_year}/new-cycle/preview",
    response_model=SurveyNewCyclePreview,
)
async def preview_survey_new_cycle(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireFullAccess,
    session: SessionDep,
) -> SurveyNewCyclePreview:
    """What starting a new survey cycle for this year would do (#357).

    Read-only: nothing is scheduled and nothing is sent. Backs the confirmation
    shown before the irreversible `new-cycle` call, so staff see how many alumni
    would be emailed — and how many of those already received the current
    cycle — before committing to it."""
    preview = await survey_schedule.preview_new_cycle(session, grad_year)
    if preview is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return preview


@router.post("/schedules/{grad_year}/new-cycle", response_model=SurveyScheduleItem)
async def start_survey_new_cycle(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    body: SurveyNewCycleRequest,
    user: RequireFullAccess,
    session: SessionDep,
) -> SurveyScheduleItem:
    """Start the NEXT survey campaign for a graduation year (#357).

    Advances the year's cycle, making the whole eligible cohort emailable again
    while every previous cycle's send log stays intact as history. Nothing is
    deleted.

    Deliberately separate from `POST /schedules`, which REPLACES a year's
    schedule without advancing the cycle (Jake, 2026-08-03). That one is the
    "I mistyped the start date" correction and must never re-email anyone; this
    one is the annual re-run and always will. Confirm with the user against
    `new-cycle/preview` first — the send it sets up cannot be recalled."""
    item = await survey_schedule.start_new_cycle(
        session,
        graduation_year=grad_year,
        start_date=body.start_date,
        actor_user_id=user.user_id,
    )
    if item is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return item


@router.post("/schedules/bulk", response_model=list[SurveyScheduleItem])
async def create_survey_schedules_bulk(
    body: SurveyScheduleBulkRequest,
    user: RequireFullAccess,
    session: SessionDep,
) -> list[SurveyScheduleItem]:
    """Create — or replace — the auto-send schedule for many graduation years in
    one call. A duplicate year in the payload resolves to a single row (last one
    wins). Returns the full, refreshed schedule list."""
    for item in body.schedules:
        if not _GRAD_YEAR_MIN <= item.graduation_year <= _GRAD_YEAR_MAX:
            raise InvalidRequestError("Graduation year is out of range.")
    return await survey_schedule.create_schedules_bulk(
        session,
        items=body.schedules,
        actor_user_id=user.user_id,
    )


@router.get(
    "/schedules/{grad_year}/non-responders",
    response_model=list[SurveyNonResponder],
)
async def list_survey_non_responders(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireFullAccess,
    session: SessionDep,
) -> list[SurveyNonResponder]:
    """Who needs MANUAL follow-up for this year's current campaign (#359).

    The alumni who received all three of this cycle's emails and never replied —
    #151's third step. `SurveyScheduleItem.non_responders` is the same set as a
    count; this is the call sheet behind it, so "N never responded" is something
    staff can act on rather than just read.

    Read-only, and gated like the rest of the console (full access) because it
    returns alumni contact details. Empty list = nobody left to chase; 404 = the
    year has no campaign at all. Cycle-scoped: a previous campaign's
    non-responders are not in here."""
    items = await survey_schedule.list_non_responders(session, grad_year)
    if items is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return items


@router.post("/schedules/{grad_year}/pause", response_model=SurveyScheduleItem)
async def pause_survey_schedule(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireFullAccess,
    session: SessionDep,
) -> SurveyScheduleItem:
    """Pause a graduation year's schedule — sending stops until it is resumed.

    The reversible stop, alongside the terminal `cancel`; same full-access gate,
    since a routine "hold this cohort for a few days" is less drastic than the
    cancel already available here. Pausing an already-paused campaign succeeds
    unchanged; pausing a completed or cancelled one is a 409."""
    item = await survey_schedule.pause_schedule(
        session, grad_year, actor_user_id=user.user_id
    )
    if item is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return item


@router.post("/schedules/{grad_year}/resume", response_model=SurveyScheduleItem)
async def resume_survey_schedule(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireFullAccess,
    session: SessionDep,
) -> SurveyScheduleItem:
    """Resume a paused schedule where its cadence left off.

    `start_date` is shifted forward by however long it was paused, so the stage
    the cron sends next is the one that was due when it stopped — a pause never
    silently ages a campaign past its reminder windows. Resuming a campaign that
    is already running succeeds unchanged; resuming a completed or cancelled one
    is a 409 (cancel stays terminal)."""
    item = await survey_schedule.resume_schedule(
        session, grad_year, actor_user_id=user.user_id
    )
    if item is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return item


@router.post("/schedules/pause-all", response_model=SurveySchedulePauseAllResult)
async def pause_all_survey_schedules(
    user: RequireEngineer, session: SessionDep
) -> SurveySchedulePauseAllResult:
    """Pause EVERY running survey campaign at once — the reversible kill switch.

    Sits beside `cancel-all` in the engineer console and is gated the same way
    (RequireEngineer): a blanket stop of every cohort is a maintenance action
    whatever its reversibility. Each paused year can be resumed individually and
    picks its cadence up where it stopped. Returns the count + the years paused
    so the console can report exactly what it stopped; calling it with nothing
    running succeeds and reports 0."""
    return await survey_schedule.pause_all_schedules(
        session, actor_user_id=user.user_id
    )


@router.post("/schedules/{grad_year}/cancel", response_model=SurveyScheduleItem)
async def cancel_survey_schedule(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireFullAccess,
    session: SessionDep,
) -> SurveyScheduleItem:
    """Cancel a graduation year's schedule — no further sends."""
    item = await survey_schedule.cancel_schedule(session, grad_year)
    if item is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return item


@router.post("/schedules/cancel-all", response_model=SurveyScheduleCancelAllResult)
async def cancel_all_survey_schedules(
    user: RequireEngineer, session: SessionDep
) -> SurveyScheduleCancelAllResult:
    """Stop EVERY running survey campaign at once — the engineer kill switch.

    Cancels all scheduled/active schedules in one statement, which is what stops
    the daily cron sending (it only picks up those two statuses). Deliberately
    narrower than the full-access per-year cancel: a blanket stop of every cohort
    is a maintenance action, so it is engineer-gated (RequireEngineer) like the
    rest of the engineer console. Returns the count + the years cancelled so the
    console can report exactly what it stopped; calling it with nothing running
    succeeds and reports 0."""
    return await survey_schedule.cancel_all_schedules(
        session, actor_user_id=user.user_id
    )


async def _run_cron(request: Request, session: AsyncSession) -> SurveyScheduleRunSummary:
    """Send scheduler cron core — NOT login-gated (Vercel Cron can't log in).

    Authorized only by a shared secret: the request must carry
    ``Authorization: Bearer <CRON_SECRET>``. Vercel Cron sends exactly this header
    automatically when ``CRON_SECRET`` is set as a project env var. Any other (or
    absent) credential → 401. When ``CRON_SECRET`` is unset the endpoint rejects
    everything, so it is never open by default.
    """
    expected = get_settings().cron_secret
    provided = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Invalid cron credentials.")
    return await survey_schedule.run_due_schedules(session)


@router.post("/cron/run", response_model=SurveyScheduleRunSummary)
async def survey_cron_run(
    request: Request, session: SessionDep
) -> SurveyScheduleRunSummary:
    """Run the survey send scheduler (POST). See :func:`_run_cron`."""
    return await _run_cron(request, session)


@router.get(
    "/cron/run", response_model=SurveyScheduleRunSummary, include_in_schema=False
)
async def survey_cron_run_get(
    request: Request, session: SessionDep
) -> SurveyScheduleRunSummary:
    """GET variant — Vercel Cron invokes the path with a GET."""
    return await _run_cron(request, session)

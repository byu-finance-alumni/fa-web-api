"""Survey campaign routes — sending the annual "confirm your info" emails.

`POST /survey/campaigns/{grad_year}/send` is full-access gated and defaults to a
**dry run** (builds + counts, sends nothing). Pass `?dry_run=false` to actually
send via Resend, `?limit=N` to override the per-call cap.
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

from app.api.dependencies.auth import RequireFullAccess
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
from app.schemas.survey import (
    GraduationYearCount,
    SurveyRespondInfo,
    SurveyResponseItem,
    SurveyScheduleBulkRequest,
    SurveyScheduleCreateRequest,
    SurveyScheduleItem,
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


@router.get("/respond/{token}", response_model=SurveyRespondInfo)
async def survey_respond_info(token: str, session: SessionDep) -> SurveyRespondInfo:
    """PUBLIC (token-gated, no login): the alum's current on-file info for the
    confirm page. The signed token is the credential — an invalid/expired one
    404s."""
    info = await survey_email.get_respondent(session, token)
    if info is None:
        raise NotFoundError("This survey link is invalid or has expired.")
    return info


@router.post("/respond/{token}", response_model=SurveySubmitResult)
async def survey_submit(
    token: str, body: SurveySubmitRequest, session: SessionDep
) -> SurveySubmitResult:
    """PUBLIC (token-gated): stage the alum's submitted changes for admin review.
    Nothing is applied to the record here."""
    return await survey_responses.submit_response(
        session, token, body.fields, body.has_photo
    )


@router.post("/respond/{token}/photo", status_code=204)
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
    """All auto-send schedules (newest cohort first) + per-stage sent counts."""
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

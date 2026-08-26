"""Survey campaign routes — sending the annual "confirm your info" emails.

`POST /survey/campaigns/{grad_year}/send` is full-access gated and defaults to a
**dry run** (builds + counts, sends nothing). Pass `?dry_run=false` to actually
send via Resend, `?limit=N` to override the per-call cap.

Sends and responses here are what the profile's Surveys tab reports on, via
`profile._derive_survey_history`. Nothing in this module should write to the
legacy `surveys` table — see `models.crm.Survey`.
"""

import contextlib
import hmac
from typing import Annotated, Literal

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
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireEngineer, RequireSurveysManage
from app.api.routes.alumni import (
    _HEADSHOT_MAX_BYTES,
    _HEADSHOT_MIME_TYPES,
    _read_capped,
    _sniff_image_mime,
    _too_large_response,
)
from app.core.config import get_settings
from app.core.database import get_session
from app.core.errors import InvalidRequestError, NotFoundError
from app.core.rate_limit import (
    OPPORTUNITY_LINK_SUBMIT_LIMITER,
    SURVEY_PHOTO_LIMITER,
    SURVEY_RESPOND_READ_LIMITER,
    SURVEY_SUBMIT_LIMITER,
)
from app.models.audit import AuditLog
from app.schemas.opportunity_link import (
    OpportunityLinkSubmitRequest,
    OpportunityLinkSubmitResult,
)
from app.schemas.survey import (
    GraduationYearCount,
    SurveyAlumniState,
    SurveyApplyResult,
    SurveyHeldOutPage,
    SurveyNewCyclePreview,
    SurveyNewCycleRequest,
    SurveyNonResponder,
    SurveyRecipientBreakdown,
    SurveyResetResult,
    SurveyRespondInfo,
    SurveyResponseItem,
    SurveyScheduleBulkRequest,
    SurveyScheduleCancelAllResult,
    SurveyScheduleCreateRequest,
    SurveyScheduleDeleteResult,
    SurveyScheduleItem,
    SurveySchedulePauseAllResult,
    SurveyScheduleRunSummary,
    SurveySendConfigItem,
    SurveySendConfigUpdateRequest,
    SurveySendResult,
    SurveySubmitRequest,
    SurveySubmitResult,
    SurveyUnreachableAlum,
    SurveyUsage,
)
from app.services import (
    opportunity_links,
    survey_email,
    survey_reset,
    survey_responses,
    survey_schedule,
)
from app.services.images import normalise_headshot

# The test cohort lives in grad year 1900 (below the normal 1950 floor), so allow
# it explicitly here.
_GRAD_YEAR_MIN = 1900
_GRAD_YEAR_MAX = 2100

SessionDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(prefix="/survey", tags=["survey"])


async def _log_survey_read(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int | None = None,
    scope: str | None = None,
) -> None:
    """Record a disclosure-audit row for an engineer-gated survey READ (#422).

    Both reads this backs name alumni and say something about them — who replied
    and when, what was emailed to whom — so each one is a FERPA-relevant
    disclosure and has to leave a trace. Neither did, which put them out of step
    with the rest of the codebase: ``GET /audit`` self-logs its own read (reading
    the log is itself audited), and ``services.alumni.log_search`` /
    ``log_preview`` exist for exactly this reason on the alumni side.

    ``scope`` records WHAT was asked for (graduation year, the reason filter,
    paging) — never a single value out of the response. The point of the row is
    that the read happened, by whom, over which slice; storing the names or reply
    dates it returned would copy the very PII the row exists to account for into
    a second table, which is what ``/audit``'s own self-log deliberately avoids.

    BEST EFFORT, like ``log_search``: the read has already succeeded by the time
    this runs, and failing the request because the bookkeeping write failed would
    turn an audit outage into an outage of the engineer's only view of who is
    being held out. A failure is swallowed and the session rolled back so the
    handler still returns cleanly. No-op when the actor is unknown.

    Where the row LANDS depends on the actor: these routes are ``RequireEngineer``
    and the ``engineer`` capability is non-assignable, so in practice the actor is
    always an engineer and the ``before_flush`` hook in ``app/models/audit.py``
    mirrors this AuditLog into ``engineer_action_log`` and drops the audit_logs
    row. That is the intended destination — engineer actions stay out of the
    record-change trail but land in the append-only log the engineer cannot purge.
    """
    if actor_user_id is None:
        return
    try:
        session.add(
            AuditLog(
                user_id=actor_user_id,
                action_type=action,
                entity_type=entity_type,
                entity_id=entity_id,
                new_value=scope[:1000] if scope else None,
            )
        )
        await session.commit()
    except Exception:  # noqa: BLE001 - audit is best-effort
        with contextlib.suppress(Exception):
            await session.rollback()


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


# Size caps for the public submit payload (#426). `submit_response` stages the
# payload as JSON, so without these anyone holding a survey link can persist a
# multi-megabyte row.
#
# This body is FIELD TEXT ONLY — a new profile photo travels as a separate
# multipart POST to `/respond/{token}/photo`, and `has_photo` here is just a
# flag — so nothing legitimate in it is large. They are ABUSE GUARDS, not data
# rules: the per-field lengths that mirror the real column widths live with the
# field table in `services.survey_responses`.
#
# THE TOTAL is the guard that matters — it is what bounds how much an abuser can
# persist per submission, and it is the one tuned against abuse rather than
# against any column.
#
# THE PER-FIELD CAP EXISTS ONLY TO STOP ONE ANSWER EATING THE WHOLE BUDGET, so
# it must sit ABOVE the widest column, never below it. It was originally 4 KiB,
# chosen as eight times LinkedIn's varchar(500) — which silently made
# `other_designations` (max_length=10000, the one `text` column here) impossible
# to fill: the byte cap fired first and that field's own limit became dead code.
# 40 KiB is 10000 characters at UTF-8's four-bytes-per-character worst case, so
# the declared column limit is now always the thing an honest respondent meets.
# `test_survey_submit_limits` asserts that relationship against the real field
# table, so tightening this or widening a column cannot quietly re-break it.
#
# Both sit far under Vercel's ~4.5 MB edge body cap ON PURPOSE. A request above
# that ceiling never reaches this function — the platform rejects it and the
# browser reports it as a CORS error, which tells the alum nothing. Capping well
# underneath is what makes our own message the one they actually see.
_SUBMIT_MAX_FIELD_BYTES = 40 * 1024
_SUBMIT_MAX_TOTAL_BYTES = 64 * 1024


def _oversized_submission(fields: dict[str, str]) -> JSONResponse | None:
    """A 413 for an over-cap submission, or ``None`` when it is within both caps.

    Sized in UTF-8 bytes over keys AND values, because that is what gets stored;
    counting characters would let a payload of astral-plane text be four times
    the size it declares. Unknown keys count too — the whitelist is applied
    downstream, and ten thousand junk keys is the same abuse as one huge value.

    The message never echoes any part of the payload back (the keys are
    submitter-chosen strings), and refusing here means nothing is staged.
    """
    total = 0
    for key, value in fields.items():
        size = len(key.encode()) + len(value.encode())
        if size > _SUBMIT_MAX_FIELD_BYTES:
            return _submission_too_large_response(
                "One of your answers is too long to save. "
                "Please shorten it and submit again."
            )
        total += size
    if total > _SUBMIT_MAX_TOTAL_BYTES:
        return _submission_too_large_response(
            "Your submission is too large to save. "
            "Please shorten your answers and submit again."
        )
    return None


def _submission_too_large_response(message: str) -> JSONResponse:
    """413 in the project error envelope, matching the upload caps' shape."""
    return JSONResponse(
        status_code=413,  # Content Too Large
        content={"error": {"code": "payload_too_large", "message": message}},
    )


@router.post(
    "/respond/{token}",
    response_model=SurveySubmitResult,
    dependencies=[Depends(SURVEY_SUBMIT_LIMITER)],
)
async def survey_submit(
    token: str, body: SurveySubmitRequest, session: SessionDep
) -> SurveySubmitResult | JSONResponse:
    """PUBLIC (token-gated): stage the alum's submitted changes for admin review.
    Nothing is applied to the record here.

    An over-cap payload is a 413 and stages nothing — see `_oversized_submission`.

    ALSO records a "yes, everything is correct" confirmation (#755): post
    `{"fields": {}, "confirmed_only": true}` and the alum's reply goes on record
    with nothing to review. Deliberately the SAME endpoint rather than a sibling
    of `/links` and `/photo`: those two carry a different KIND of thing (rows in
    another table, bytes in a bucket), while a confirmation is the same survey
    reply with an empty payload — same token check, same limiter budget, same
    caps, no second public write path to keep in step with this one.
    """
    too_large = _oversized_submission(body.fields)
    if too_large is not None:
        return too_large
    return await survey_responses.submit_response(
        session, token, body.fields, body.has_photo, body.confirmed_only
    )


@router.post(
    "/respond/{token}/links",
    response_model=OpportunityLinkSubmitResult,
    dependencies=[Depends(OPPORTUNITY_LINK_SUBMIT_LIMITER)],
)
async def survey_submit_opportunity_links(
    token: str, body: OpportunityLinkSubmitRequest, session: SessionDep
) -> OpportunityLinkSubmitResult:
    """PUBLIC (token-gated, no login): stage the alum's internship / job links
    for staff review (#441). Every link lands PENDING.

    A SEPARATE call from `POST /respond/{token}` on purpose, not an extra key in
    that body. The field submit is built on "one survey question maps to one
    database column" — its payload keys are literally `table.column` — and an
    opportunity has a url, a location, a role type, a deadline and a description
    of its own, several per alum. It gets its own table, its own write path, and
    its own moderation, and it does NOT enter the survey field whitelist or the
    response review queue.

    THE URL IS PUBLIC INPUT RENDERED AS AN HREF TO A SIGNED-IN STAFF MEMBER. It
    is validated on THIS path, server-side, by the same function the staff create
    route uses — see `app/schemas/opportunity_link.validate_opportunity_url` for
    what scheme gating does and does not defend, and why the later human approval
    is a governance control rather than a filter that catches phishing.

    A bad field is a 422 for the whole batch, not a silently dropped value: unlike
    the field whitelist there is no existing good value to protect here, and an
    alum at their keyboard can fix what they are told about.
    """
    try:
        return await opportunity_links.submit_links(session, token, body)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


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
    The signed token gates it (no login).

    ⚠️ THIS IS THE ONLY GENUINELY UNTRUSTED UPLOADER IN THE SYSTEM — a stranger
    holding a mailed link, with no account and no staff review before the bytes
    land. So the image is NORMALISED here (decoded and re-encoded as our own
    JPEG) rather than merely inspected, and what reaches the bucket is our
    output. `apply_response` normalises again when it promotes the photo onto the
    real profile; that repetition is deliberate — see the comment there.

    The photo only becomes the alum's headshot if an admin applies the response."""
    content_type = (photo.content_type or "").split(";")[0].strip().lower()
    if content_type not in _HEADSHOT_MIME_TYPES:
        raise InvalidRequestError("Photo must be a JPEG, PNG, or WebP image.")
    # ⚠️ THE CAP MATTERS MORE NOW, NOT LESS. Everything past this line DECODES the
    # bytes, so this is what bounds how much attacker-chosen input a single
    # request can hand to Pillow. It must stay ahead of the normalise call.
    data = await _read_capped(photo, _HEADSHOT_MAX_BYTES)
    if data is None:
        return _too_large_response(_HEADSHOT_MAX_BYTES)
    # `normalise_headshot` would reject an empty body too, but "the uploaded image
    # is empty" tells a survey respondent which of their two problems they have —
    # a browser that submitted an empty file part is a different fix from a photo
    # we could not read.
    if not data:
        raise InvalidRequestError("The uploaded image is empty.")
    # A cheap magic-byte gate kept BEFORE the decode, for one reason only: it
    # bounds which Pillow decoder hostile bytes can reach to the three formats we
    # actually accept, instead of every plugin Pillow ships.
    #
    # What is deliberately GONE is the old declared-type-vs-sniffed-type
    # comparison. That check existed because we used to store the uploader's own
    # bytes under the uploader's own label, so the two had to agree. We now store
    # OUR JPEG under `image/jpeg` whatever arrived, so the comparison guards
    # nothing — while still refusing a real photo whose browser mislabelled it,
    # which is a live failure mode (iOS HEIC-to-JPEG conversion, a .png that is
    # really a JPEG). The decode below is a strictly stronger test than either
    # sniff: the prefix sniff PASSES a JPEG with an HTML payload appended, which
    # is the whole reason `services/images.py` exists.
    if _sniff_image_mime(data) is None:
        raise InvalidRequestError("File content is not a JPEG, PNG, or WebP image.")
    # Re-encode BEFORE staging so hostile bytes never reach the bucket at all,
    # rather than sitting in it until a reviewer approves them. `InvalidRequestError`
    # maps to 422 with a client-safe message (see `main.invalid_request_handler`),
    # and `images.py` is careful that the message never echoes the uploaded bytes.
    data = normalise_headshot(data)
    # ⚠️ The recorded type must be the type we actually WROTE. `normalise_headshot`
    # always emits JPEG, so passing the uploader's declared type here would label a
    # PNG upload `image/png` while the stored object is a JPEG — and that label is
    # what the bucket serves the preview and the promoted headshot with.
    await survey_responses.stage_photo(
        session, token, survey_response_id, data, "image/jpeg"
    )
    return Response(status_code=204)


@router.get(
    "/campaigns/{grad_year}/responses",
    response_model=list[SurveyResponseItem],
)
async def survey_pending_responses(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireSurveysManage,
    session: SessionDep,
) -> list[SurveyResponseItem]:
    """Admin review queue: pending responses for a grad year, each with a diff."""
    return await survey_responses.list_pending(session, grad_year)


@router.post("/responses/{response_id}/apply", response_model=SurveyApplyResult)
async def survey_apply_response(
    response_id: int, user: RequireSurveysManage, session: SessionDep
) -> SurveyApplyResult:
    """Apply a staged response to the alum's record.

    Returns any soft duplicate warnings the apply raised (#646). A survey can now
    change an alum's name, and a rename can collide with an existing record —
    the same fuzzy first + last + graduation-year check the staff rename path
    runs (#627). It NEVER blocks: the write has already happened by the time this
    returns, exactly as on that path.

    `photo_dropped` says a staged photo could not be decoded and was discarded
    while the field changes went through. The UI MUST show it: otherwise the
    reviewer approves a submission that plainly carried a photo and never learns
    the profile still has the old one.

    Was a bodyless 204 before #646.
    """
    outcome = await survey_responses.apply_response(session, response_id, user.user_id)
    return SurveyApplyResult(
        duplicate_warnings=outcome.duplicate_warnings,
        photo_dropped=outcome.photo_dropped,
    )


@router.post("/responses/{response_id}/reject", status_code=204)
async def survey_reject_response(
    response_id: int, user: RequireSurveysManage, session: SessionDep
) -> None:
    """Reject a staged response — nothing is written to the record."""
    await survey_responses.reject_response(session, response_id, user.user_id)


@router.get("/graduation-years", response_model=list[GraduationYearCount])
async def survey_graduation_years(
    user: RequireSurveysManage, session: SessionDep
) -> list[GraduationYearCount]:
    """Distinct graduation years present in the DB (eligible alumni) + counts,
    newest first — powers the console's year picker."""
    return await survey_email.list_graduation_years(session)


@router.get("/usage", response_model=SurveyUsage)
async def survey_send_usage(
    user: RequireSurveysManage, session: SessionDep
) -> SurveyUsage:
    """Real Resend send usage (emails actually sent today / this calendar month),
    for the console's daily/monthly tallies against the send caps."""
    return await survey_email.get_send_usage(session)


@router.get("/send-config", response_model=SurveySendConfigItem)
async def get_survey_send_config(
    user: RequireSurveysManage, session: SessionDep
) -> SurveySendConfigItem:
    """The account-wide send cap the scheduler paces against — the daily/monthly
    email budget and whether it's enforced."""
    return await survey_schedule.get_send_config(session)


@router.post("/send-config", response_model=SurveySendConfigItem)
async def update_survey_send_config(
    body: SurveySendConfigUpdateRequest,
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
    session: SessionDep,
    dry_run: Annotated[bool, Query()] = True,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> SurveySendResult:
    """Send the survey to a graduation year now (dry run by default).

    A REAL send to a year that has NO campaign CREATES one (#405), anchored to
    the day the initial email actually went out, and says so via
    `campaign_created`. The campaign is what the daily cron runs the day-0/+7/+14
    cadence from — without it the manual send delivered the initial and the two
    reminders silently never fired, with nothing in the UI saying so. It cannot
    re-send the initial: the campaign opens on the very cycle this send claimed
    under, so those send-log rows are already its stage 0.

    A dry run creates nothing (it claims nothing, and the creation is driven by
    what is in `survey_send_log`). An existing campaign is never replaced —
    changing a campaign's start date is `POST /schedules`, and re-running the
    year is `POST /schedules/{year}/new-cycle`."""
    return await survey_email.send_campaign(
        session,
        graduation_year=grad_year,
        actor_user_id=user.user_id,
        dry_run=dry_run,
        limit=limit,
    )


@router.get(
    "/campaigns/{grad_year}/recipients",
    response_model=SurveyRecipientBreakdown,
)
async def survey_recipient_breakdown(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireSurveysManage,
    session: SessionDep,
) -> SurveyRecipientBreakdown:
    """Who this year's survey would reach, and who it would not (#392).

    The console's send confirmation reads THIS rather than doing its own
    arithmetic on the year picker's totals. That arithmetic
    (``total_alumni - responded``) ignored suppression, unreachable alumni and
    the shared-address dedupe, so the button promised a number the send could not
    deliver — the parity bug this codebase keeps re-growing.

    The same function backs `SurveySendResult.breakdown`, so the figure shown
    before a send and the figure explaining it afterwards cannot disagree.

    Read-only, sends nothing, takes no send lock — safe to poll while the daily
    cron is mid-run. Gated like the rest of the console.
    """
    return await survey_email.recipient_breakdown(session, grad_year)


@router.get(
    "/campaigns/{grad_year}/unreachable",
    response_model=list[SurveyUnreachableAlum],
)
async def list_survey_unreachable(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireSurveysManage,
    session: SessionDep,
) -> list[SurveyUnreachableAlum]:
    """The alumni this year's survey CANNOT email, by name (#392).

    ``SurveyRecipientBreakdown.unreachable`` is this set as a count; this is the
    worklist behind it, so "we can't reach 20 of them" becomes something staff
    can act on. Each row says WHY and shows whatever is in the two email columns,
    because a typo'd work address is fixable on sight while a wholly missing one
    has to be chased.

    Campaign-scoped, NOT schedule-scoped: a year with no schedule still has a
    contact-data gap worth seeing, so this never 404s — an empty list means
    everyone is reachable.

    Contains no suppressed alumni. Deceased / Do Not Contact are excluded from
    the campaign by decision, not by a gap, and must never be presented as people
    to chase for an address.

    Read-only and gated like the rest of the console (it returns alumni contact
    details).
    """
    return await survey_email.list_unreachable(session, grad_year)


@router.get(
    "/campaigns/{grad_year}/held-out",
    response_model=SurveyHeldOutPage,
)
async def list_survey_held_out(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireEngineer,
    session: SessionDep,
    reason: Annotated[
        Literal["suppressed", "already_responded", "unreachable"] | None, Query()
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=survey_email.HELD_OUT_PAGE_MAX)
    ] = survey_email.HELD_OUT_PAGE_DEFAULT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> SurveyHeldOutPage:
    """WHO this year's send is holding out, and why (#658).

    `SurveyRecipientBreakdown` gives the three exclusions as counts; this expands
    them into people. It was written for one specific dead end: a campaign was
    deleted and re-sent, the console said "1 already replied within the last
    year", and there was no way to find out who. The behaviour was right —
    retiring a cycle deliberately does not clear the 365-day annual window for
    alumni who actually ANSWERED — but a "1" nobody can expand is not something
    an operator can act on, so the cohort was searched by name until she turned
    up.

    `already_responded` rows carry `last_reply_at`, which is the fact the
    decision turns on: a reply from three months ago is a reason to leave someone
    alone; one that predates a retired campaign may not be. The way to act on it
    is `GET /survey/alumni/{alumni_id}/state` and then, if warranted, `POST
    /survey/alumni/{alumni_id}/reset` — this endpoint changes nothing itself.

    `reason` narrows to one bucket; omit it for all three. `total` is always the
    size of the FULL filtered set, so it can be checked against the matching
    breakdown count — both come from the same predicates and are supposed to be
    identical. Paged (default 200, max 1000) because the responded bucket grows
    for the life of a campaign.

    ENGINEER-GATED (`RequireEngineer`), matching the state/reset pair it exists to
    inform rather than the console's assignable `surveys.manage`. It names alumni
    who replied and when — and it is read as the first half of a decision about
    who receives a real email, exactly like `GET /survey/alumni/{id}/state`, whose
    gate is narrowed for that same reason.

    AUDITED (#422): naming alumni and dating their replies is a disclosure, so
    the read writes a `read_survey_held_out` row recording who asked and for which
    year/bucket/page — never any of the people it returned.
    """
    page = await survey_email.list_held_out(
        session, grad_year, reason=reason, limit=limit, offset=offset
    )
    # Logged AFTER the read so the row only ever records a disclosure that
    # actually happened; a request that raised disclosed nothing to account for.
    await _log_survey_read(
        session,
        actor_user_id=user.user_id,
        action="read_survey_held_out",
        entity_type="survey_campaign",
        entity_id=grad_year,
        scope=(
            f"graduation_year={grad_year}; reason={reason or 'all'}; "
            f"limit={limit}; offset={offset}"
        ),
    )
    return page


# --------------------------------------------------------------- scheduler ----


@router.get("/schedules", response_model=list[SurveyScheduleItem])
async def list_survey_schedules(
    user: RequireSurveysManage, session: SessionDep
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
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
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
    user: RequireSurveysManage,
    session: SessionDep,
) -> SurveyScheduleItem:
    """Cancel a graduation year's schedule — no further sends.

    Terminal, and non-destructive: the row stays with its `cycle_seq`, next to
    the send log it explains. This is what a campaign that has already emailed
    people gets instead of `DELETE` below (#398). Audited."""
    item = await survey_schedule.cancel_schedule(
        session, grad_year, actor_user_id=user.user_id
    )
    if item is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return item


@router.delete("/schedules/{grad_year}", response_model=SurveyScheduleDeleteResult)
async def delete_survey_schedule(
    grad_year: Annotated[int, Path(ge=_GRAD_YEAR_MIN, le=_GRAD_YEAR_MAX)],
    user: RequireEngineer,
    session: SessionDep,
) -> SurveyScheduleDeleteResult:
    """Remove a survey campaign — ANY campaign, whatever its status (#398).

    For the campaign scheduled against the wrong year, or created by mistake:
    pausing hid it, but the row stayed forever. This removes it, and with it any
    future send (the cron only ever selects rows that exist). No status is
    exempt — `scheduled`, `active`, `paused`, `completed` and `cancelled` all
    delete. The first cut refused any campaign that had ever emailed anyone,
    which in practice meant every real one.

    DELETES NO HISTORY. `survey_send_log` and `survey_responses` are not touched
    here or anywhere in this path — a "delete campaign" that took the alumni's
    submitted answers with it is precisely what Jake ruled out on #395 the same
    day. The response says how many of each were kept.

    What it does instead of refusing: RETIRES the campaign's cycle. The deleted
    row's `cycle_seq` is recorded in `survey_campaign_retirement`, and the next
    campaign for that year starts above it — so the alumni this one emailed are
    eligible again, and the send log's unique key cannot refuse their new rows.
    Without that, deleting the row would leave the send-log rows looking like the
    current cycle's and the next campaign would find everyone already emailed and
    send to nobody (#357). Alumni who ANSWERED stay held out by the 365-day
    annual window, exactly as after a new cycle.

    `POST /schedules/{year}/cancel` is still here and still distinct: it stops a
    live campaign and KEEPS it listed with its counts.

    Engineer-gated like the other maintenance controls (pause-all / cancel-all /
    per-alumnus reset) rather than `surveys.manage`, which is assignable."""
    result = await survey_schedule.delete_schedule(
        session, grad_year, actor_user_id=user.user_id
    )
    if result is None:
        raise NotFoundError("No schedule exists for that graduation year.")
    return result


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


# ------------------------------------------- per-alumnus campaign reset -------


@router.get(
    "/alumni/{alumni_id}/state",
    response_model=SurveyAlumniState,
)
async def survey_alumnus_state(
    alumni_id: int,
    user: RequireEngineer,
    session: SessionDep,
) -> SurveyAlumniState:
    """One alumnus's survey state: what was emailed, what came back, and what is
    holding them out of the next send (#395).

    Read-only, and the REQUIRED first half of the reset below — the engineer has
    to be able to see that someone looks "blocked" only because they legitimately
    replied three months ago, in which case re-asking them may not be wanted.
    `blocked_reasons` says so in plain words; empty means a reset would change
    nothing at all.

    Engineer-gated (`RequireEngineer` = the non-assignable `engineer`
    capability), matching its twin below: the read exists to inform that one
    decision, so widening it would only invite the reset to be run blind.

    AUDITED (#422): this is a named alumnus's survey history, so the read writes a
    `read_survey_alumni_state` row saying who looked at whose record — the reset
    below was already audited, and the read that justifies it now is too. The row
    carries the alumni_id and nothing the response returned.
    """
    state = await survey_reset.get_state(session, alumni_id)
    # After the read: a 404 for an alumnus who does not exist disclosed nothing.
    await _log_survey_read(
        session,
        actor_user_id=user.user_id,
        action="read_survey_alumni_state",
        entity_type="alumni",
        entity_id=alumni_id,
    )
    return state


@router.post("/alumni/{alumni_id}/reset", response_model=SurveyResetResult)
async def survey_reset_alumnus(
    alumni_id: int,
    user: RequireEngineer,
    session: SessionDep,
) -> SurveyResetResult:
    """Make ONE alumnus surveyable again (#395) — the UI replacement for
    hand-running DELETE statements, which now deletes nothing itself.

    DESTROYS NOTHING (Jake, 2026-08-05). It records a reset in
    `survey_reset_log`; their submitted answers, the record of the emails sent to
    them, and any staged survey photo all stay in the database and on their
    profile. A `pending` answer stays pending and stays in the review queue.
    Eligibility queries stop counting what predates the reset — that is the
    entire effect. Callers must show `GET /survey/alumni/{alumni_id}/state`
    first, because a reset that unblocks nothing is simply noise.

    Gated on `RequireEngineer` — the `engineer` capability, which is the one
    capability the permission editor cannot grant to another role. Deliberately
    NOT `surveys.manage`: that capability IS assignable, and this button decides
    who receives a real email, so it stays with the maintenance controls
    (pause-all / cancel-all) rather than with response review.

    Scoped to exactly one alumnus. There is no bulk or cohort variant; the annual
    cohort re-run is `POST /schedules/{grad_year}/new-cycle`.
    """
    return await survey_reset.reset_alumnus(
        session, alumni_id, actor_user_id=user.user_id
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

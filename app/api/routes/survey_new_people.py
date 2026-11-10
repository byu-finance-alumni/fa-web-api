"""The new-people survey (api#661): a shared link for people not in the database.

PUBLIC (no login, rate-limited, ``Cache-Control: no-store``):

* ``GET  /survey/new/{key}``  -- the form's required fields and support contact.
* ``POST /survey/new/{key}``  -- HOLD a submission for staff review.

``{key}`` is the shared link's secret. A wrong key and a switched-off link both
404 with the same message. Nothing a submission says reaches ``alumni`` until a
reviewer approves it.

STAFF (``surveys.manage``, the same capability as the survey review queue):

* ``GET  /survey/new-people/link``           -- the link (created disabled if none)
* ``PUT  /survey/new-people/link``           -- switch it on or off
* ``POST /survey/new-people/link/reissue``   -- new key; the old one dies at once
* ``GET  /survey/new-people/submissions``    -- pending, oldest first
* ``GET  /survey/new-people/submissions/{id}/preview?alumni_id=`` -- merge diff
* ``POST /survey/new-people/submissions/reject`` -- bulk reject
* ``POST /survey/new-people/submissions/{id}/approve``
* ``POST /survey/new-people/submissions/{id}/reject``
* ``POST /survey/new-people/invite``         -- email the link to pasted addresses
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireSurveysManage
from app.api.routes.survey import _log_survey_read, _oversized_submission
from app.core.database import get_session
from app.core.rate_limit import (
    NEW_PEOPLE_READ_LIMITER,
    NEW_PEOPLE_SUBMIT_LIMITER,
    NewPeopleInviteRateLimit,
    SurveysBrowseReadRateLimit,
)
from app.schemas.new_people import (
    NewPeopleBulkRejectRequest,
    NewPeopleBulkRejectResult,
    NewPeopleInviteRequest,
    NewPeopleInviteResult,
    NewPeopleLinkRead,
    NewPeopleLinkUpdate,
    NewPeopleSurveyInfo,
    NewPersonApproveRequest,
    NewPersonApproveResult,
    NewPersonMergePreview,
    NewPersonSubmissionItem,
    NewPersonSubmitRequest,
    NewPersonSubmitResult,
)
from app.services import new_people_invites, new_people_survey, survey_email

SessionDep = Annotated[AsyncSession, Depends(get_session)]

router = APIRouter(prefix="/survey", tags=["survey"])

log = logging.getLogger(__name__)


# ----------------------------------------------------------------- public ----


@router.get(
    "/new/{key}",
    response_model=NewPeopleSurveyInfo,
    dependencies=[Depends(NEW_PEOPLE_READ_LIMITER)],
)
async def new_people_survey_info(
    key: str, session: SessionDep, response: Response
) -> NewPeopleSurveyInfo:
    """PUBLIC: the rules the form renders from. No PII: the required field keys
    and the one support contact the normal survey already shows."""
    response.headers["Cache-Control"] = "no-store"
    await new_people_survey.verify_key(session, key)
    return NewPeopleSurveyInfo(
        required_fields=new_people_survey.required_fields(),
        support_contact=await survey_email.survey_support_contact(session),
    )


def _no_store(resp: JSONResponse) -> JSONResponse:
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.post(
    "/new/{key}",
    response_model=NewPersonSubmitResult,
    dependencies=[Depends(NEW_PEOPLE_SUBMIT_LIMITER)],
)
async def new_people_survey_submit(
    key: str, body: NewPersonSubmitRequest, session: SessionDep, response: Response
) -> NewPersonSubmitResult | JSONResponse:
    """PUBLIC: hold a submission for review. Nothing is added to the database.

    * Wrong key or link off: 404, the same message for both.
    * Honeypot (``website``) filled: 200 ``{"received": true}`` and NOTHING is
      stored, so a bot learns nothing from the response.
    * Over the survey's size caps: 413, nothing stored.
    * A required group unanswered (or answered with a value the survey refuses):
      422 naming the field keys.
    * Holding queue full: 429, nothing stored.
    """
    response.headers["Cache-Control"] = "no-store"
    await new_people_survey.verify_key(session, key)
    if body.website.strip():
        # No value, no IP: just that it happened.
        log.info("new-people survey: honeypot submission dropped")
        return NewPersonSubmitResult()
    too_large = _oversized_submission(body.fields)
    if too_large is not None:
        return _no_store(too_large)
    try:
        await new_people_survey.submit(
            session,
            kind=body.kind,
            fields=body.fields,
            comments=body.comments,
            fill_seconds=body.fill_seconds,
        )
    except new_people_survey.MissingRequiredFields as exc:
        return _no_store(
            JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "validation_error",
                        "message": "Please answer the required questions.",
                        "fields": exc.fields,
                    }
                },
            )
        )
    except new_people_survey.PendingCapReached:
        return _no_store(
            JSONResponse(
                status_code=429,
                content={
                    "error": {
                        "code": "rate_limited",
                        "message": new_people_survey.PENDING_CAP_MESSAGE,
                    }
                },
            )
        )
    return NewPersonSubmitResult()


# ------------------------------------------------------------------ staff ----


@router.get("/new-people/link", response_model=NewPeopleLinkRead)
async def get_new_people_link(
    user: RequireSurveysManage, session: SessionDep
) -> NewPeopleLinkRead:
    """The shared link. Created, switched OFF, the first time it is read."""
    return await new_people_survey.get_link(session, user.user_id)


@router.put("/new-people/link", response_model=NewPeopleLinkRead)
async def update_new_people_link(
    body: NewPeopleLinkUpdate, user: RequireSurveysManage, session: SessionDep
) -> NewPeopleLinkRead:
    """Switch the link on or off. Audited (never the key)."""
    return await new_people_survey.set_enabled(session, body.enabled, user.user_id)


@router.post("/new-people/link/reissue", response_model=NewPeopleLinkRead)
async def reissue_new_people_link(
    user: RequireSurveysManage, session: SessionDep
) -> NewPeopleLinkRead:
    """A new key. The old URL stops working immediately; on/off is unchanged."""
    return await new_people_survey.reissue(session, user.user_id)


@router.get("/new-people/submissions", response_model=list[NewPersonSubmissionItem])
async def list_new_people_submissions(
    user: SurveysBrowseReadRateLimit, session: SessionDep
) -> list[NewPersonSubmissionItem]:
    """Pending submissions, oldest first, with possible matches. Audit-logged
    like the survey review queue (the rows carry submitted PII)."""
    items = await new_people_survey.list_pending(session)
    await _log_survey_read(
        session,
        actor_user_id=user.user_id,
        action="read_new_people_submissions",
        entity_type="new_people_submission",
        scope=f"pending; rows={len(items)}",
    )
    return items


@router.post("/new-people/submissions/reject", response_model=NewPeopleBulkRejectResult)
async def bulk_reject_new_people_submissions(
    body: NewPeopleBulkRejectRequest, user: RequireSurveysManage, session: SessionDep
) -> NewPeopleBulkRejectResult:
    """Reject many at once (1-500 ids). Each pending one is purged like a single
    reject; ids that are not pending are skipped. One audit row with the count.

    No path collision with ``/{submission_id}/reject``: that one has an extra
    segment, so ``/submissions/reject`` can never be read as an id."""
    rejected = await new_people_survey.reject_many(session, body.submission_ids, user.user_id)
    return NewPeopleBulkRejectResult(rejected=rejected)


@router.get(
    "/new-people/submissions/{submission_id}/preview",
    response_model=NewPersonMergePreview,
)
async def preview_new_people_merge(
    submission_id: int,
    alumni_id: Annotated[int, Query()],
    user: SurveysBrowseReadRateLimit,
    session: SessionDep,
) -> NewPersonMergePreview:
    """What a MERGE into ``alumni_id`` would change, and which of those changes
    would replace populated data. 404 when the submission is not pending or the
    record does not exist or is archived. Audit-logged: it shows the record's
    current values."""
    preview = await new_people_survey.preview_merge(session, submission_id, alumni_id)
    await _log_survey_read(
        session,
        actor_user_id=user.user_id,
        action="preview_new_person_merge",
        entity_type="alumni",
        entity_id=alumni_id,
        scope=f"new_people_submission={submission_id}; changes={len(preview.changes)}",
    )
    return preview


@router.post(
    "/new-people/submissions/{submission_id}/approve",
    response_model=NewPersonApproveResult,
)
async def approve_new_people_submission(
    submission_id: int,
    body: NewPersonApproveRequest,
    user: RequireSurveysManage,
    session: SessionDep,
) -> NewPersonApproveResult:
    """Create a record from the submission, or apply it to an existing one.

    404 when it is not pending; 409 when ``submitted_at`` does not match the
    row (it changed since the reviewer opened it)."""
    return await new_people_survey.approve(session, submission_id, body, user.user_id)


@router.post("/new-people/submissions/{submission_id}/reject", status_code=204)
async def reject_new_people_submission(
    submission_id: int, user: RequireSurveysManage, session: SessionDep
) -> Response:
    """Refuse it. The held answers are purged; an audit row (no PII) remains."""
    await new_people_survey.reject(session, submission_id, user.user_id)
    return Response(status_code=204)


@router.post("/new-people/invite", response_model=NewPeopleInviteResult)
async def invite_new_people(
    body: NewPeopleInviteRequest, user: NewPeopleInviteRateLimit, session: SessionDep
) -> NewPeopleInviteResult:
    """Email the shared survey link to the pasted addresses (one email each).

    * 422: nothing to send to, or more than 50 unique addresses.
    * 409: the link is off; the send does not fit the shared Resend budget
      (nothing is sent, never a partial send); or a survey send holds the
      shared send lock.
    * 502: Resend did not accept the batch. Nothing was sent and nothing is
      recorded.
    * 200 with ``sent=0`` when every address was skipped.

    One audit row per send with the COUNTS only, never an address."""
    return await new_people_invites.send_invites(session, body.emails, user.user_id)

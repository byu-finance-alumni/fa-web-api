"""Opportunity-link routes — the staff "Links" tab and its moderation queue (#441).

Alumni submit internship / job links through the public survey; staff work the
resulting list here. There is NO public or student-facing surface — the owner's
decision on #441 was explicit that distribution stays manual, so nothing in this
module is reachable without a login.

AUTHORIZATION, and why it is drawn where it is:

  * **Reads** require any view-access role (``view``), like ``GET /notes`` — the
    list is a staff working surface, not a PII export.
  * …but ONLY over ``approved`` links. Asking for ``pending`` or ``rejected``
    rows additionally requires ``surveys.manage``. A pending link is unmoderated,
    attacker-supplied text with a clickable URL in it; the set of people who may
    look at that is exactly the set of people who moderate it, not everyone with
    a login. A caller without the capability who asks for another status gets a
    403 rather than a silently narrowed result — a filter that quietly does
    something else is worse than a refusal.
  * **Writes and moderation** require ``surveys.manage``.
  * **DELETION requires ``links.delete``** — its own capability, and NOT held by
    full_access. See below.

``surveys.manage`` covers creating, editing, approving and rejecting, on purpose:
these links arrive through the survey and moderating one is the same job as
reviewing a survey response, held by the same people (super_admin, full_access,
and the engineer override).

DELETION IS THE ONE EXCEPTION, and the line is drawn by REVERSIBILITY, not by
endpoint. Rejecting a link takes it out of circulation and can be undone — the
row survives, and that row is the record that we once saw the thing. Deleting
destroys it, and all that is left is the audit snapshot. Those are different
levels of trust, so they are different capabilities: ``links.delete`` defaults to
super_admin + the engineer override only, and a full_access moderator working the
queue every day can approve and reject all they like without being able to erase
from it. ONE rule covers both delete routes (single and bulk) so there is a
single answer to "who can remove a link", never two that can drift apart.

The PUBLIC submit route is NOT here — it lives on the survey router as
``POST /survey/respond/{token}/links``, next to the other token-gated respond
routes and under the same style of rate limiter.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    PermissionConfig,
    RequireLinksDelete,
    RequireSurveysManage,
    RequireViewAccess,
)
from app.api.params import IdPath
from app.core.capabilities import Capability, effective_capabilities
from app.core.database import get_session
from app.core.errors import InvalidRequestError
from app.core.security import AuthorizationError
from app.schemas.auth import UserContext
from app.schemas.opportunity_link import (
    LinkStatus,
    OpportunityLinkBulkDeleteRequest,
    OpportunityLinkBulkDeleteResult,
    OpportunityLinkCreate,
    OpportunityLinkPage,
    OpportunityLinkRead,
    OpportunityLinkUpdate,
    RoleType,
)
from app.services import opportunity_links as service

router = APIRouter(prefix="/opportunity-links", tags=["opportunity-links"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _may_moderate(user: UserContext, config: dict[str, frozenset[str]]) -> bool:
    """True if the caller holds ``surveys.manage`` under the LIVE permission
    config — i.e. may see and act on unmoderated links."""
    return Capability.SURVEYS_MANAGE in effective_capabilities(config, user.roles)


@router.get("", response_model=OpportunityLinkPage)
async def list_opportunity_links(
    user: RequireViewAccess,
    config: PermissionConfig,
    session: SessionDep,
    status_filter: Annotated[
        LinkStatus | None,
        Query(
            alias="status",
            description=(
                "Moderation state. Omit for approved links only. "
                "'pending' / 'rejected' require the surveys.manage capability."
            ),
        ),
    ] = None,
    role_type: Annotated[
        RoleType | None, Query(description="Internship / full-time / both.")
    ] = None,
    company: Annotated[
        str | None,
        Query(
            max_length=200,
            description=(
                "Substring match on the company the link is listed under — the "
                "typed name, or the alum's employer for 'my company' entries."
            ),
        ),
    ] = None,
    q: Annotated[
        str | None,
        Query(
            max_length=200,
            description="Free-text search over company, details, location and url.",
        ),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OpportunityLinkPage:
    """The staff Links tab: filtered, paginated, newest first.

    DEFAULTS TO APPROVED. An unfiltered read is the safe read: a caller who does
    not ask for unmoderated rows does not get them, whatever their role. A
    moderator asking for the queue passes ``?status=pending``.
    """
    wants = status_filter or "approved"
    if wants != "approved" and not _may_moderate(user, config):
        # Explicit 403, not a silent narrowing — see the module docstring.
        raise AuthorizationError()
    return await service.list_links(
        session,
        status=wants,
        role_type=role_type,
        company=company,
        search=q,
        limit=limit,
        offset=offset,
    )


@router.get("/{link_id}", response_model=OpportunityLinkRead)
async def get_opportunity_link(
    link_id: IdPath,
    user: RequireViewAccess,
    config: PermissionConfig,
    session: SessionDep,
) -> OpportunityLinkRead:
    """One link. 404 if it does not exist; 403 if it is unmoderated and the
    caller may not moderate — the same boundary the list draws, enforced here too
    so a direct id fetch is not a way around the status gate."""
    link = await service.get_link(session, link_id)
    if link.status != "approved" and not _may_moderate(user, config):
        raise AuthorizationError()
    return link


@router.post("", response_model=OpportunityLinkRead, status_code=status.HTTP_201_CREATED)
async def create_opportunity_link(
    payload: OpportunityLinkCreate,
    user: RequireSurveysManage,
    session: SessionDep,
) -> OpportunityLinkRead:
    """Staff manual entry (surveys.manage). Lands APPROVED — the staff member
    typing it in is the review. 404 if the alumnus does not exist, 422 if any
    field fails the shared validation rules."""
    try:
        return await service.create_link(session, payload, actor_user_id=user.user_id)
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


@router.patch("/{link_id}", response_model=OpportunityLinkRead)
async def update_opportunity_link(
    link_id: IdPath,
    payload: OpportunityLinkUpdate,
    user: RequireSurveysManage,
    session: SessionDep,
) -> OpportunityLinkRead:
    """Edit a link (surveys.manage). Only the keys present in the body change,
    and the merged row is re-validated as a whole. Does NOT change the moderation
    status — fixing a typo in a pending link must not approve it."""
    try:
        return await service.update_link(
            session, link_id, payload, actor_user_id=user.user_id
        )
    except ValueError as exc:
        raise InvalidRequestError(str(exc)) from exc


@router.post("/bulk-delete", response_model=OpportunityLinkBulkDeleteResult)
async def bulk_delete_opportunity_links(
    payload: OpportunityLinkBulkDeleteRequest,
    user: RequireLinksDelete,
    session: SessionDep,
) -> OpportunityLinkBulkDeleteResult:
    """Delete the links a staff member multi-selected (``links.delete``).

    POST rather than ``DELETE`` with a body: a request body on DELETE is legal
    but under-specified, and proxies and clients are entitled to drop it — which
    would turn "delete these three" into a malformed request, or worse, silence.

    BEST-EFFORT, and the response says exactly what happened. Every id that
    resolves is deleted; ids that no longer resolve come back in ``missing_ids``
    instead of failing the batch, because the commonest reason an id is stale is
    that the row is already gone. It is still ONE transaction — the rows and
    their audit snapshots commit together. See ``OpportunityLinkBulkDeleteResult``
    for the full reasoning, and note that a caller wanting strict all-or-nothing
    can get it by checking ``missing_ids`` is empty.

    Each deleted row is snapshotted to the audit trail individually, exactly as
    the single delete does, so a bulk delete is reconstructible row by row.

    422 if the list is empty, contains a non-positive id, or exceeds
    ``MAX_LINKS_PER_BULK_DELETE`` — the cap is what stops one call from being an
    unbounded row-destruction primitive.
    """
    deleted_ids, missing_ids = await service.delete_links(
        session, payload.opportunity_link_ids, actor_user_id=user.user_id
    )
    return OpportunityLinkBulkDeleteResult(
        requested=len(set(payload.opportunity_link_ids)),
        deleted_ids=deleted_ids,
        missing_ids=missing_ids,
    )


@router.delete("/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_opportunity_link(
    link_id: IdPath,
    user: RequireLinksDelete,
    session: SessionDep,
) -> None:
    """Delete one link (``links.delete``, NOT ``surveys.manage``).

    Re-gated onto the same capability the bulk route uses, so deletion has one
    rule rather than two: it would be indefensible for a role to be refused the
    multi-select and then be able to loop this endpoint to the same effect.
    Snapshotted to the audit trail first.
    """
    await service.delete_link(session, link_id, actor_user_id=user.user_id)


@router.post("/{link_id}/approve", response_model=OpportunityLinkRead)
async def approve_opportunity_link(
    link_id: IdPath,
    user: RequireSurveysManage,
    session: SessionDep,
) -> OpportunityLinkRead:
    """Approve a link (surveys.manage), stamping the reviewer and the time.

    ⚠️ This records that a named person took responsibility for the link. It is
    NOT a check that the URL is safe: scheme gating on the write path stops
    ``javascript:``, but no automated rule — and no human eyeballing a queue —
    reliably distinguishes a real careers page from a phishing one. See
    ``app/schemas/opportunity_link.validate_opportunity_url``.
    """
    return await service.moderate_link(
        session, link_id, approve=True, actor_user_id=user.user_id
    )


@router.post("/{link_id}/reject", response_model=OpportunityLinkRead)
async def reject_opportunity_link(
    link_id: IdPath,
    user: RequireSurveysManage,
    session: SessionDep,
) -> OpportunityLinkRead:
    """Reject a link (surveys.manage). The row is kept — rejection is a decision
    worth having on the record, and it is reversible if it was a mistake."""
    return await service.moderate_link(
        session, link_id, approve=False, actor_user_id=user.user_id
    )

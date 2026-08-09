"""Stored-object maintenance jobs. One endpoint today: the headshot sweep cron.

Deliberately its OWN router rather than another route on ``/alumni``: nothing
here reads or writes the database, nothing here is reachable by a logged-in
user, and the authorization model is a shared secret rather than a capability.
Mixing it into the alumni routes would put a non-authenticated endpoint in the
middle of the most authorization-sensitive file in the app.

Both endpoints are ``include_in_schema=False``. No browser client calls them, so
keeping them out of the OpenAPI document keeps the generated frontend types
(``api.gen.ts``) unchanged — and CI's type-contract drift guard quiet — for a
change the frontend has no business knowing about.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.schemas.storage import HeadshotSweepSummary
from app.services import headshot_sweep

router = APIRouter(prefix="/storage", tags=["storage"])


async def _run_headshot_sweep(request: Request) -> HeadshotSweepSummary:
    """Headshot sweep cron core — NOT login-gated (Vercel Cron can't log in).

    Same contract as the survey scheduler cron (``/survey/cron/run``), on
    purpose: the request must carry ``Authorization: Bearer <CRON_SECRET>``,
    which Vercel Cron sends automatically when ``CRON_SECRET`` is set as a
    project env var. Any other (or absent) credential -> 401, and when
    ``CRON_SECRET`` is unset the endpoint rejects everything, so it is never open
    by default.

    That default-closed behaviour matters more here than it does for the survey
    cron: this endpoint REWRITES STORED PHOTOS. An unauthenticated caller who
    could trigger it would be able to spend the function's whole budget churning
    real alumni images.
    """
    expected = get_settings().cron_secret
    provided = request.headers.get("Authorization", "")
    if not expected or not hmac.compare_digest(provided, f"Bearer {expected}"):
        raise HTTPException(status_code=401, detail="Invalid cron credentials.")
    return await headshot_sweep.run_sweep()


@router.post(
    "/cron/headshot-sweep",
    response_model=HeadshotSweepSummary,
    include_in_schema=False,
)
async def headshot_sweep_cron(request: Request) -> HeadshotSweepSummary:
    """Run one bounded pass of the headshot sweep (POST). See
    :func:`_run_headshot_sweep`. POST exists so an engineer can trigger a run by
    hand with curl; Vercel Cron itself uses the GET below."""
    return await _run_headshot_sweep(request)


@router.get(
    "/cron/headshot-sweep",
    response_model=HeadshotSweepSummary,
    include_in_schema=False,
)
async def headshot_sweep_cron_get(request: Request) -> HeadshotSweepSummary:
    """GET variant — Vercel Cron invokes the path with a GET."""
    return await _run_headshot_sweep(request)

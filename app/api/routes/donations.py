"""Pay It Forward Fund donation routes (#161).

Field-level access (CRITICAL): donor IDENTITY (who gave, and in which years) is
readable by any view-access role, but dollar AMOUNTS are gated to ``full_access``
and up. The gate is enforced HERE, server-side — every amount field is nulled
before serialization for a caller without the ``alumni.full`` capability, so a
non-privileged client never receives a value (not merely a hidden one). Donation
notes are gated alongside amounts (free text may reference figures).

Writes are admin-tier: add / edit / bulk import are ``super_admin``-only;
DELETE is gated to ``full_access`` and up (the destructive-data-management tier,
matching event delete / alumni archive — broadened from super_admin in QA
hardening, H4).

Responses are assembled as plain dicts (no ``response_model``) so the amount
fields can be conditionally nulled per-caller — the same convention the events
routes use.
"""

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    PermissionConfig,
    RequireFullAccess,
    RequireSuperAdmin,
    RequireViewAccess,
)
from app.core.capabilities import Capability, effective_capabilities
from app.core.database import get_session
from app.core.errors import InvalidRequestError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.models.donation import Donation
from app.schemas.auth import UserContext
from app.schemas.donation import DonationCreate, DonationUpdate
from app.services import import_donations

router = APIRouter(prefix="/donations", tags=["donations"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _can_view_amounts(
    user: UserContext, config: dict[str, frozenset[str]]
) -> bool:
    """True if the caller may see dollar amounts: holds ``alumni.full`` under the
    live permission config (full_access, super_admin, engineer by default)."""
    return Capability.ALUMNI_FULL in effective_capabilities(config, user.roles)


def _alumni_name(first: str | None, preferred: str | None, last: str | None, alumni_id: int) -> str:
    name = " ".join(p for p in (preferred or first, last) if p).strip()
    return name or f"Alumni #{alumni_id}"


def _money(value, show: bool) -> float | None:
    """Serialize a money value: the float amount when *show*, else None (gated)."""
    if not show or value is None:
        return None
    return float(value)


# --- Donor list + summary (view access; amounts gated) -----------------------


@router.get("/donors")
async def list_donors(
    user: RequireViewAccess,
    config: PermissionConfig,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict:
    """List donors with per-year and lifetime roll-ups (view access, paginated).

    Everyone sees who gave and in which years; ``lifetime_total`` and each
    ``per_year.total`` are non-null only for amount-viewers (full_access+).

    Returns a ``{items, total, limit, offset}`` envelope. The ranking, LIMIT, and
    OFFSET are pushed into PostgreSQL; only the page's per-year breakdown is then
    aggregated (``WHERE alumni_id IN (<page ids>)``), so the endpoint is bounded
    regardless of donor count. Amount-viewers see the biggest givers first;
    others get a stable name sort (the lifetime ranking is amount-gated too)."""
    show = _can_view_amounts(user, config)

    total = await session.scalar(
        select(func.count(func.distinct(Donation.alumni_id)))
    )

    lifetime_total = func.coalesce(func.sum(Donation.amount), 0).label("lifetime_total")
    page_stmt = (
        select(
            Alumni.alumni_id,
            Alumni.first_name,
            Alumni.preferred_first_name,
            Alumni.last_name,
            Alumni.graduation_year,
            func.count(Donation.donation_id),
            lifetime_total,
        )
        .join(Donation, Donation.alumni_id == Alumni.alumni_id)
        .group_by(Alumni.alumni_id)
    )
    # Push the sort into SQL. Amount-viewers rank by lifetime giving (tie-broken by
    # name); non-viewers get a name-only sort so the giving ranking isn't leaked.
    if show:
        page_stmt = page_stmt.order_by(
            desc("lifetime_total"), Alumni.last_name, Alumni.first_name
        )
    else:
        page_stmt = page_stmt.order_by(Alumni.last_name, Alumni.first_name)
    base = (
        await session.execute(page_stmt.limit(limit).offset(offset))
    ).all()

    # Per-(alumnus, year) roll-up for the breakdown + the "years gave" list —
    # only for the page's donors so we never aggregate the whole donation table.
    page_ids = [row[0] for row in base]
    per_year_map: dict[int, list[tuple[int, Decimal]]] = {}
    if page_ids:
        year_rows = (
            await session.execute(
                select(
                    Donation.alumni_id,
                    Donation.donation_year,
                    func.coalesce(func.sum(Donation.amount), 0),
                )
                .where(Donation.alumni_id.in_(page_ids))
                .group_by(Donation.alumni_id, Donation.donation_year)
                .order_by(Donation.alumni_id, Donation.donation_year.desc())
            )
        ).all()
        for alumni_id, year, year_total in year_rows:
            per_year_map.setdefault(alumni_id, []).append((year, year_total))

    donors: list[dict] = []
    for alumni_id, first, preferred, last, grad, count, lifetime in base:
        years = per_year_map.get(alumni_id, [])
        donors.append(
            {
                "alumni_id": alumni_id,
                "name": _alumni_name(first, preferred, last, alumni_id),
                "graduation_year": grad,
                "donation_count": int(count),
                "years": [y for y, _ in years],
                "lifetime_total": _money(lifetime, show),
                "per_year": [
                    {"year": y, "total": _money(year_total, show)}
                    for y, year_total in years
                ],
            }
        )

    return {
        "items": donors,
        "total": int(total or 0),
        "limit": limit,
        "offset": offset,
    }


@router.get("/summary")
async def donations_summary(
    user: RequireViewAccess,
    config: PermissionConfig,
    session: SessionDep,
) -> dict:
    """Fund totals (view access). Donor / donation COUNTS are public; the dollar
    ``total_raised`` and each ``per_year.total`` are gated to amount-viewers."""
    show = _can_view_amounts(user, config)

    donor_count = await session.scalar(
        select(func.count(func.distinct(Donation.alumni_id)))
    )
    donation_count = await session.scalar(select(func.count(Donation.donation_id)))
    total_raised = await session.scalar(
        select(func.coalesce(func.sum(Donation.amount), 0))
    )

    year_rows = (
        await session.execute(
            select(
                Donation.donation_year,
                func.count(func.distinct(Donation.alumni_id)),
                func.coalesce(func.sum(Donation.amount), 0),
            )
            .group_by(Donation.donation_year)
            .order_by(Donation.donation_year.desc())
        )
    ).all()

    return {
        "donor_count": int(donor_count or 0),
        "donation_count": int(donation_count or 0),
        "total_raised": _money(total_raised, show),
        "per_year": [
            {
                "year": year,
                "donor_count": int(dcount),
                "total": _money(total, show),
            }
            for year, dcount, total in year_rows
        ],
    }


# --- Per-alumnus detail + writes ---------------------------------------------


@router.get("/alumni/{alumni_id}")
async def list_alumni_donations(
    alumni_id: int,
    user: RequireViewAccess,
    config: PermissionConfig,
    session: SessionDep,
) -> dict:
    """A single donor's donation history (view access). 404 if the alumnus is
    unknown. Each entry's ``amount`` and ``notes`` are gated to amount-viewers."""
    show = _can_view_amounts(user, config)
    alumni = await session.get(Alumni, alumni_id)
    if alumni is None:
        raise NotFoundError(f"Alumni {alumni_id} not found.")

    rows = (
        await session.execute(
            select(Donation)
            .where(Donation.alumni_id == alumni_id)
            .order_by(
                Donation.donation_year.desc(),
                Donation.donation_month.desc().nullslast(),
                Donation.donation_id.desc(),
            )
        )
    ).scalars().all()

    lifetime = sum((d.amount for d in rows), Decimal(0))
    return {
        "alumni_id": alumni_id,
        "name": _alumni_name(
            alumni.first_name, alumni.preferred_first_name, alumni.last_name, alumni_id
        ),
        "donation_count": len(rows),
        "lifetime_total": _money(lifetime, show),
        "donations": [
            {
                "donation_id": d.donation_id,
                "year": d.donation_year,
                "month": d.donation_month,
                "amount": _money(d.amount, show),
                "notes": d.notes if show else None,
            }
            for d in rows
        ],
    }


def _serialize_donation(d: Donation) -> dict:
    """Full serialization including amount — used only on super_admin write paths
    (the caller is authorized to see the value)."""
    return {
        "donation_id": d.donation_id,
        "alumni_id": d.alumni_id,
        "year": d.donation_year,
        "month": d.donation_month,
        "amount": float(d.amount),
        "notes": d.notes,
    }


@router.post("/alumni/{alumni_id}", status_code=status.HTTP_201_CREATED)
async def add_donation(
    alumni_id: int,
    payload: DonationCreate,
    user: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Add a donation to an alumnus (super_admin). 404 if the alumnus is unknown
    or archived. Audits the write (entity_type "donation", action "create")."""
    alumni = await session.get(Alumni, alumni_id)
    if alumni is None or alumni.archived:
        raise NotFoundError(f"Alumni {alumni_id} not found.")

    donation = Donation(
        alumni_id=alumni_id,
        amount=payload.amount,
        donation_month=payload.month,
        donation_year=payload.year,
        notes=payload.notes,
        logged_by_user_id=user.user_id,
    )
    session.add(donation)
    await session.flush()
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="create",
            entity_type="donation",
            entity_id=donation.donation_id,
            new_value=f"{payload.amount} ({payload.month or '-'}/{payload.year})",
        )
    )
    await session.commit()
    await session.refresh(donation)
    return _serialize_donation(donation)


_FIELD_MAP = {
    "amount": "amount",
    "year": "donation_year",
    "month": "donation_month",
    "notes": "notes",
}


@router.patch("/{donation_id}")
async def update_donation(
    donation_id: int,
    payload: DonationUpdate,
    user: RequireSuperAdmin,
    session: SessionDep,
) -> dict:
    """Partially update a donation (super_admin). Only fields present in the body
    are applied; each change is audited. 404 if the donation is unknown."""
    donation = await session.get(Donation, donation_id)
    if donation is None:
        raise NotFoundError(f"Donation {donation_id} not found.")

    changes = payload.model_dump(exclude_unset=True)
    # amount/year back NOT NULL columns. An explicit ``null`` survives
    # exclude_unset, so reject it as a 422 rather than letting setattr push NULL
    # to the DB and surface as an opaque 500. (month/notes are nullable.)
    for required in ("amount", "year"):
        if required in changes and changes[required] is None:
            raise InvalidRequestError(f"{required} cannot be set to null.")
    applied = False
    for field, value in changes.items():
        column = _FIELD_MAP[field]
        old = getattr(donation, column)
        if old != value:
            setattr(donation, column, value)
            session.add(
                AuditLog(
                    user_id=user.user_id,
                    action_type="update",
                    entity_type="donation",
                    entity_id=donation_id,
                    field_name=column,
                    old_value=str(old) if old is not None else None,
                    new_value=str(value) if value is not None else None,
                )
            )
            applied = True

    if applied:
        await session.commit()
        await session.refresh(donation)
    return _serialize_donation(donation)


@router.delete("/{donation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_donation(
    donation_id: int,
    user: RequireFullAccess,
    session: SessionDep,
) -> Response:
    """Delete a donation (full_access and up). 404 if unknown. Audits the write
    (entity_type "donation", action "delete") with the actor's user id — the
    DB trigger snapshots the actor email for the FERPA trail. Returns 204.

    Gated to the ``alumni.full`` admin tier (full_access / super_admin /
    engineer), matching the other destructive data-management writes (event
    delete, alumni archive). Broadened from the original super_admin-only gate
    during QA hardening (H4)."""
    donation = await session.get(Donation, donation_id)
    if donation is None:
        raise NotFoundError(f"Donation {donation_id} not found.")
    snapshot = f"{donation.amount} ({donation.donation_month or '-'}/{donation.donation_year})"
    await session.delete(donation)
    session.add(
        AuditLog(
            user_id=user.user_id,
            action_type="delete",
            entity_type="donation",
            entity_id=donation_id,
            old_value=snapshot,
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Bulk CSV import (super_admin) -------------------------------------------


async def _read_capped(file: UploadFile) -> bytes | None:
    data = await file.read(import_donations.MAX_UPLOAD_BYTES + 1)
    if len(data) > import_donations.MAX_UPLOAD_BYTES:
        return None
    return data


def _too_large_response() -> JSONResponse:
    mib = import_donations.MAX_UPLOAD_BYTES // (1024 * 1024)
    return JSONResponse(
        status_code=413,
        content={
            "error": {
                "code": "payload_too_large",
                "message": (
                    f"File exceeds the {mib} MB upload limit. Split into "
                    "smaller batches."
                ),
            }
        },
    )


@router.get("/import/template")
async def donations_import_template(_: RequireSuperAdmin) -> Response:
    """Download the donations bulk-import CSV template (super_admin): columns
    Net ID, Name, Month, Year, Amount plus a couple of example rows."""
    return Response(
        content=import_donations.build_template_csv(),
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="donations_import_template.csv"'
        },
    )


@router.post("/import/preview", response_model=None)
async def preview_import_donations(
    _: RequireSuperAdmin,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Dry-run a donations bulk CSV import (super_admin, NO writes). Matches
    donors by Net ID and flags unmatched Net IDs, bad month/year, and non-numeric
    amounts. A bad header set surfaces as ``columns_ok: false``."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_donations.parse_and_map(file_bytes)
    if header_errors:
        return {
            "columns_ok": False,
            "header_errors": header_errors,
            "summary": {"total": 0, "importable": 0, "rejected": 0},
            "rows": [],
        }
    return await import_donations.evaluate(session, rows)


@router.post("/import", response_model=None)
async def import_donations_commit(
    user: RequireSuperAdmin,
    session: SessionDep,
    file: Annotated[UploadFile, File()],
) -> dict | JSONResponse:
    """Commit a donations bulk CSV import (super_admin). Re-evaluates and inserts
    every importable donation in one transaction (audited per row); rejected rows
    are skipped and reported. A bad header set imports nothing."""
    file_bytes = await _read_capped(file)
    if file_bytes is None:
        return _too_large_response()
    rows, header_errors = import_donations.parse_and_map(file_bytes)
    if header_errors:
        return {
            "imported": 0,
            "skipped": 0,
            "rejects": [
                {"row": 0, "name": "(header)", "reason": msg} for msg in header_errors
            ],
        }
    return await import_donations.commit_import(session, rows, actor_user_id=user.user_id)

"""Alumni CRUD routes.

Reads require view access (either role); writes require full_access. ``DELETE``
is a soft-delete (archive), never a hard delete — audit history depends on
retained records.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireFullAccess, RequireViewAccess
from app.core.database import get_session
from app.schemas.alumni import AlumniCreate, AlumniPage, AlumniRead, AlumniUpdate
from app.services import alumni as service

router = APIRouter(prefix="/alumni", tags=["alumni"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("", response_model=AlumniPage)
async def list_alumni(
    _: RequireViewAccess,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    include_archived: bool = False,
) -> AlumniPage:
    items, total = await service.list_alumni(
        session, limit=limit, offset=offset, include_archived=include_archived
    )
    return AlumniPage(items=items, total=total, limit=limit, offset=offset)


@router.get("/{alumni_id}", response_model=AlumniRead)
async def get_alumni(
    alumni_id: int, _: RequireViewAccess, session: SessionDep
) -> AlumniRead:
    return await service.get_alumni(session, alumni_id)


@router.post("", response_model=AlumniRead, status_code=status.HTTP_201_CREATED)
async def create_alumni(
    payload: AlumniCreate, _: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    return await service.create_alumni(session, payload)


@router.patch("/{alumni_id}", response_model=AlumniRead)
async def update_alumni(
    alumni_id: int,
    payload: AlumniUpdate,
    _: RequireFullAccess,
    session: SessionDep,
) -> AlumniRead:
    return await service.update_alumni(session, alumni_id, payload)


@router.delete("/{alumni_id}", response_model=AlumniRead)
async def archive_alumni(
    alumni_id: int, _: RequireFullAccess, session: SessionDep
) -> AlumniRead:
    """Soft-delete (archive) an alumni record."""
    return await service.archive_alumni(session, alumni_id)

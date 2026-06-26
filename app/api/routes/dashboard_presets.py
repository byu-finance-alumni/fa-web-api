"""Dashboard quick-filter preset routes.

Two surfaces:
- ``GET /dashboard/presets`` — the quick-filter presets shown on the dashboard's
  Quick search tab. Readable by any provisioned role (``RequireViewAccess``).
- ``/admin/dashboard-presets`` CRUD — add / edit / remove presets. Restricted to
  ``RequireSuperAdmin`` (engineer + super_admin). Every mutation writes an audit
  row, like the support-contact and vocabulary admin routes.

The stored rows ARE exactly what's displayed (no active flag) — admins curate the
list directly.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireSuperAdmin, RequireViewAccess
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.models.audit import AuditLog
from app.models.dashboard_preset import DashboardPreset
from app.schemas.dashboard_preset import (
    DashboardPresetCreate,
    DashboardPresetRead,
    DashboardPresetUpdate,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Logged-in read: the presets the dashboard Quick search tab renders.
router = APIRouter(prefix="/dashboard/presets", tags=["dashboard"])
# Engineer + super_admin CRUD.
admin_router = APIRouter(
    prefix="/admin/dashboard-presets", tags=["dashboard-admin"]
)


def _ordered() -> select:
    return select(DashboardPreset).order_by(
        DashboardPreset.sort_order, DashboardPreset.dashboard_preset_id
    )


async def _load(session: AsyncSession, preset_id: int) -> DashboardPreset:
    preset = await session.scalar(
        select(DashboardPreset).where(
            DashboardPreset.dashboard_preset_id == preset_id
        )
    )
    if preset is None:
        raise NotFoundError(f"Dashboard preset {preset_id} not found.")
    return preset


@router.get("", response_model=list[DashboardPresetRead])
async def list_dashboard_presets(
    _: RequireViewAccess, session: SessionDep
) -> list[DashboardPresetRead]:
    """The quick-filter presets to show on the dashboard (ordered)."""
    rows = (await session.scalars(_ordered())).all()
    return [DashboardPresetRead.model_validate(p) for p in rows]


@admin_router.get("", response_model=list[DashboardPresetRead])
async def list_dashboard_presets_admin(
    _: RequireSuperAdmin, session: SessionDep
) -> list[DashboardPresetRead]:
    """Same list, behind the admin gate, for the editor UI."""
    rows = (await session.scalars(_ordered())).all()
    return [DashboardPresetRead.model_validate(p) for p in rows]


@admin_router.post(
    "", response_model=DashboardPresetRead, status_code=status.HTTP_201_CREATED
)
async def create_dashboard_preset(
    payload: DashboardPresetCreate,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> DashboardPresetRead:
    """Add a quick-filter preset (engineer / super_admin)."""
    preset = DashboardPreset(
        label=payload.label,
        href=payload.href,
        sort_order=payload.sort_order,
    )
    session.add(preset)
    await session.flush()
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="add_dashboard_preset",
            entity_type="dashboard_preset",
            entity_id=preset.dashboard_preset_id,
            field_name=preset.label,
            new_value=preset.href,
        )
    )
    await session.commit()
    await session.refresh(preset)
    return DashboardPresetRead.model_validate(preset)


@admin_router.patch("/{preset_id}", response_model=DashboardPresetRead)
async def update_dashboard_preset(
    preset_id: int,
    payload: DashboardPresetUpdate,
    actor: RequireSuperAdmin,
    session: SessionDep,
) -> DashboardPresetRead:
    """Edit a quick-filter preset (engineer / super_admin). 404 if missing."""
    preset = await _load(session, preset_id)
    old_href = preset.href
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(preset, field, value)
    if changes:
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="update_dashboard_preset",
                entity_type="dashboard_preset",
                entity_id=preset.dashboard_preset_id,
                field_name=preset.label,
                old_value=old_href,
                new_value=preset.href,
            )
        )
        await session.commit()
        await session.refresh(preset)
    return DashboardPresetRead.model_validate(preset)


@admin_router.delete("/{preset_id}", response_model=DashboardPresetRead)
async def delete_dashboard_preset(
    preset_id: int, actor: RequireSuperAdmin, session: SessionDep
) -> DashboardPresetRead:
    """Remove a quick-filter preset (engineer / super_admin). 404 if missing."""
    preset = await _load(session, preset_id)
    snapshot = DashboardPresetRead.model_validate(preset)
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="delete_dashboard_preset",
            entity_type="dashboard_preset",
            entity_id=preset.dashboard_preset_id,
            field_name=preset.label,
            old_value=preset.href,
        )
    )
    await session.delete(preset)
    await session.commit()
    return snapshot

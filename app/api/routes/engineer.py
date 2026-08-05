"""Engineer-console routes (#162/#164/#165).

Engineer-only surfaces backing the Engineer Console:

- ``GET /engineer/permissions`` — the full permission matrix (every capability +
  each role's grants), for the role-capabilities table (#163) and the editor.
- ``PATCH /engineer/permissions`` — grant/revoke one capability for one role
  (#164). Engineer-gated, audited, enforced server-side.
- ``POST /engineer/preview-log`` — record that the engineer entered
  preview-as-role mode for a role (#165). The preview itself is a read-only
  frontend affordance; this only writes the audit trail.

All routes require the engineer capability, which is non-assignable (see
``app/core/capabilities``) so the console stays engineer-exclusive.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import (
    RequireEngineer,
    RequireSuperAdmin,
    get_permission_config,
)
from app.core.capabilities import (
    ALL_CAPABILITY_CODES,
    ASSIGNABLE_CAPABILITY_CODES,
    CAPABILITIES,
    CAPABILITIES_BY_CODE,
)
from app.core.database import get_session
from app.core.errors import InvalidRequestError, NotFoundError
from app.core.roles import ROLE_LABELS, ROLE_ORDER, RoleName
from app.models.audit import AuditLog
from app.models.user import Role
from app.repositories import permissions as perms_repo
from app.schemas.permissions import (
    CapabilityInfo,
    PermissionMatrix,
    PermissionToggleRequest,
    PreviewLogRequest,
    PreviewLogResponse,
    RoleGrants,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
ConfigDep = Annotated[dict, Depends(get_permission_config)]

router = APIRouter(prefix="/engineer", tags=["engineer"])
# Read-only matrix for the role-capabilities table in the Users section (#163).
# Visible to user admins (engineer + super_admin) — they curate users, so they
# see what each role can do — but only the engineer can EDIT it (above).
admin_router = APIRouter(prefix="/admin", tags=["admin"])

# Capability-code ordering index for stable, registry-ordered output.
_CAP_INDEX = {c.code: i for i, c in enumerate(CAPABILITIES)}


def _build_matrix(config: dict[str, frozenset[str]]) -> PermissionMatrix:
    """Assemble the matrix response from the loaded grant config."""
    capabilities = [
        CapabilityInfo(
            code=c.code,
            label=c.label,
            description=c.description,
            assignable=c.assignable,
        )
        for c in CAPABILITIES
    ]
    roles: list[RoleGrants] = []
    for role in ROLE_ORDER:
        is_engineer = role is RoleName.ENGINEER
        # Engineer always holds everything (and its row is not editable); other
        # roles hold exactly what the config grants them.
        # Intersected with the registry so a RETIRED code still sitting in
        # `role_capabilities` (`alumni.full`, which #379 dissolved but the
        # migration deliberately leaves behind) never leaks into the matrix as a
        # phantom grant with no row to render it against.
        held = (
            set(ALL_CAPABILITY_CODES)
            if is_engineer
            else set(config.get(role.value, frozenset())) & ALL_CAPABILITY_CODES
        )
        roles.append(
            RoleGrants(
                role=role.value,
                label=ROLE_LABELS[role.value],
                editable=not is_engineer,
                capabilities=sorted(held, key=lambda code: _CAP_INDEX.get(code, 99)),
            )
        )
    return PermissionMatrix(capabilities=capabilities, roles=roles)


@router.get("/permissions", response_model=PermissionMatrix)
async def get_permissions(_: RequireEngineer, config: ConfigDep) -> PermissionMatrix:
    """Return the full permission matrix (engineer-only)."""
    return _build_matrix(config)


@admin_router.get("/role-capabilities", response_model=PermissionMatrix)
async def get_role_capabilities(
    _: RequireSuperAdmin, config: ConfigDep
) -> PermissionMatrix:
    """Read-only permission matrix for the role-capabilities table (#163).

    Same data as the engineer editor but behind the user-admin gate (engineer +
    super_admin), so a super_admin can SEE what each role can do without being
    able to change it. The table renders the non-engineer roles."""
    return _build_matrix(config)


@router.patch("/permissions", response_model=PermissionMatrix)
async def toggle_permission(
    payload: PermissionToggleRequest,
    actor: RequireEngineer,
    session: SessionDep,
) -> PermissionMatrix:
    """Grant or revoke one capability for one role (engineer-only, audited).

    Rejects (422) toggling the engineer role (its grants are fixed) or a
    non-assignable capability (the ``engineer`` console capability can never be
    handed to another role). 404 if the role doesn't exist.
    """
    # The engineer role's grants are fixed — it always holds everything.
    if payload.role == RoleName.ENGINEER.value:
        raise InvalidRequestError(
            "The engineer role's capabilities cannot be changed."
        )
    if payload.role not in ROLE_LABELS:
        raise InvalidRequestError(f"Unknown role: {payload.role}.")
    cap = CAPABILITIES_BY_CODE.get(payload.capability)
    if cap is None:
        raise InvalidRequestError(f"Unknown capability: {payload.capability}.")
    if payload.capability not in ASSIGNABLE_CAPABILITY_CODES:
        raise InvalidRequestError(
            f"The '{cap.label}' capability cannot be assigned to another role."
        )

    role = await session.scalar(
        select(Role).where(Role.role_name == payload.role)
    )
    if role is None:
        raise NotFoundError(f"Role '{payload.role}' is not provisioned.")

    changed = await perms_repo.set_grant(
        session,
        role_id=role.role_id,
        capability_code=payload.capability,
        granted=payload.granted,
    )
    if changed:
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="grant_capability"
                if payload.granted
                else "revoke_capability",
                entity_type="role",
                entity_id=role.role_id,
                field_name=payload.capability,
                new_value=payload.role,
            )
        )
        await session.commit()

    # Return the fresh matrix straight from the database so the client sees the
    # authoritative post-change state (not the request-cached pre-change config).
    config = await perms_repo.load_grants(session)
    return _build_matrix(config)


@router.post("/preview-log", response_model=PreviewLogResponse)
async def log_preview(
    payload: PreviewLogRequest,
    actor: RequireEngineer,
    session: SessionDep,
) -> PreviewLogResponse:
    """Record that the engineer entered preview-as-role mode for a role (#165).

    Preview-as-role is a read-only frontend affordance — it never grants the
    engineer access to anything they couldn't already reach — but entering it is
    audited so the trail shows when the engineer was viewing the app as another
    role. 422 if the role is unknown.
    """
    if payload.role not in ROLE_LABELS:
        raise InvalidRequestError(f"Unknown role: {payload.role}.")
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="preview_as_role",
            entity_type="role",
            field_name="role",
            new_value=payload.role,
        )
    )
    await session.commit()
    return PreviewLogResponse()

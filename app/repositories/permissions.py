"""Data access for the editable permission config (``role_capabilities``).

Loads the role→capabilities grant map used by the authorization guards, and
applies a single grant/revoke from the permission editor. The capability codes
are defined in code (``app/core/capabilities.py``); this module only reads and
writes which roles hold which.
"""

from __future__ import annotations

import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.capabilities import DEFAULT_GRANTS
from app.models.role_capability import RoleCapability
from app.models.user import Role

logger = logging.getLogger(__name__)


async def load_grants(session: AsyncSession) -> dict[str, frozenset[str]]:
    """Return ``{role_name: frozenset(capability_codes)}`` from the database.

    Joins ``role_capabilities`` to ``roles`` so the result is keyed by the
    stable role name (matching ``UserContext.roles``).

    **Fail-safe to baseline.** If the table is empty (the seed migration hasn't
    run yet) OR it can't be read for any reason, falls back to
    :data:`app.core.capabilities.DEFAULT_GRANTS` — the historical hardcoded guard
    mapping. The fallback is never MORE permissive than the original model, so a
    transient read failure degrades authorization to exactly the pre-#164
    behaviour rather than denying everything or 500-ing every request.
    """
    try:
        rows = (
            await session.execute(
                select(Role.role_name, RoleCapability.capability_code).join(
                    RoleCapability, RoleCapability.role_id == Role.role_id
                )
            )
        ).all()
    except Exception:  # noqa: BLE001 - fail safe to the historical defaults
        logger.warning(
            "Could not read role_capabilities; using default grants.",
            exc_info=True,
        )
        return dict(DEFAULT_GRANTS)
    if not rows:
        return dict(DEFAULT_GRANTS)

    grants: dict[str, set[str]] = {}
    for role_name, capability_code in rows:
        grants.setdefault(role_name, set()).add(capability_code)
    return {role: frozenset(caps) for role, caps in grants.items()}


async def set_grant(
    session: AsyncSession, *, role_id: int, capability_code: str, granted: bool
) -> bool:
    """Grant or revoke ``capability_code`` for ``role_id``. Idempotent.

    Returns True if a row was actually inserted/removed (i.e. the state
    changed), False if it was already in the requested state — so the caller can
    skip writing an audit row for a no-op. Does NOT commit; the caller owns the
    transaction (so the change + its audit row land atomically).
    """
    existing = await session.scalar(
        select(RoleCapability).where(
            RoleCapability.role_id == role_id,
            RoleCapability.capability_code == capability_code,
        )
    )
    if granted:
        if existing is not None:
            return False
        session.add(
            RoleCapability(role_id=role_id, capability_code=capability_code)
        )
        return True
    if existing is None:
        return False
    await session.execute(
        delete(RoleCapability).where(
            RoleCapability.role_id == role_id,
            RoleCapability.capability_code == capability_code,
        )
    )
    return True

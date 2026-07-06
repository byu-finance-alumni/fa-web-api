"""Engineer-managed support-contact routes.

Two surfaces:
- ``GET /support-contacts`` — the contacts shown to a logged-in user on the
  in-app error screen. Readable by any provisioned role (``RequireViewAccess``);
  there is deliberately NO unauthenticated endpoint, so these names/emails are
  never exposed on the public login page.
- ``/admin/support-contacts`` CRUD — add / edit / remove contacts. Restricted to
  ``RequireEngineer`` ("the engineer controls it"). Every mutation writes an
  audit row, like the user- and vocabulary-admin routes.

The stored rows ARE exactly what's displayed (no active flag) — the engineer
curates the list directly.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireEngineer, RequireViewAccess
from app.api.params import IdPath
from app.core.database import get_session
from app.core.errors import NotFoundError
from app.models.audit import AuditLog
from app.models.support_contact import SupportContact
from app.schemas.support import (
    SupportContactCreate,
    SupportContactRead,
    SupportContactUpdate,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Logged-in read: the contacts the in-app error screen renders.
router = APIRouter(prefix="/support-contacts", tags=["support"])
# Engineer-only CRUD.
admin_router = APIRouter(prefix="/admin/support-contacts", tags=["support-admin"])


def _ordered() -> select:
    return select(SupportContact).order_by(
        SupportContact.sort_order, SupportContact.support_contact_id
    )


async def _load(session: AsyncSession, contact_id: int) -> SupportContact:
    contact = await session.scalar(
        select(SupportContact).where(
            SupportContact.support_contact_id == contact_id
        )
    )
    if contact is None:
        raise NotFoundError(f"Support contact {contact_id} not found.")
    return contact


@router.get("", response_model=list[SupportContactRead])
async def list_support_contacts(
    _: RequireViewAccess, session: SessionDep
) -> list[SupportContactRead]:
    """The support contacts to show a logged-in user (ordered)."""
    rows = (await session.scalars(_ordered())).all()
    return [SupportContactRead.model_validate(c) for c in rows]


@admin_router.get("", response_model=list[SupportContactRead])
async def list_support_contacts_admin(
    _: RequireEngineer, session: SessionDep
) -> list[SupportContactRead]:
    """Same list, behind the engineer gate, for the editor UI."""
    rows = (await session.scalars(_ordered())).all()
    return [SupportContactRead.model_validate(c) for c in rows]


@admin_router.post(
    "", response_model=SupportContactRead, status_code=status.HTTP_201_CREATED
)
async def create_support_contact(
    payload: SupportContactCreate,
    actor: RequireEngineer,
    session: SessionDep,
) -> SupportContactRead:
    """Add a support contact (engineer only)."""
    contact = SupportContact(
        role_label=payload.role_label,
        name=payload.name,
        email=payload.email,
        sort_order=payload.sort_order,
    )
    session.add(contact)
    await session.flush()
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="add_support_contact",
            entity_type="support_contact",
            entity_id=contact.support_contact_id,
            field_name=contact.role_label,
            new_value=contact.email,
        )
    )
    await session.commit()
    await session.refresh(contact)
    return SupportContactRead.model_validate(contact)


@admin_router.patch("/{contact_id}", response_model=SupportContactRead)
async def update_support_contact(
    contact_id: IdPath,
    payload: SupportContactUpdate,
    actor: RequireEngineer,
    session: SessionDep,
) -> SupportContactRead:
    """Edit a support contact (engineer only). 404 if missing."""
    contact = await _load(session, contact_id)
    old_email = contact.email
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(contact, field, value)
    if changes:
        session.add(
            AuditLog(
                user_id=actor.user_id,
                action_type="update_support_contact",
                entity_type="support_contact",
                entity_id=contact.support_contact_id,
                field_name=contact.role_label,
                old_value=old_email,
                new_value=contact.email,
            )
        )
        await session.commit()
        await session.refresh(contact)
    return SupportContactRead.model_validate(contact)


@admin_router.delete("/{contact_id}", response_model=SupportContactRead)
async def delete_support_contact(
    contact_id: IdPath, actor: RequireEngineer, session: SessionDep
) -> SupportContactRead:
    """Remove a support contact (engineer only). 404 if missing."""
    contact = await _load(session, contact_id)
    snapshot = SupportContactRead.model_validate(contact)
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="delete_support_contact",
            entity_type="support_contact",
            entity_id=contact.support_contact_id,
            field_name=contact.role_label,
            old_value=contact.email,
        )
    )
    await session.delete(contact)
    await session.commit()
    return snapshot
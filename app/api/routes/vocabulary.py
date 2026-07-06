"""Editable controlled-vocabulary routes (#82).

Two surfaces:
- ``GET /vocabulary/{category}`` — the active option strings for a dropdown.
  Readable by any provisioned role (the app needs options to render forms).
- ``/admin/vocabulary`` CRUD — add / edit / deactivate terms. Engineer-only
  (the controlled-vocabulary admin role) via ``RequireVocabAdmin``. Every
  mutation writes an audit row (who changed what), like the user-admin routes.

Deletes are soft (active=false): a value still on existing records stays valid,
it just disappears from new-entry dropdowns.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireViewAccess, RequireVocabAdmin
from app.api.params import IdPath
from app.core.database import get_session
from app.core.vocabularies import VocabularyCategory
from app.models.audit import AuditLog
from app.schemas.vocabulary import (
    VocabularyTermCreate,
    VocabularyTermRead,
    VocabularyTermUpdate,
)
from app.services import vocabulary as service

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Public-read router: dropdown options for the app's forms.
router = APIRouter(prefix="/vocabulary", tags=["vocabulary"])
# Admin CRUD router: manage the vocabulary (engineer-only).
admin_router = APIRouter(prefix="/admin/vocabulary", tags=["vocabulary-admin"])


@router.get("/{category}")
async def get_vocabulary(
    category: VocabularyCategory, _: RequireViewAccess, session: SessionDep
) -> dict:
    """Active option strings for a category (dropdown payload). An unknown
    category is a 422 (validated against VocabularyCategory)."""
    values = await service.list_active_values(session, category)
    return {"category": category.value, "values": values}


@admin_router.get("/{category}", response_model=list[VocabularyTermRead])
async def list_vocabulary_admin(
    category: VocabularyCategory, _: RequireVocabAdmin, session: SessionDep
) -> list[VocabularyTermRead]:
    """All terms in a category, INCLUDING inactive ones, for the admin UI."""
    terms = await service.list_terms(session, category, include_inactive=True)
    return [VocabularyTermRead.model_validate(t) for t in terms]


@admin_router.post(
    "", response_model=VocabularyTermRead, status_code=status.HTTP_201_CREATED
)
async def create_vocabulary_term(
    payload: VocabularyTermCreate,
    actor: RequireVocabAdmin,
    session: SessionDep,
    response: Response,
) -> VocabularyTermRead:
    """Add a term (or reactivate a previously-deactivated identical one).
    409 if an active term with the same value already exists in the category.

    Returns 201 Created for a genuinely new term, 200 OK when an existing
    soft-deleted term was reactivated (nothing new was created) (#176)."""
    term, reactivated = await service.create_term(
        session, payload.category, payload.value, payload.sort_order
    )
    if reactivated:
        response.status_code = status.HTTP_200_OK
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="reactivate_vocab" if reactivated else "add_vocab",
            entity_type="vocabulary",
            entity_id=term.term_id,
            field_name=term.category,
            new_value=term.value,
        )
    )
    await session.commit()
    await session.refresh(term)
    return VocabularyTermRead.model_validate(term)


@admin_router.patch("/{term_id}", response_model=VocabularyTermRead)
async def update_vocabulary_term(
    term_id: IdPath,
    payload: VocabularyTermUpdate,
    actor: RequireVocabAdmin,
    session: SessionDep,
) -> VocabularyTermRead:
    """Edit a term (rename / reorder / activate-deactivate). 404 if missing;
    409 if a rename collides with another term in the same category."""
    before = await service.get_term(session, term_id)
    old_value = before.value
    term = await service.update_term(
        session,
        term_id,
        value=payload.value,
        sort_order=payload.sort_order,
        active=payload.active,
    )
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="update_vocab",
            entity_type="vocabulary",
            entity_id=term.term_id,
            field_name=term.category,
            old_value=old_value,
            new_value=term.value,
        )
    )
    await session.commit()
    await session.refresh(term)
    return VocabularyTermRead.model_validate(term)


@admin_router.delete("/{term_id}", response_model=VocabularyTermRead)
async def deactivate_vocabulary_term(
    term_id: IdPath, actor: RequireVocabAdmin, session: SessionDep
) -> VocabularyTermRead:
    """Soft-delete a term (active=false): hidden from new-entry dropdowns, but
    still valid on existing records. Idempotent."""
    term = await service.deactivate_term(session, term_id)
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="deactivate_vocab",
            entity_type="vocabulary",
            entity_id=term.term_id,
            field_name=term.category,
            old_value=term.value,
        )
    )
    await session.commit()
    await session.refresh(term)
    return VocabularyTermRead.model_validate(term)

"""Editable controlled-vocabulary routes (#82).

Three surfaces:
- ``GET /vocabulary/{category}`` — the active option strings for a dropdown.
  Readable by any provisioned role (the app needs options to render forms).
- ``GET /vocabulary/state-regions`` — the static state -> region crosswalk.
  Same read gate; NOT editable vocabulary (see the route's docstring).
- ``/admin/vocabulary`` CRUD — add / edit / deactivate terms. Engineer-only
  (the controlled-vocabulary admin role) via ``RequireVocabAdmin``. Every
  mutation writes an audit row (who changed what), like the user-admin routes.

Deletes are soft (active=false): a value still on existing records stays valid,
it just disappears from new-entry dropdowns.
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies.auth import RequireViewAccess, RequireVocabAdmin
from app.api.params import IdPath
from app.core.database import get_session
from app.core.dropdowns import filter_primary_industries
from app.core.vocabularies import VocabularyCategory
from app.models.audit import AuditLog
from app.schemas.state_regions import StateRegionMap
from app.schemas.vocabulary import (
    VocabularyTermCreate,
    VocabularyTermRead,
    VocabularyTermUpdate,
)
from app.services import state_regions
from app.services import vocabulary as service

SessionDep = Annotated[AsyncSession, Depends(get_session)]

# Public-read router: dropdown options for the app's forms.
router = APIRouter(prefix="/vocabulary", tags=["vocabulary"])
# Admin CRUD router: manage the vocabulary (engineer-only).
admin_router = APIRouter(prefix="/admin/vocabulary", tags=["vocabulary-admin"])


# The crosswalk payload, built ONCE at import straight from
# app.services.state_regions — the same module the write path uses to derive
# contact.region from career.current_state (#283). Deriving it (rather than
# retyping the map here) is the entire point: the endpoint cannot drift from the
# regions the server actually persists. Building it at import also makes each
# request a cheap in-memory send, with no database touched.
_STATE_REGION_MAP = StateRegionMap(
    regions=list(state_regions.REGIONS),
    region_by_state={
        state: region
        for region, states in state_regions.STATES_BY_REGION.items()
        for state in states
    },
)


# Declared BEFORE the /{category} route below: paths are matched in declaration
# order, so this literal path must win before FastAPI tries "state-regions" as a
# VocabularyCategory (which would 422). Keep it above.
@router.get("/state-regions", response_model=StateRegionMap)
async def get_state_regions(
    _: RequireViewAccess, response: Response
) -> StateRegionMap:
    """The 50-states + DC -> region crosswalk (#283).

    Lets the edit form fill in Region the moment an Employment State is picked,
    so the value is visible before saving and matches what the server will derive
    on write. The frontend must NOT keep its own copy of this map — a hand-copied
    map would silently rot and no test could catch the disagreement.

    Despite living under ``/vocabulary`` (it is dropdown data for a form, on the
    same read gate), this is NOT editable vocabulary: it is static reference data
    defined in code, so it has no admin CRUD and cannot be changed at runtime.

    Cacheable — the payload is identical for every caller, contains no PII, and
    only ever changes on deploy. The one-hour max-age is short enough that a
    correction to the map propagates the same day.
    """
    response.headers["Cache-Control"] = "public, max-age=3600"
    return _STATE_REGION_MAP


@router.get("/{category}")
async def get_vocabulary(
    category: VocabularyCategory,
    _: RequireViewAccess,
    session: SessionDep,
    scope: Annotated[
        Literal["all", "primary"],
        Query(
            description=(
                "'all' (default) returns every active term. 'primary' additionally "
                "hides the industries that may only be used as a SECONDARY industry "
                "(Law, Corporate Banking, Sales and Trading, Credit Risk) — pass it "
                "when rendering the PRIMARY industry dropdown. No effect on other "
                "categories."
            )
        ),
    ] = "all",
) -> dict:
    """Active option strings for a category (dropdown payload). An unknown
    category (or scope) is a 422.

    ``scope=primary`` narrows the ``industry`` category to the primary-industry
    options (#282). The terms it hides are still ACTIVE vocabulary and are still
    accepted on write — they are only withheld from the primary dropdown."""
    values = await service.list_active_values(session, category)
    if category is VocabularyCategory.INDUSTRY and scope == "primary":
        values = filter_primary_industries(values)
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


@admin_router.delete(
    "/{term_id}/permanent", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_vocabulary_term(
    term_id: IdPath, actor: RequireVocabAdmin, session: SessionDep
) -> Response:
    """Permanently remove a term (hard delete), unlike the soft-delete DELETE
    above. Existing records that already stored this value keep it — only the
    managed option is removed, so it no longer appears in any admin list or
    dropdown and cannot be restored. Writes an audit row. 404 if missing."""
    term = await service.get_term(session, term_id)
    # Snapshot for the audit row before the row is deleted (attributes are
    # expired after commit).
    category, value, entity_id = term.category, term.value, term.term_id
    await service.delete_term(session, term_id)
    session.add(
        AuditLog(
            user_id=actor.user_id,
            action_type="delete_vocab",
            entity_type="vocabulary",
            entity_id=entity_id,
            field_name=category,
            old_value=value,
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)

"""Service layer for the editable controlled vocabulary (#82).

CRUD over ``vocabulary_terms`` plus the read helpers the rest of the app uses to
populate dropdowns and validate writes. Deletes are soft (``active=false``) so a
value still referenced by existing records stays valid; it is only hidden from
new-entry dropdowns. Creating a value that already exists but is inactive
reactivates it (so admins can't hit a dead "already exists" wall for a value
they previously hid).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.vocabularies import VocabularyCategory
from app.models.vocabulary import VocabularyTerm


async def list_terms(
    session: AsyncSession,
    category: VocabularyCategory,
    *,
    include_inactive: bool = False,
) -> list[VocabularyTerm]:
    """All terms in a category, ordered by sort_order then value. Active-only by
    default (the dropdown view); admins pass include_inactive=True to manage."""
    stmt = select(VocabularyTerm).where(VocabularyTerm.category == category.value)
    if not include_inactive:
        stmt = stmt.where(VocabularyTerm.active.is_(True))
    stmt = stmt.order_by(VocabularyTerm.sort_order, VocabularyTerm.value)
    return list((await session.scalars(stmt)).all())


async def list_active_values(
    session: AsyncSession, category: VocabularyCategory
) -> list[str]:
    """Just the active option strings for a category (dropdown payload)."""
    return [t.value for t in await list_terms(session, category)]


async def is_valid_value(
    session: AsyncSession, category: VocabularyCategory, value: str
) -> bool:
    """True if ``value`` is an ACTIVE term in ``category``. Used to validate
    writes (e.g. an event's type) against the admin-managed set."""
    found = await session.scalar(
        select(VocabularyTerm.term_id).where(
            VocabularyTerm.category == category.value,
            VocabularyTerm.value == value,
            VocabularyTerm.active.is_(True),
        )
    )
    return found is not None


async def _get_by_category_value(
    session: AsyncSession, category: str, value: str
) -> VocabularyTerm | None:
    return await session.scalar(
        select(VocabularyTerm).where(
            VocabularyTerm.category == category, VocabularyTerm.value == value
        )
    )


async def create_term(
    session: AsyncSession,
    category: VocabularyCategory,
    value: str,
    sort_order: int = 0,
) -> tuple[VocabularyTerm, bool]:
    """Add a term. Returns (term, reactivated). If an identical (category, value)
    already exists: active → 409 ConflictError; inactive → reactivated and
    returned with reactivated=True. Does NOT commit (the caller commits after
    writing its audit row)."""
    existing = await _get_by_category_value(session, category.value, value)
    if existing is not None:
        if existing.active:
            raise ConflictError(
                f"'{value}' already exists in {category.value}."
            )
        existing.active = True
        existing.sort_order = sort_order
        return existing, True
    term = VocabularyTerm(
        category=category.value, value=value, sort_order=sort_order, active=True
    )
    session.add(term)
    await session.flush()  # assign term_id without committing
    return term, False


async def get_term(session: AsyncSession, term_id: int) -> VocabularyTerm:
    term = await session.get(VocabularyTerm, term_id)
    if term is None:
        raise NotFoundError(f"Vocabulary term {term_id} not found.")
    return term


async def update_term(
    session: AsyncSession,
    term_id: int,
    *,
    value: str | None = None,
    sort_order: int | None = None,
    active: bool | None = None,
) -> VocabularyTerm:
    """Patch a term in place (does NOT commit). A rename that collides with
    another term in the same category is a 409."""
    term = await get_term(session, term_id)
    if value is not None and value != term.value:
        clash = await _get_by_category_value(session, term.category, value)
        if clash is not None and clash.term_id != term.term_id:
            raise ConflictError(
                f"'{value}' already exists in {term.category}."
            )
        term.value = value
    if sort_order is not None:
        term.sort_order = sort_order
    if active is not None:
        term.active = active
    return term


async def deactivate_term(session: AsyncSession, term_id: int) -> VocabularyTerm:
    """Soft-delete: set active=false. Idempotent. Does NOT commit."""
    term = await get_term(session, term_id)
    term.active = False
    return term

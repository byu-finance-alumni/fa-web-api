"""Unified-notes service: CRUD for notes attached to an alumni, an interaction,
or an event.

Authorization: the route layer admits a write with ``notes.create`` or
``notes.manage`` and resolves the caller's :class:`NoteRights` from the live
permission config; this layer applies the finer rule — a caller without
``notes.manage`` may add notes on alumni profiles only and may edit or delete
only the notes they wrote (403 otherwise, after the 404 existence check). It
also validates the attach target exists,
maps the unified ``(entity_type, entity_id)`` pair to the right FK column, and
records a FERPA audit row for every write — auditing against the OWNING alumni
where one exists (alumni notes, and interaction notes via the interaction's
alumni) so the change surfaces in that alumni's profile Audit tab; event notes
audit against the event entity. Note bodies are snapshotted on edit/delete so a
later review can reconstruct removed/altered free text, exactly as interaction
notes do.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.security import AuthorizationError
from app.models.audit import AuditLog
from app.models.crm import Interaction
from app.models.event import Event
from app.models.note import Note
from app.models.user import User
from app.schemas.note import NoteCreate, NoteEntityType, NoteRead, NoteUpdate

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class NoteRights:
    """What one caller may do with notes, resolved by the route from the LIVE
    permission config.

    * ``can_manage`` — ``notes.manage``: add at any level, change ANY note.
    * ``can_create`` — ``notes.create`` (or implied by ``can_manage``): add on an
      alumni profile, change only notes ``actor_user_id`` wrote.
    * ``include_archived`` — the caller may see archived alumni (#591); below
      that tier a note on an archived alumnus reads as absent (404).
    """

    actor_user_id: int | None
    can_manage: bool
    can_create: bool
    include_archived: bool = True

    def may_change(self, note: Note) -> bool:
        """May this caller edit / delete ``note``? A note with no author (legacy,
        or its author was deleted) can only be changed by ``notes.manage``."""
        if self.can_manage:
            return True
        return (
            self.can_create
            and self.actor_user_id is not None
            and note.created_by_user_id == self.actor_user_id
        )


# Used when a caller passes no rights (a non-HTTP caller, or an older test):
# behaves like the pre-2026-11-03 service — every write allowed, archived
# visible — because the route guard was the only gate then.
_MANAGE_ALL = NoteRights(actor_user_id=None, can_manage=True, can_create=True)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _full_name(first: str | None, last: str | None, email: str | None) -> str | None:
    name = " ".join(p for p in (first, last) if p).strip()
    return name or email


async def _actor_name(session: AsyncSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return _full_name(user.first_name, user.last_name, user.email) if user else None


async def _actor_first_name(session: AsyncSession, user_id: int | None) -> str | None:
    """The author's FIRST NAME only — for view_only readers, who see who wrote a
    note but not their full identity. Intentionally NO email fallback: a nameless
    account surfaces as ``None`` ("—") rather than leaking an email address."""
    if user_id is None:
        return None
    user = await session.get(User, user_id)
    return (user.first_name or None) if user else None


def _to_read(note: Note, author: str | None, can_edit: bool = False) -> NoteRead:
    """Project a ``Note`` ORM row onto the unified read shape."""
    if note.alumni_id is not None:
        entity_type, entity_id = NoteEntityType.ALUMNI, note.alumni_id
    elif note.interaction_id is not None:
        entity_type, entity_id = NoteEntityType.INTERACTION, note.interaction_id
    else:
        entity_type, entity_id = NoteEntityType.EVENT, note.event_id
    return NoteRead(
        note_id=note.note_id,
        entity_type=entity_type,
        entity_id=entity_id,
        body=note.body,
        author=author,
        can_edit=can_edit,
        created_at=note.created_at,
        updated_at=note.updated_at,
    )


async def _resolve_target(
    session: AsyncSession,
    entity_type: NoteEntityType,
    entity_id: int,
) -> tuple[dict[str, int], str, int]:
    """Validate the attach target exists and return:

      * ``column`` — the FK kwargs to set on the ``Note`` (one of
        ``alumni_id`` / ``interaction_id`` / ``event_id``);
      * ``audit_entity_type`` / ``audit_entity_id`` — the entity the write is
        audited against. Alumni and interaction notes audit against the owning
        alumni (so they appear in that alumni's profile Audit tab); event notes
        audit against the event.

    Raises ``NotFoundError`` (404) if the parent record is missing.
    """
    if entity_type is NoteEntityType.ALUMNI:
        from app.models.alumni import Alumni

        if await session.get(Alumni, entity_id) is None:
            raise NotFoundError(f"Alumni {entity_id} not found.")
        return {"alumni_id": entity_id}, "alumni", entity_id

    if entity_type is NoteEntityType.INTERACTION:
        interaction = await session.get(Interaction, entity_id)
        if interaction is None:
            raise NotFoundError(f"Interaction {entity_id} not found.")
        # Audit against the interaction's alumni so it lands on the profile tab.
        return {"interaction_id": entity_id}, "alumni", interaction.alumni_id

    if await session.get(Event, entity_id) is None:
        raise NotFoundError(f"Event {entity_id} not found.")
    return {"event_id": entity_id}, "event", entity_id


async def _ensure_alumni_visible(
    session: AsyncSession,
    audit_type: str,
    audit_id: int,
    include_archived: bool,
    not_found: str,
) -> None:
    """404 when the note's owning alumnus is ARCHIVED and the caller is below the
    tier that may see archived records (#591) — so a write can never confirm a
    record the profile read already hides. Event notes are unaffected."""
    if include_archived or audit_type != "alumni":
        return
    from app.models.alumni import Alumni

    owner = await session.get(Alumni, audit_id)
    if owner is None or owner.archived:
        raise NotFoundError(not_found)


def _audit(
    session: AsyncSession,
    actor_user_id: int | None,
    action: str,
    audit_entity_type: str,
    audit_entity_id: int,
    *,
    old_value: str | None = None,
    new_value: str | None = None,
) -> None:
    """Record a note write in the audit trail.

    The actor is always present on the API path (writes go through
    ``RequireNotesWrite``). Guard anyway: if a future non-HTTP caller supplies no
    actor, log a warning (never the note body) rather than silently producing an
    unaudited write."""
    if actor_user_id is None:
        log.warning(
            "Note audit skipped: no actor for action=%s entity=%s/%s",
            action,
            audit_entity_type,
            audit_entity_id,
        )
        return
    session.add(
        AuditLog(
            user_id=actor_user_id,
            action_type=action,
            entity_type=audit_entity_type,
            entity_id=audit_entity_id,
            field_name="note",
            old_value=old_value,
            new_value=new_value,
        )
    )


async def list_notes(
    session: AsyncSession,
    entity_type: NoteEntityType,
    entity_id: int,
    actor_user_id: int | None = None,
    *,
    full_author_name: bool = True,
    include_archived: bool = True,
    rights: NoteRights | None = None,
) -> list[NoteRead]:
    """Return the notes attached to one entity, newest first.

    Reads are intentionally open to every view-access role (incl. view_only /
    Professor) per the unified-notes spec — there is no per-record ownership
    scoping. Because note bodies are sensitive free text, the disclosure is
    audit-logged (``view_notes``) so a FERPA review can answer "who read the
    notes on this record?", mirroring the ``view_profile`` / ``search`` trail.

    ``full_author_name`` controls author disclosure: editors (full_access and up,
    incl. student) see the author's full name; a ``view_only`` caller sees the
    first name only — matching how interactions' ``logged_by`` is reduced for
    view_only. The caller (route) decides this from the authenticated role.

    404s if the parent entity does not exist (so a bad id is distinguishable
    from an entity that simply has no notes yet).

    ``include_archived=False`` (callers below full_access, #591) also 404s when
    the notes belong to an ARCHIVED alumnus — directly, or through one of their
    interactions — matching the profile read, which already 404s that record for
    them. Event notes name no single alumnus and are unaffected.

    Each note's ``can_edit`` says whether THIS caller may edit / delete it
    (``rights``); with no rights given it is False everywhere."""
    _, audit_type, audit_id = await _resolve_target(session, entity_type, entity_id)
    await _ensure_alumni_visible(
        session,
        audit_type,
        audit_id,
        include_archived,
        f"{entity_type.value.capitalize()} {entity_id} not found.",
    )
    column = {
        NoteEntityType.ALUMNI: Note.alumni_id,
        NoteEntityType.INTERACTION: Note.interaction_id,
        NoteEntityType.EVENT: Note.event_id,
    }[entity_type]
    rows = (
        (
            await session.execute(
                select(Note).where(column == entity_id).order_by(Note.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    resolve_author = _actor_name if full_author_name else _actor_first_name
    result = [
        _to_read(
            n,
            await resolve_author(session, n.created_by_user_id),
            can_edit=rights.may_change(n) if rights is not None else False,
        )
        for n in rows
    ]
    # Best-effort disclosure audit — a logging failure must never break the read.
    if actor_user_id is not None:
        try:
            _audit(
                session,
                actor_user_id,
                "view_notes",
                audit_type,
                audit_id,
                new_value=f"{len(result)} note(s)",
            )
            await session.commit()
        except Exception:  # noqa: BLE001 - audit is best-effort
            with contextlib.suppress(Exception):
                await session.rollback()
    return result


async def create_note(
    session: AsyncSession,
    payload: NoteCreate,
    actor_user_id: int | None,
    rights: NoteRights | None = None,
) -> NoteRead:
    """Create a note. Without ``notes.manage`` (``rights.can_manage``) only an
    alumni-profile note may be added — interaction and event notes are 403."""
    rights = rights or _MANAGE_ALL
    if not rights.can_create:
        raise AuthorizationError()
    if not rights.can_manage and payload.entity_type is not NoteEntityType.ALUMNI:
        raise AuthorizationError("You can only add notes on an alumni profile.")
    column, audit_type, audit_id = await _resolve_target(
        session, payload.entity_type, payload.entity_id
    )
    await _ensure_alumni_visible(
        session,
        audit_type,
        audit_id,
        rights.include_archived,
        f"{payload.entity_type.value.capitalize()} {payload.entity_id} not found.",
    )
    note = Note(
        body=payload.body,
        created_by_user_id=actor_user_id,
        updated_by_user_id=actor_user_id,
        **column,
    )
    session.add(note)
    _audit(
        session,
        actor_user_id,
        "add_note",
        audit_type,
        audit_id,
        new_value=payload.body,
    )
    await session.commit()
    await session.refresh(note)
    return _to_read(
        note,
        await _actor_name(session, note.created_by_user_id),
        can_edit=rights.may_change(note),
    )


async def _get_changeable_note(
    session: AsyncSession, note_id: int, rights: NoteRights
) -> tuple[Note, str, int]:
    """Load a note the caller is about to edit / delete, or raise.

    404 if it doesn't exist (or sits on an archived alumnus the caller cannot
    see); THEN 403 if the caller may not change it — someone else's note without
    ``notes.manage``. Returns the note and its audit target."""
    note = await session.get(Note, note_id)
    if note is None:
        raise NotFoundError(f"Note {note_id} not found.")
    audit_type, audit_id = await _audit_target_for_note(session, note)
    await _ensure_alumni_visible(
        session, audit_type, audit_id, rights.include_archived, f"Note {note_id} not found."
    )
    if not rights.may_change(note):
        raise AuthorizationError("You can only edit or delete notes you wrote.")
    return note, audit_type, audit_id


async def update_note(
    session: AsyncSession,
    note_id: int,
    payload: NoteUpdate,
    actor_user_id: int | None,
    rights: NoteRights | None = None,
) -> NoteRead:
    """Edit a note's body. A ``notes.manage`` holder may edit any note (notes are
    a shared institutional record, mirroring how interactions are edited); a
    ``notes.create``-only holder only their own. The change is fully audited (old
    + new value) so the FERPA trail attributes it. A no-op edit (identical body)
    writes nothing and emits no audit row."""
    rights = rights or _MANAGE_ALL
    note, audit_type, audit_id = await _get_changeable_note(session, note_id, rights)
    if note.body == payload.body:
        # Genuine no-op: don't bump the row, don't write a spurious audit entry.
        return _to_read(
            note, await _actor_name(session, note.created_by_user_id), can_edit=True
        )
    old = note.body
    note.body = payload.body
    note.updated_by_user_id = actor_user_id
    note.updated_at = _now()
    _audit(
        session,
        actor_user_id,
        "update_note",
        audit_type,
        audit_id,
        old_value=old,
        new_value=payload.body,
    )
    await session.commit()
    await session.refresh(note)
    return _to_read(
        note, await _actor_name(session, note.created_by_user_id), can_edit=True
    )


async def delete_note(
    session: AsyncSession,
    note_id: int,
    actor_user_id: int | None,
    rights: NoteRights | None = None,
) -> None:
    """Delete a note: any note with ``notes.manage``, only your own with
    ``notes.create``."""
    rights = rights or _MANAGE_ALL
    note, audit_type, audit_id = await _get_changeable_note(session, note_id, rights)
    # Snapshot BEFORE deletion so the FERPA trail retains the removed text.
    snapshot = note.body
    await session.delete(note)
    _audit(
        session,
        actor_user_id,
        "delete_note",
        audit_type,
        audit_id,
        old_value=snapshot,
    )
    await session.commit()


async def _audit_target_for_note(session: AsyncSession, note: Note) -> tuple[str, int]:
    """The (entity_type, entity_id) a note's edit/delete is audited against,
    resolved from the note's own FK columns (mirrors ``_resolve_target``)."""
    if note.alumni_id is not None:
        return "alumni", note.alumni_id
    if note.interaction_id is not None:
        interaction = await session.get(Interaction, note.interaction_id)
        # The interaction may already be gone in a cascade race; fall back to the
        # interaction id so the write is still attributable.
        if interaction is not None:
            return "alumni", interaction.alumni_id
        return "interaction", note.interaction_id
    return "event", note.event_id

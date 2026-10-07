"""Per-record version history response schemas (#45).

``GET /alumni/{id}/history`` returns the record's CHANGES (never its read /
disclosure log rows), grouped one save per entry, newest first. Each field
change carries the ``audit_id`` of the ``audit_logs`` row it came from, so a
later per-field restore can name exactly which recorded value it is reverting.
"""

from __future__ import annotations

import datetime
from typing import Literal

from pydantic import BaseModel

# Where a change came from. ``None`` when the row predates provenance capture
# (#45 Phase 1) and the action alone doesn't say.
HistorySource = Literal["manual", "import", "survey"]


class AlumniHistoryChange(BaseModel):
    """One recorded field change (one ``audit_logs`` row)."""

    # The audit row id — the handle a future restore will take.
    audit_id: int
    # The recorded action, e.g. ``update``, ``add_employment``, ``archive``.
    action: str
    # ``first_name`` for a core column, ``contact.email`` / ``career.*`` for a
    # section field, ``employment[12].employment_title`` for a per-row field,
    # ``employment[12]`` for a whole-row add/delete. ``None`` for an action that
    # names no field (archive, restore, create).
    field: str | None = None
    # Human label for ``field`` when one is known; the client falls back to a
    # humanized ``field`` otherwise.
    label: str | None = None
    old: str | None = None
    new: str | None = None
    # True when the caller's role may not see this change's values (they are
    # nulled, exactly as the profile would null the field for that role).
    redacted: bool = False


class AlumniHistoryGroup(BaseModel):
    """One save: every change written together, under one actor and time."""

    # Stable key for the group: the change_set_id, or ``row:<audit_id>`` for a
    # row written without one (older rows, single-row actions).
    group_id: str
    change_set_id: str | None = None
    at: datetime.datetime
    # Display NAME of the person who made the change. Never an email address.
    actor_name: str | None = None
    source: HistorySource | None = None
    changes: list[AlumniHistoryChange]


class AlumniHistoryPage(BaseModel):
    items: list[AlumniHistoryGroup]
    # Opaque cursor for the next (older) page; ``None`` when there is none.
    # Pass it back as ``?before=``.
    next_before: str | None = None
    # Field-level history only exists from this date (no backfill), so the
    # client can say so instead of looking broken on an older record.
    history_starts: datetime.date

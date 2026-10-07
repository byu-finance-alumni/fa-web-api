"""Per-record version history (#45) — the READ half only.

Reads ``audit_logs`` for ONE alumnus and returns its recorded CHANGES, grouped
one save per entry (``change_set_id``), newest first, keyset-paginated.

What is deliberately left out:

* **Read / disclosure rows.** ``view_profile``, ``search``, ``view_notes``,
  exports and the like are ~72% of the table and record that someone LOOKED,
  not that the record changed. The filter is an ALLOWLIST
  (:data:`HISTORY_ACTIONS`) rather than a denylist, so a new read action added
  later can never leak "who viewed this alum" into a view every editor can open.
  ``tests/test_alumni_history.py`` fails if an alumni audit action is written
  anywhere that is classified as neither a change nor a non-change.
* **Engineer edits.** An engineer's audit rows are rerouted to the append-only
  ``engineer_action_log`` (``app/models/audit.py``), which only super_admin may
  read. Surfacing them here would widen that log to every editor, so they are
  not merged in; the history therefore has no entry for an engineer's edit.
* **Email addresses.** The actor is identified by the display name snapshotted
  onto the row at write time, never ``actor_email``.
* **Restore.** Not built. Each change carries its ``audit_id`` so it can be.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import datetime
import re

from sqlalchemy import String, and_, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InvalidRequestError, NotFoundError
from app.models.alumni import Alumni
from app.models.audit import AuditLog
from app.schemas.alumni_history import (
    AlumniHistoryChange,
    AlumniHistoryGroup,
    AlumniHistoryPage,
)
from app.services.alumni_export import CATALOG

# Field-level capture (change_set_id + section old/new) shipped 2026-08-18;
# nothing before it can be reconstructed, so the UI says where history begins.
HISTORY_STARTS = datetime.date(2026, 8, 18)

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# Every audit action written against an ``alumni`` entity that CHANGES the
# record or something shown on its profile. Only these reach the history.
HISTORY_ACTIONS: frozenset[str] = frozenset(
    {
        # The record itself (alumni_service / survey apply / CSV update).
        "create",
        "update",
        "archive",
        "restore",
        "archive_current_role",
        "apply_survey_response",
        # Headshot.
        "upload_headshot",
        "delete_headshot",
        # Per-row lists on the profile.
        "add_employment",
        "update_employment",
        "delete_employment",
        "add_education",
        "update_education",
        "delete_education",
        "add_leadership",
        "update_leadership",
        "delete_leadership",
        # Tags / status labels / events.
        "add_tag",
        "remove_tag",
        "add_status_label",
        "remove_status_label",
        "add_event_attendance",
        # Timeline content (interactions, tasks, notes) — all editor-visible.
        "add_interaction",
        "update_interaction",
        "delete_interaction",
        "add_task",
        "complete_task",
        "reopen_task",
        "add_note",
        "update_note",
        "delete_note",
    }
)

# Actions written against an ``alumni`` entity that are NOT record changes:
# disclosure reads, exports, upload bookkeeping, survey-campaign state and
# opportunity-link moderation. Listed so the classification test can tell a
# deliberate exclusion from a newly added action nobody classified.
NON_HISTORY_ACTIONS: frozenset[str] = frozenset(
    {
        "view",
        "view_profile",
        "view_notes",
        "view_history",
        "search",
        "preview",
        "export_alumni",
        "export_profile",
        "upload_headshot_started",
        "upload_headshot_rejected",
        "read_survey_alumni_state",
        "reset_survey_campaign",
        "reject_survey_response",
        "add_opportunity_link",
        "approve_opportunity_link",
        "update_opportunity_link",
        "reject_opportunity_link",
        "delete_opportunity_link",
    }
)

# A survey approval writes one summary row ("survey_response=12 fields=8 ...")
# next to its field rows. It is bookkeeping, not a change: it supplies the
# group's source but is not listed as a change when the group has real ones.
_SUMMARY_ACTIONS = frozenset({"apply_survey_response"})

_SOURCES = frozenset({"manual", "import", "survey"})

# ``<section>.<column>`` / bare core column → export-catalog label. The export
# catalog's ``source`` names are exactly the audit section prefixes, so one
# table serves both and the history reads with the same names as the export.
_LABELS: dict[str, str] = {}
for _c in CATALOG:
    _key = _c.attr if _c.source == "alumni" else f"{_c.source}.{_c.attr}"
    _LABELS.setdefault(_key, _c.label)
del _c, _key

_ROW_KINDS = {"employment": "Past role", "education": "Education"}
_ROW_FIELD = re.compile(r"^(?P<kind>[a-z_]+)\[(?P<id>\d+)\](?:\.(?P<field>\w+))?$")


def _humanize(name: str) -> str:
    spaced = name.replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:] if spaced else name


def field_label(field: str | None) -> str | None:
    """Friendly label for an audit ``field_name``; ``None`` for no field."""
    if not field:
        return None
    if field in _LABELS:
        return _LABELS[field]
    m = _ROW_FIELD.match(field)
    if m:
        kind = _ROW_KINDS.get(m["kind"], _humanize(m["kind"]))
        return f"{kind}: {_humanize(m['field'])}" if m["field"] else kind
    section, _, column = field.partition(".")
    if column:
        return _humanize(column)
    return _humanize(field)


def encode_cursor(at: datetime.datetime, head_id: int) -> str:
    raw = f"{at.isoformat()}|{head_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(token: str) -> tuple[datetime.datetime, int]:
    """Parse a ``next_before`` token. Anything malformed is a 422, never a 500."""
    try:
        padded = token + "=" * (-len(token) % 4)
        at_s, _, id_s = base64.urlsafe_b64decode(padded).decode().partition("|")
        at = datetime.datetime.fromisoformat(at_s)
        head_id = int(id_s)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise InvalidRequestError("Invalid history cursor.") from exc
    if head_id < 1 or head_id > 2**63 - 1:
        raise InvalidRequestError("Invalid history cursor.")
    return at, head_id


def _group_key():
    """``change_set_id``, or a per-row key for a row written without one.

    The ``'row:'`` prefix is rendered INLINE, not as a bind parameter: the same
    expression appears in SELECT and GROUP BY, and Postgres only treats the two
    as one expression when their text matches — with a bound literal each
    occurrence gets its own ``$n`` and the query fails ("must appear in the
    GROUP BY clause")."""
    return func.coalesce(
        AuditLog.change_set_id,
        literal_column("'row:'", String) + cast(AuditLog.audit_log_id, String),
    )


async def get_history(
    session: AsyncSession,
    alumni_id: int,
    *,
    can_edit: bool,
    limit: int = DEFAULT_LIMIT,
    before: str | None = None,
    actor_user_id: int | None = None,
) -> AlumniHistoryPage:
    """One page of an alumnus's change history.

    Scoped exactly like the profile read (``profile.get_profile``):
      * an archived record 404s, as the profile does for every role;
      * ``can_edit`` is ``user.can_edit_alumni`` — the same flag the profile
        minimizes on. Every role that holds ``alumni.edit`` by default is an
        editor and sees the whole profile, so it sees every value here. If an
        engineer grants ``alumni.edit`` to a non-editor role, the profile hands
        that caller a minimized aggregate WITHOUT its audit trail, so this read
        returns the shape (who / when / which field) with every value nulled
        and ``redacted`` set, rather than more than the profile would show.

    The read itself is audit-logged (``view_history``), best-effort, like
    ``view_profile``.
    """
    alumnus = await session.get(Alumni, alumni_id)
    if alumnus is None or alumnus.archived:
        raise NotFoundError(f"Alumni {alumni_id} not found.")

    limit = max(1, min(limit, MAX_LIMIT))
    gkey = _group_key()
    scope = and_(
        AuditLog.entity_type == "alumni",
        AuditLog.entity_id == alumni_id,
        AuditLog.action_type.in_(HISTORY_ACTIONS),
    )
    at_col = func.max(AuditLog.created_at)
    head_col = func.max(AuditLog.audit_log_id)
    heads_q = (
        select(gkey.label("gkey"), at_col.label("at"), head_col.label("head_id"))
        .where(scope)
        .group_by(gkey)
    )
    if before is not None:
        c_at, c_id = decode_cursor(before)
        heads_q = heads_q.having(or_(at_col < c_at, and_(at_col == c_at, head_col < c_id)))
    heads_q = heads_q.order_by(at_col.desc(), head_col.desc()).limit(limit + 1)
    heads = (await session.execute(heads_q)).all()

    has_more = len(heads) > limit
    heads = heads[:limit]
    keys = [h.gkey for h in heads]

    rows_by_key: dict[str, list[AuditLog]] = {k: [] for k in keys}
    if keys:
        rows = (
            await session.execute(
                select(gkey.label("gkey"), AuditLog)
                .where(scope, gkey.in_(keys))
                .order_by(AuditLog.audit_log_id)
            )
        ).all()
        for key, row in rows:
            rows_by_key[key].append(row)

    items = [
        _build_group(h.gkey, h.at, rows_by_key.get(h.gkey, []), can_edit=can_edit) for h in heads
    ]
    next_before = encode_cursor(heads[-1].at, heads[-1].head_id) if has_more and heads else None

    if actor_user_id is not None:
        # Best-effort: disclosure logging must never break the read itself.
        # Records WHO read WHICH record's history and the page asked for — never
        # the returned values.
        try:
            session.add(
                AuditLog(
                    user_id=actor_user_id,
                    action_type="view_history",
                    entity_type="alumni",
                    entity_id=alumni_id,
                    field_name=f"limit={limit}" + (";paged" if before else ""),
                )
            )
            await session.commit()
        except Exception:  # noqa: BLE001 - audit is best-effort
            with contextlib.suppress(Exception):
                await session.rollback()

    return AlumniHistoryPage(items=items, next_before=next_before, history_starts=HISTORY_STARTS)


def _build_group(
    key: str, at: datetime.datetime, rows: list[AuditLog], *, can_edit: bool
) -> AlumniHistoryGroup:
    actor_name = next((r.actor_name for r in rows if r.actor_name), None)
    source = next((r.source for r in rows if r.source in _SOURCES), None)
    if source is None and any(r.action_type == "apply_survey_response" for r in rows):
        source = "survey"

    shown = [r for r in rows if r.action_type not in _SUMMARY_ACTIONS] or rows
    changes = [
        AlumniHistoryChange(
            audit_id=r.audit_log_id,
            action=r.action_type,
            field=r.field_name,
            label=field_label(r.field_name),
            old=r.old_value if can_edit else None,
            new=r.new_value if can_edit else None,
            redacted=not can_edit and (r.old_value is not None or r.new_value is not None),
        )
        for r in shown
    ]
    first = rows[0] if rows else None
    return AlumniHistoryGroup(
        group_id=key,
        change_set_id=first.change_set_id if first is not None else None,
        at=at,
        actor_name=actor_name,
        source=source,
        changes=changes,
    )

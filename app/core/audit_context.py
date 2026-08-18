"""Request-scoped audit context: engineer suppression (#199) and write provenance.

Two independent request-scoped facts live here, both carried in contextvars for
the same reason: they must reach EVERY ``audit_logs`` write without being
threaded through every callsite.

1. **Engineer suppression (#199)** — see below.
2. **Write source / provenance (#45)** — whether the rows being written come from
   a hand edit, a bulk CSV import, or a staff approval of an alum's survey
   submission. The first two go through ``alumni_service.update_alumni``, so an
   audit row alone cannot tell them apart; a later restore feature must not
   silently revert a value an import legitimately corrected, nor treat an answer
   the alum gave about themselves as a staff edit. The importer wraps its apply
   loop in ``audit_source_scope(AUDIT_SOURCE_IMPORT)``, the survey review queue
   wraps its apply in ``audit_source_scope(AUDIT_SOURCE_SURVEY)``, and everything
   else defaults to ``manual``.

Also home to ``new_change_set_id()``: the per-save grouping key that makes one
save read as one version (see ``AuditLog.change_set_id``).

The engineer is a super-user / maintenance role. Its actions must NOT clutter
the FERPA audit trail, so no ``audit_logs`` row is written while an engineer is
the acting user. This module carries that decision for the current request in a
contextvar: the auth layer records it once per authenticated request (see
``app/api/dependencies/auth.py``), and a ``before_flush`` hook on the AuditLog
model reads it and drops any pending audit inserts (see ``app/models/audit.py``).

Using a contextvar -- rather than threading the actor's roles through every audit
callsite -- keeps the guard CENTRAL: it covers every existing and future write to
``audit_logs`` without touching the individual callsites. The value is re-set on
each authenticated request, and asyncio copies the context per request task, so
one request's value never leaks into another. It defaults to "not suppressed", so
if it is ever unset the audit trail is preserved (fail toward keeping the record).
"""

import contextlib
import contextvars
import uuid
from collections.abc import Iterable, Iterator

ENGINEER_ROLE = "engineer"

# Provenance values for ``audit_logs.source``. Kept short and stable — they are
# written into the database and a later restore UI reads them back.
AUDIT_SOURCE_MANUAL = "manual"
AUDIT_SOURCE_IMPORT = "import"
# A staff approval of an alum's own survey submission (#45). A THIRD provenance,
# not a flavour of the other two: the values were typed by the alumnus about
# themselves and a staff reviewer only approved them, so the change is neither a
# staff hand edit nor a spreadsheet correction. Restore has to be able to tell
# them apart -- reverting an alum's own correction of their employer is a very
# different act from reverting an import that overwrote it -- and provenance
# cannot be reconstructed after the fact, so it is recorded at write time.
AUDIT_SOURCE_SURVEY = "survey"

_actor_is_engineer: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "audit_actor_is_engineer", default=False
)


def set_audit_actor(roles: Iterable[str] | None) -> None:
    """Record whether the current request's actor is an engineer.

    Called once per authenticated request from the auth layer with the actor's
    role names, so the engineer-detection rule lives in exactly one place.
    """
    _actor_is_engineer.set(ENGINEER_ROLE in set(roles or ()))


def audit_suppressed() -> bool:
    """True when the current actor's ``audit_logs`` writes must be dropped (#199)."""
    return _actor_is_engineer.get()


def reset_audit_actor() -> None:
    """Reset to the default (not suppressed). Used by tests to isolate state."""
    _actor_is_engineer.set(False)


_audit_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "audit_source", default=AUDIT_SOURCE_MANUAL
)


def audit_source() -> str:
    """Provenance to stamp on ``audit_logs`` rows written right now (#45).

    Defaults to ``manual``: the overwhelming majority of writes are hand edits,
    and defaulting that way means a forgotten scope mislabels an import as a hand
    edit rather than mislabelling every hand edit as an import.
    """
    return _audit_source.get()


@contextlib.contextmanager
def audit_source_scope(source: str) -> Iterator[None]:
    """Stamp *source* on every audit row written inside this block.

    Reset via the contextvar TOKEN rather than by re-setting the default, so
    nesting restores the enclosing value instead of collapsing to ``manual``.
    """
    token = _audit_source.set(source)
    try:
        yield
    finally:
        _audit_source.reset(token)


def reset_audit_source() -> None:
    """Reset to the default (``manual``). Used by tests to isolate state."""
    _audit_source.set(AUDIT_SOURCE_MANUAL)


def new_change_set_id() -> str:
    """A fresh change-set id: one save = one version (#45).

    Rows written by the same save share this, which is what makes them groupable.
    Timestamps cannot do the job: Postgres ``now()`` is transaction-start time, so
    a bulk import — which commits thousands of records in ONE transaction
    (``import_csv.commit_update``) — gives every row it writes an identical
    ``created_at``. A uuid4 string keeps the key opaque, collision-free without a
    sequence, and generable client-side before the INSERT.
    """
    return str(uuid.uuid4())

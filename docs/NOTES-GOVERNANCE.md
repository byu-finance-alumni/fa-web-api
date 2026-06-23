# Notes Governance — Design Decisions and FERPA Rationale

Records the governance decisions behind the notes system (#39) so a future FERPA
review or auditor can understand design intent from a written source rather than
inferring it from code comments alone. Each claim below has been verified against
the implementation files cited.

---

## 1. Two distinct kinds of "notes" and their visibility

The application contains two entirely separate data constructs both called "notes."
They have different provenance, different governance, and different visibility rules.
The asymmetry is intentional.

### `alumni.notes` — import-provenance column

`alumni.notes` is a free-text column on the `alumni` table, populated from the CSV
import "Notes" field. Its content is administrative in nature: internal observations
captured at intake, not CRM engagement records. It is therefore classified as
import-provenance data.

This field is included in `VIEW_ONLY_HIDDEN_FIELDS` (`app/schemas/alumni.py`,
lines 493–513) and is NULLED on every alumni read for a `view_only` ("Professor")
caller. The `minimize_alumni_read` function (`app/schemas/alumni.py`, line 516)
applies this nulling whenever `can_edit is False`. The schema comment at lines
504–507 (immediately above the `"notes"` entry at line 508) makes the distinction
explicit:

> "NOTE: this is the alumni record's import-provenance 'Notes' column (CSV intake),
> hidden from view_only. It is DISTINCT from the unified CRM `notes` table (#39),
> whose engagement/interaction/event notes are intentionally visible to view_only
> per the unified-notes spec."

**Rationale:** Faculty have no legitimate educational-interest basis for reading
internal import notes about an alumnus. FERPA minimum-necessary principle requires
that view-only callers receive only the fields needed for their stated purpose (read-
only directory browsing). Import provenance does not meet that bar.

### `notes` table — unified CRM engagement notes (#39)

The unified `notes` table (`database/migrations/2026-06-22_unified_notes.sql`)
stores free-text CRM discussion notes attached to one of three entity levels: an
alumni profile, a logged interaction, or an event. These are operational engagement
records — the kind of notes a faculty advisor or staff member would write after
meeting an alumnus.

**Read access is intentionally open to every view-access role, including
`view_only` / Professor.** The GET endpoint (`app/api/routes/notes.py`, line 26)
uses `RequireViewAccess`, not `RequireFullAccess`. The docstring confirms this is a
deliberate decision consistent with the unified-notes spec.

**Write access (create / edit / delete) requires `full_access` and up.** The POST,
PATCH, and DELETE endpoints all use `RequireFullAccess` (`app/api/routes/notes.py`,
lines 41, 52, 63). The `student` role is deliberately excluded from writes, again
matching the spec.

**The inconsistency between the two constructs is intentional.** Import-provenance
notes are hidden from faculty because they contain administrative intake data
unrelated to the faculty role. CRM engagement notes are visible to faculty because
their purpose is precisely to support faculty-facing engagement — a Professor reading
who met with an alumnus and what was discussed is within their stated use of the
system. These are different categories of data requiring different treatment, not an
oversight.

---

## 2. Edit/delete ownership policy

Any `full_access` user may edit or delete any note, regardless of authorship. There
is no author-only gate. This decision was made deliberately and consistently with
how the rest of the CRM treats shared institutional records (e.g., interactions are
edited by any full_access user, not locked to the logging officer).

The `update_note` service function (`app/services/notes.py`, line 220) states:

> "Any `full_access` user may edit any note (notes are a shared institutional
> record, mirroring how interactions are edited) — the change is fully audited
> (old + new value) so the FERPA trail attributes it."

The privacy control that makes this acceptable is the audit trail, not authorship
restriction:

- **On edit:** `update_note` (`app/services/notes.py`, lines 236–248) captures the
  prior body into `old_value` and the replacement into `new_value` before committing
  the change. The `_audit` helper writes an `update_note` row to `audit_logs`.
- **On delete:** `delete_note` (`app/services/notes.py`, lines 263–274) snapshots
  the full note body into `old_value` BEFORE the delete executes, then writes a
  `delete_note` audit row. A future FERPA review can reconstruct exactly what was
  removed and who removed it.

A no-op edit (body unchanged) writes neither a DB row update nor an audit entry,
preventing spurious noise in the trail.

Gating writes to author-only was considered and deliberately not adopted. Author-only
restriction would create inconsistency with interaction editing, complicate the UI
(surfacing or hiding controls per-note based on session identity), and is not needed
when every write is fully attributable and the prior content is preserved. The FERPA
recordkeeping requirement is met by the audit snapshot, not by ownership locks.

---

## 3. Legacy per-row note columns — additive model decision

Two pre-existing columns capture a note at the moment of logging an activity:

- `interactions.interaction_notes` — the note recorded when logging an interaction
  (`database/schema.sql`, line 377).
- `events.event_notes` — the note recorded when creating an event record
  (`database/schema.sql`, line 406).

These columns are **retained as-is**. They are not migrated into the unified `notes`
table. The migration comment (`database/migrations/2026-06-22_unified_notes.sql`,
lines 12–13) records this explicitly:

> "NOTE: this is ADDITIVE. The existing interaction_notes / event_notes columns are
> left in place; migrating them into this table is a separate follow-up."

The unified notes attached to interactions and events are therefore **additive
"discussion / follow-up" notes** shown in a separate UI disclosure, not a
replacement for the inline primary captured note.

**Rationale for the additive model (no migration):**

1. **Duplication risk.** Backfilling `interaction_notes` / `event_notes` into the
   notes feed would cause the primary note to appear twice: once inline in the
   interaction/event record, and again in the unified notes list. The UI would need
   deduplication logic with no user-visible benefit.
2. **Data migration risk.** Fully switching the create flow so that new interactions
   write the `notes` table instead of `interaction_notes` is an in-review feature
   change that is deferred as a future option. Doing it prematurely risks stranding
   data in one column while reads look in the other.
3. **No compliance gap.** The legacy columns are readable by authorized roles through
   the interaction/event read endpoints. The unified notes table adds a new surface;
   it does not replace or obscure existing data.

**Resolution:** The "unify legacy notes" follow-up is resolved as: additive model,
legacy columns retained, future migration deferred pending a formal design decision.
This document is the written record of that resolution so it is not re-litigated in
future code review.

---

## 4. FERPA recordkeeping infrastructure

Two cross-cutting controls underpin the notes governance above:

**Read-disclosure audit (`view_notes`).** Every call to `list_notes`
(`app/services/notes.py`, lines 174–189) emits a `view_notes` audit row recording
the actor, entity type, entity id, and note count. This is best-effort (a logging
failure does not break the read), but it means a FERPA review can answer "who read
the notes on this alumni record?" — the same capability that exists for
`view_profile` and `search` disclosures. Event-level notes audit against the event;
alumni-level and interaction-level notes audit against the owning alumni, so all
notes activity surfaces in the alumni profile Audit tab.

**Actor-identity snapshot trigger (`2026-06-17_audit_actor_snapshot.sql`).** The
`audit_logs` table carries `user_id ON DELETE SET NULL`, which would anonymize the
actor record if the staff user were later deleted. The `trg_audit_logs_snapshot_actor`
trigger snapshots `actor_email` and `actor_name` onto every audit row at INSERT time.
The snapshot is immutable: it is never overwritten on update, and the trigger only
fires when `actor_email IS NULL`, so an explicit value is preserved. This means
note-write audit records retain the actor's identity even after the user account is
deleted — a FERPA requirement for disclosure records.

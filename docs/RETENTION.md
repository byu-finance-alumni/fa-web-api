# Data Retention & Erasure Policy

_Status: recorded 2026-07-03 (fleet audit #175). This documents current, deliberate
behavior; a formal institutional policy should ratify or amend it before real alumni
data is loaded._

## Alumni records — soft-delete by design

`DELETE /alumni/{id}` is a **soft-delete (archive)**, never a hard delete
(`app/api/routes/alumni.py`, `app/services/alumni.py::archive_alumni`). Records are
flagged `archived = true` and removed from the directory/search, list, geography, and
dashboard surfaces, but the row and its PII are retained in the database.

This is intentional: the audit trail (`audit_logs`) references alumni records, and
FERPA-relevant disclosure history depends on those records remaining resolvable. A
hard delete would orphan or erase that trail. FERPA itself does **not** mandate
erasure, so retaining archived records is a defensible institutional-recordkeeping
choice — but it is a *choice*, recorded here so the "no erasure path" is a documented
decision rather than a silent gap.

Archived records:
- are excluded from every read surface for all roles (a `view_only`/`student` caller
  cannot resurface them even by passing `include_archived=true` — the flag is AND'd
  away server-side);
- cannot be edited via `PATCH /alumni/{id}` (returns 404, symmetric with GET — restore
  first via `POST /alumni/{id}/restore`);
- still count toward the `archived` KPI and remain visible to `full_access`+ via the
  explicit archived view.

## Right-to-be-forgotten / PII scrub

There is currently **no hard-delete or PII-scrub endpoint** for alumni records. If a
genuine erasure obligation arises for a subset of records, the recommended
implementation (not yet built) is a `super_admin`/`engineer`-gated **scrub** that:
- nulls the PII columns (name, IDs, DOB, contact, addresses, notes) in place;
- preserves the row's `alumni_id` and audit *metadata* (who/when/what-action), mirroring
  the actor-email snapshot pattern already used for staff-account deletion
  (`super-admin permanent delete-user`), so the FERPA disclosure trail survives the scrub.

This keeps deletion auditable while removing the underlying personal data — the correct
shape for a right-to-be-forgotten request against an audited system. It is left as a
tracked follow-up rather than shipped speculatively, since the destructive path needs an
explicit institutional policy and careful review before it exists.

## Survey send log and email bounce events

`survey_send_log` keeps one row per survey email sent. Since #858 each row also
holds the address the email went to (`sent_to`) and Resend's message id
(`resend_email_id`), and `survey_email_events` holds the bounce/complaint events
Resend reports for those emails (event type, bounce type/subtype, when, the alum
it was matched to — no address, no raw payload). Both are **retained with the
send log**: nothing prunes them, an archived alum's rows stay, and deleting an
alum removes their send-log rows (`ON DELETE CASCADE`) while their events are kept
unattributed (`alumni_id` set NULL). A PII scrub (above) must null
`survey_send_log.sent_to` as well.

## Staff/user accounts

Staff accounts already support permanent deletion (`super_admin`/`engineer`, two-step
email confirmation) with an actor-email/name snapshot trigger so the audit trail
survives the account's removal. See the super-admin provisioning/deletion flow.

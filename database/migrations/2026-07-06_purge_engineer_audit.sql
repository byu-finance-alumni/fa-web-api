-- Purge existing engineer actions from the audit trail (#199).
--
-- The engineer is a super-user / maintenance role; its actions must NOT clutter
-- the FERPA audit trail. Going forward the API no longer writes an audit_logs row
-- when the acting user is an engineer (a before_flush guard reading a per-request
-- contextvar; see app/core/audit_context.py + app/models/audit.py). This one-off
-- migration removes the engineer rows already recorded before that guard shipped.
--
-- An engineer actor is recoverable from a row two ways, and we delete on either:
--   1. user_id still points at a user who currently holds the engineer role
--      (the normal case), OR
--   2. actor_email (snapshotted at INSERT by trg_audit_logs_snapshot_actor, see
--      2026-06-17_audit_actor_snapshot.sql) matches a current engineer's email --
--      this catches rows whose user_id was later SET NULL by the actor's deletion
--      but whose snapshot survives. Matched case-insensitively to be safe.
--
-- Rows authored by a since-deleted engineer who no longer exists cannot be
-- attributed (their role is gone with them) and are intentionally left untouched.
BEGIN;

DELETE FROM audit_logs a
USING user_roles ur
JOIN roles r ON r.role_id = ur.role_id
WHERE a.user_id = ur.user_id
  AND r.role_name = 'engineer';

DELETE FROM audit_logs a
WHERE a.actor_email IS NOT NULL
  AND lower(a.actor_email) IN (
      SELECT lower(u.email)
        FROM users u
        JOIN user_roles ur ON ur.user_id = u.user_id
        JOIN roles r ON r.role_id = ur.role_id
       WHERE r.role_name = 'engineer'
  );

COMMIT;

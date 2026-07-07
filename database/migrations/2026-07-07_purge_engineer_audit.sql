-- MOVE existing engineer actions out of the audit trail, preserving them (#199,
-- and the Medium follow-up from the #199/#200 security review).
--
-- The engineer is a super-user / maintenance role; its actions must NOT clutter
-- the FERPA record-change trail. Going forward the API no longer writes an
-- audit_logs row when the acting user is an engineer -- it reroutes the row into
-- the append-only engineer_action_log instead (a before_flush guard reading a
-- per-request contextvar; see app/core/audit_context.py + app/models/audit.py).
--
-- This one-off migration reconciles the rows already recorded in audit_logs BEFORE
-- that guard shipped. The ORIGINAL version DELETEd them (over-broad: it destroyed
-- forensic history keyed on the actor's CURRENT role). Instead we now MOVE them:
-- copy each engineer-authored row into engineer_action_log, THEN delete it from
-- audit_logs -- decluttering the audit UI WITHOUT losing the record.
--
-- An engineer actor is recoverable from a row two ways, and we move on either:
--   1. user_id still points at a user who currently holds the engineer role
--      (the normal case), OR
--   2. actor_email (snapshotted at INSERT by trg_audit_logs_snapshot_actor, see
--      2026-06-17_audit_actor_snapshot.sql) matches a current engineer's email --
--      this catches rows whose user_id was later SET NULL by the actor's deletion
--      but whose snapshot survives. Matched case-insensitively to be safe.
--
-- Rows authored by a since-deleted engineer who no longer exists cannot be
-- attributed (their role is gone with them) and are intentionally left untouched.
--
-- DEPENDS ON 2026-07-07_engineer_action_log.sql (creates the destination table);
-- lexical filename order runs 'engineer_action_log' before 'purge_engineer_audit'.
BEGIN;

-- Identify the engineer-authored rows ONCE, so the copy and the delete operate on
-- exactly the same set (ON COMMIT DROP: the temp table is gone at COMMIT).
CREATE TEMP TABLE _engineer_audit_ids ON COMMIT DROP AS
SELECT a.audit_log_id
  FROM audit_logs a
 WHERE a.user_id IN (
           SELECT ur.user_id
             FROM user_roles ur
             JOIN roles r ON r.role_id = ur.role_id
            WHERE r.role_name = 'engineer'
       )
    OR (a.actor_email IS NOT NULL
        AND lower(a.actor_email) IN (
               SELECT lower(u.email)
                 FROM users u
                 JOIN user_roles ur ON ur.user_id = u.user_id
                 JOIN roles r ON r.role_id = ur.role_id
                WHERE r.role_name = 'engineer'
           ));

-- 1. Copy into the tamper-resistant engineer_action_log (preserve, don't destroy).
--    actor_email carries over the existing snapshot; the destination trigger only
--    fills it when NULL, so an already-snapshotted value is kept as-is.
INSERT INTO engineer_action_log (
    actor_user_id, actor_email, action_type, entity_type,
    entity_id, field_name, old_value, new_value, occurred_at
)
SELECT a.user_id, a.actor_email, a.action_type, a.entity_type,
       a.entity_id, a.field_name, a.old_value, a.new_value, a.created_at
  FROM audit_logs a
  JOIN _engineer_audit_ids m ON m.audit_log_id = a.audit_log_id;

-- 2. Remove them from audit_logs so the record-change trail / UI is decluttered.
DELETE FROM audit_logs a
 USING _engineer_audit_ids m
 WHERE a.audit_log_id = m.audit_log_id;

COMMIT;

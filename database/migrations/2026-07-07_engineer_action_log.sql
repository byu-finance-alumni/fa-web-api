-- Tamper-resistant engineer-action log (#199 / #200 forensic blind spot).
--
-- Since #199 an engineer actor's audit_logs writes are suppressed (a before_flush
-- guard drops the pending AuditLog; see app/models/audit.py) so engineer
-- maintenance actions don't clutter the FERPA record-change trail. Combined with
-- the engineer-only DELETE /admin/logins purge (#200), dropping them outright left
-- ZERO forensic trace of engineer create / assign-role / delete-user actions -- an
-- engineer could act invisibly. A security review flagged this High.
--
-- This table closes the blind spot. The same before_flush guard now REROUTES each
-- suppressed engineer AuditLog into an equivalent row here instead of discarding
-- it. It is APPEND-ONLY and tamper-resistant BY DESIGN:
--   * there is NO delete / purge route and no view-gate an engineer can flip;
--   * DELETE /admin/logins (#200) deliberately does NOT touch it;
--   * only the super_admin role can READ it (GET /admin/engineer-actions) -- the
--     engineer (the audited party) cannot read, delete, or disable it.
--
-- Columns mirror audit_logs. actor_email is snapshotted at INSERT by the trigger
-- below (mirrors trg_audit_logs_snapshot_actor, 2026-06-17_audit_actor_snapshot.sql)
-- so a row survives the actor's later deletion (actor_user_id -> NULL) with
-- attribution intact.
--
-- ORDERING: this migration MUST run before 2026-07-07_purge_engineer_audit.sql,
-- which MOVES existing engineer audit_logs rows INTO this table (INSERT ... SELECT).
-- Lexical filename order (migrate.sh) puts 'engineer_action_log' before
-- 'purge_engineer_audit' on the same date, so the table exists first.
BEGIN;

CREATE TABLE IF NOT EXISTS engineer_action_log (
    engineer_action_log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_user_id bigint,
    -- Snapshotted at INSERT by the trigger below so it survives the actor's later
    -- deletion (actor_user_id -> NULL).
    actor_email   varchar(255),
    action_type   varchar(100) NOT NULL,
    entity_type   varchar(100) NOT NULL,
    entity_id     bigint,
    field_name    varchar(255),
    old_value     text,
    new_value     text,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_engineer_action_log_actor_user_id
        FOREIGN KEY (actor_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- GET /admin/engineer-actions lists newest-first and pages; index the sort column.
CREATE INDEX IF NOT EXISTS idx_engineer_action_log_occurred_at
    ON engineer_action_log (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_engineer_action_log_actor_user_id
    ON engineer_action_log (actor_user_id);

-- Snapshot the acting user's email onto each row at write time, so a later user
-- deletion (actor_user_id -> NULL) never erases who performed the action. Mirrors
-- audit_logs_snapshot_actor (2026-06-17_audit_actor_snapshot.sql).
CREATE OR REPLACE FUNCTION engineer_action_log_snapshot_actor()
RETURNS trigger AS $$
BEGIN
    IF NEW.actor_email IS NULL AND NEW.actor_user_id IS NOT NULL THEN
        SELECT u.email
          INTO NEW.actor_email
          FROM users u
         WHERE u.user_id = NEW.actor_user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_engineer_action_log_snapshot_actor ON engineer_action_log;
CREATE TRIGGER trg_engineer_action_log_snapshot_actor
    BEFORE INSERT ON engineer_action_log
    FOR EACH ROW
    EXECUTE FUNCTION engineer_action_log_snapshot_actor();

-- Deny-all RLS like every other public table (Supabase auto-exposes the public
-- schema via its Data API; the backend bypasses RLS with a privileged role).
-- Mirrors database/rls_lockdown.sql. No policies = deny-all. Idempotent.
ALTER TABLE engineer_action_log ENABLE ROW LEVEL SECURITY;

COMMIT;

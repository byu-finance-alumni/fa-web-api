-- Login history (security log). Logins happen client-side via Supabase, so the
-- backend never recorded them and users.last_login_at was always NULL. The
-- frontend now calls POST /auth/login on a successful sign-in, which stamps
-- users.last_login_at and inserts one row here.
--
-- Kept DELIBERATELY SEPARATE from audit_logs: sign-in events are a security log,
-- not the record-change audit trail (the Audit page states as much), and folding
-- thousands of logins into audit_logs would bury record-change forensics.
--
-- email is snapshotted at insert and user_id is ON DELETE SET NULL, so the login
-- history survives a later user deletion with its attribution intact (mirrors the
-- audit actor-snapshot approach in 2026-06-17_audit_actor_snapshot.sql).
BEGIN;

CREATE TABLE IF NOT EXISTS login_events (
    login_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        bigint,
    email          varchar(255) NOT NULL,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_login_events_user_id
        FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- The Logins tab lists newest-first and pages; index the sort/filter columns.
CREATE INDEX IF NOT EXISTS idx_login_events_occurred_at
    ON login_events (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_login_events_user_id
    ON login_events (user_id);

-- Deny-all RLS like every other public table (Supabase auto-exposes the public
-- schema via its Data API; the backend bypasses RLS with a privileged role).
-- Mirrors database/rls_lockdown.sql. Idempotent.
ALTER TABLE login_events ENABLE ROW LEVEL SECURITY;

COMMIT;

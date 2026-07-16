-- =============================================================================
-- Migration: add `login_failures` (per-attempt failed-login security log)
-- Date: 2026-07-16
-- -----------------------------------------------------------------------------
-- Today a failed login only bumps the aggregate per-email counter in
-- `login_attempts` (which drives the cooldown/lock) and leaves NO per-attempt
-- trail: no row per failure, no IP, no time you can list. This adds a per-attempt
-- FAILURE log so an engineer can see who failed, when, and from what IP.
--
-- Kept SEPARATE from both `login_events` (successful sign-ins) and
-- `login_attempts` (the rolling counter): those don't preserve per-attempt
-- forensics. `email` is the attempted address, snapshotted at insert. There is
-- deliberately NO foreign key to `users`: a failure may be for an email with no
-- account at all (a probe / typo), which is still worth logging -- mirrors
-- `login_attempts`' deliberate lack of a FK.
--
-- SAFE TO RE-RUN: CREATE TABLE / INDEX IF NOT EXISTS are idempotent.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS login_failures (
    login_failure_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email            varchar(255) NOT NULL,
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    -- Client IP + approximate (IP-based) location forwarded by the Next.js login
    -- action (x-forwarded-for + Vercel geo headers). Nullable: absent in local
    -- dev / when the client forwards no context.
    ip_address       varchar(64),
    city             varchar(128),
    region           varchar(128),
    country          varchar(64),
    -- Coarse failure reason (e.g. a Supabase auth error code), optional.
    reason           varchar(64)
);

-- The Login-failures tab lists newest-first and pages; index the sort column.
CREATE INDEX IF NOT EXISTS idx_login_failures_occurred_at
    ON login_failures (occurred_at DESC);

-- Deny-all RLS like every other public table (Supabase auto-exposes the public
-- schema via its Data API; the backend bypasses RLS with a privileged role).
-- Enabling RLS with NO policies = deny-all for the anon/authenticated API roles.
-- Mirrors database/rls_lockdown.sql. Idempotent.
ALTER TABLE login_failures ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if the table must be dropped):
-- =============================================================================
-- DROP TABLE IF EXISTS login_failures;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
-- WHERE schemaname = 'public' AND tablename = 'login_failures';

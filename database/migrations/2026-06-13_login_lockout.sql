-- =============================================================================
-- Migration: login lockout (cooldown + hard-lock) state
-- Date: 2026-06-13
-- -----------------------------------------------------------------------------
-- Adds the persistence backing the pre-login throttling/lockout flow
-- (see app/services/login_lockout.py):
--
--   * users.locked_at / users.locked_reason — a HARD lock on a registered
--     account after too many failed logins. While locked_at is set, the account
--     is denied at precheck regardless of credentials, until a super_admin
--     resets the password (which clears these columns).
--
--   * login_attempts — the rolling per-email failed-login counter that drives
--     the short COOLDOWN and, for registered emails, the hard lock. Keyed by the
--     lowercased email so the counter is case-insensitive. Unregistered emails
--     also get rows here (cooldown applies to everyone, to avoid leaking which
--     emails are registered), but they are NEVER hard-locked.
--
-- Authoritative throttling lives in the application layer; this is its store.
--
-- NOTE: login_attempts.email_lc is NOT a foreign key to users — by design it
-- tracks attempts against arbitrary (possibly non-existent) emails so the
-- cooldown path cannot be used to enumerate which emails are registered.
-- =============================================================================

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS locked_at     timestamptz,
    ADD COLUMN IF NOT EXISTS locked_reason text;

CREATE TABLE IF NOT EXISTS login_attempts (
    email_lc        text PRIMARY KEY,
    failed_count    int NOT NULL DEFAULT 0,
    first_failed_at timestamptz,
    last_failed_at  timestamptz,
    cooldown_until  timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- New table must get deny-all RLS to match rls_lockdown.sql: Supabase auto-
-- exposes every public table via the Data API reachable with the anon key, and
-- this table (which reveals failed-login activity per email) must never be
-- readable that way. Enabling RLS with no policies = deny-all for anon /
-- authenticated; the backend's privileged role bypasses RLS and is unaffected.
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- Migration: force password change on first login
-- Date: 2026-06-16
-- -----------------------------------------------------------------------------
-- Adds users.must_change_password, the flag backing the "force a password
-- change on next login" flow:
--
--   * Set true when an account is provisioned with a one-time temp password
--     (POST /admin/users) or when a super_admin resets a password
--     (POST /admin/users/{id}/reset-password).
--   * Exposed on GET /auth/context so the frontend can gate the user into a
--     set-a-new-password screen.
--   * Cleared by the authenticated user themselves via POST
--     /auth/password/complete after they set a new password client-side.
--
-- Authoritative in the application layer; this column is its store. Existing
-- rows default to false (NOT forced to change).
-- =============================================================================

BEGIN;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false;

COMMIT;

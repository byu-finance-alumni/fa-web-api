-- =============================================================================
-- Migration: add free-text profile_updated_by NAME to alumni
-- Date: 2026-07-08
-- -----------------------------------------------------------------------------
-- Adds a single nullable string `profile_updated_by` on the alumni record
-- holding the free-text "updated by" NAME from the intake sheet (the person who
-- last updated the profile, as typed).
--
-- This is DISTINCT from profile_updated_by_user_id
-- (2026-07-08_add_alumni_survey_citizenship_grad_fields.sql): that column is the
-- resolved app-user FK (users.user_id); this one stores the raw name verbatim as
-- free text and backs the "Profile updated by ..." hover fallback when no user
-- FK is linked.
--
-- The column lives on the existing `alumni` table, which already has RLS enabled
-- (see rls_lockdown.sql), so no new RLS / lockdown step is needed here -- adding
-- a column inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS profile_updated_by varchar(200);

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type, character_maximum_length
-- FROM information_schema.columns
-- WHERE table_name = 'alumni'
--   AND column_name = 'profile_updated_by';

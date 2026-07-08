-- =============================================================================
-- Migration: add nullable graduation_month to alumni
-- Date: 2026-07-07
-- -----------------------------------------------------------------------------
-- Adds a separate nullable INT `graduation_month` (1-12) alongside the existing
-- `graduation_year`. This lets an alumni record capture the month of graduation
-- (e.g. April / August / December) without changing the year column.
--
-- Value range (1-12) is enforced at the application layer (AlumniBase validator);
-- the column itself is a plain nullable int, matching graduation_year.
--
-- The column lives on the existing `alumni` table, which already has RLS enabled
-- (see rls_lockdown.sql), so no new RLS / lockdown step is needed here -- adding
-- a column inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE alumni ADD COLUMN IF NOT EXISTS graduation_month int;

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_name = 'alumni' AND column_name = 'graduation_month';

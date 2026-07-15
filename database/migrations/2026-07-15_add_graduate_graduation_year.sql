-- =============================================================================
-- Migration: add `alumni.graduate_graduation_year`
-- Date: 2026-07-15
-- -----------------------------------------------------------------------------
-- Adds a single nullable integer `graduate_graduation_year` on the alumni record
-- holding the graduation year of a GRADUATE program. This is DISTINCT from the
-- existing undergrad `graduation_year` column: an alumnus can hold both an
-- undergrad grad year and a separate graduate-program grad year, and the
-- redesigned edit form captures each independently.
--
-- The column lives on the existing `alumni` table, which already has RLS enabled
-- (see rls_lockdown.sql), so no new RLS / lockdown step is needed here -- adding
-- a column inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS graduate_graduation_year int;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if the column must be dropped):
-- =============================================================================
-- ALTER TABLE alumni DROP COLUMN IF EXISTS graduate_graduation_year;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_name = 'alumni'
--   AND column_name = 'graduate_graduation_year';

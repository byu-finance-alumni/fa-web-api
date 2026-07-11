-- =============================================================================
-- Migration: add profile fields `alumni.hometown` + `current_employment.company_address`
-- Date: 2026-07-10
-- -----------------------------------------------------------------------------
-- Two nullable string columns the redesigned profile's 3 info columns render
-- (#366):
--   * alumni.hometown                  -- home town of ORIGIN (paired with the
--                                          existing home_country origin field;
--                                          distinct from the current-address city)
--   * current_employment.company_address -- the "Company Address" street line
--                                          (city/state/country/zip already exist)
--
-- Both live on existing tables (`alumni`, `current_employment`) that already have
-- RLS enabled, so adding a column inherits each table's deny-all policy for the
-- Supabase API roles -- no new RLS / lockdown step is needed here.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS hometown varchar(100);

ALTER TABLE current_employment
    ADD COLUMN IF NOT EXISTS company_address varchar(255);

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT table_name, column_name, data_type, character_maximum_length
-- FROM information_schema.columns
-- WHERE (table_name = 'alumni' AND column_name = 'hometown')
--    OR (table_name = 'current_employment' AND column_name = 'company_address');

-- =============================================================================
-- Migration: add secondary affiliation / education fields to alumni
-- Date: 2026-06-29
-- -----------------------------------------------------------------------------
-- Supports additional professional/educational status per the PRD (section 6):
-- MBA program, law school, medical school, graduate school, startup
-- involvement, advisory roles, and secondary employment. These extend the
-- alumni record beyond the existing core program/employment fields
-- (byu-finance-alumni/fa-web-api#47).
--
-- All columns are OPTIONAL / NULLABLE additive fields, so this is a non-breaking
-- change. Short single-value fields (school / program names) are varchar(255),
-- matching the related-table conventions; the narrative fields (free-text
-- descriptions of startup involvement, advisory roles, and any secondary
-- employment) are text, mirroring how `notes` is modeled.
--
-- All columns live on the existing `alumni` table, which already has RLS enabled
-- (see rls_lockdown.sql), so no new RLS / lockdown step is needed here -- adding
-- columns inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS throughout is idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS mba_program          varchar(255),
    ADD COLUMN IF NOT EXISTS law_school           varchar(255),
    ADD COLUMN IF NOT EXISTS medical_school       varchar(255),
    ADD COLUMN IF NOT EXISTS graduate_school      varchar(255),
    ADD COLUMN IF NOT EXISTS startup_involvement  text,
    ADD COLUMN IF NOT EXISTS advisory_roles       text,
    ADD COLUMN IF NOT EXISTS secondary_employment text;

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type, character_maximum_length
-- FROM information_schema.columns
-- WHERE table_name = 'alumni'
--   AND column_name IN ('mba_program','law_school','medical_school',
--       'graduate_school','startup_involvement','advisory_roles',
--       'secondary_employment')
-- ORDER BY column_name;

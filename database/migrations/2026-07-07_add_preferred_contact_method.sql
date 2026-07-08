-- =============================================================================
-- Migration: add nullable preferred_contact_method to alumni_contact_info
-- Date: 2026-07-07
-- -----------------------------------------------------------------------------
-- Adds a single nullable string `preferred_contact_method` on the contact row
-- naming WHICH contact method the alum flags as "preferred". The frontend stars
-- that method and surfaces it in the profile header.
--
-- Allowed values (personal_email / work_email / phone / linkedin, or NULL for
-- none) are validated at the application layer (see ContactCreate in
-- app/schemas/alumni.py); the column itself is a plain nullable varchar(30).
--
-- The column lives on the existing `alumni_contact_info` table, which already
-- has RLS enabled, so no new RLS / lockdown step is needed here -- adding a
-- column inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE alumni_contact_info
    ADD COLUMN IF NOT EXISTS preferred_contact_method varchar(30);

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type, character_maximum_length
-- FROM information_schema.columns
-- WHERE table_name = 'alumni_contact_info'
--   AND column_name = 'preferred_contact_method';

-- =============================================================================
-- Migration: add nullable best_contact to alumni_contact_info
-- Date: 2026-07-08
-- -----------------------------------------------------------------------------
-- Adds a single nullable string `best_contact` on the contact row holding the
-- literal "best contact" VALUE from the intake sheet (a phone number or email
-- the alum flagged as their best point of contact).
--
-- This is DISTINCT from preferred_contact_method (2026-07-07_add_preferred_
-- contact_method.sql): that column only NAMES which method is preferred
-- (personal_email / work_email / phone / linkedin); this one stores the actual
-- value verbatim as free text.
--
-- The column lives on the existing `alumni_contact_info` table, which already
-- has RLS enabled, so no new RLS / lockdown step is needed here -- adding a
-- column inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE alumni_contact_info
    ADD COLUMN IF NOT EXISTS best_contact varchar(255);

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type, character_maximum_length
-- FROM information_schema.columns
-- WHERE table_name = 'alumni_contact_info'
--   AND column_name = 'best_contact';

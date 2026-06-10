-- =============================================================================
-- Migration: drop PIFF donor amount
-- Date: 2026-06-09
-- -----------------------------------------------------------------------------
-- The department does not track donation amounts, only whether someone is a
-- PIFF donor (the `piff_donor` boolean stays). Drop the unused amount column
-- from the program-engagement profile.
--
-- SAFE TO RE-RUN: DROP COLUMN IF EXISTS is idempotent. This removes a column
-- that holds no real data yet (mock only), so there's nothing to preserve.
-- =============================================================================

BEGIN;

ALTER TABLE alumni_program_engagement
    DROP COLUMN IF EXISTS piff_donor_amount;

COMMIT;

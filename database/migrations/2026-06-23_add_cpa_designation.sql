-- =============================================================================
-- Migration: add cpa_designation to alumni_program_engagement
-- Date: 2026-06-23
-- -----------------------------------------------------------------------------
-- The program-engagement profile already tracks the CFP and CFA professional
-- designations (cfp_designation, cfa_designation). Adds the matching CPA flag so
-- alumni search can filter by it (byu-finance-alumni/fa-web-app#159). Mirrors the
-- existing designation columns exactly: boolean, NOT NULL, defaults false.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent.
-- =============================================================================

BEGIN;

ALTER TABLE alumni_program_engagement
    ADD COLUMN IF NOT EXISTS cpa_designation boolean NOT NULL DEFAULT false;

COMMIT;

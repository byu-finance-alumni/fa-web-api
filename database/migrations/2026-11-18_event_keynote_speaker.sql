-- =============================================================================
-- Migration: Keynote Speaker on an event (api#695)
-- Date: 2026-11-18
-- -----------------------------------------------------------------------------
-- Each event gets an optional Keynote Speaker: a plain typed name, NOT linked
-- to a profile (2026-11-18 meeting). Shown on the event Overview, set and
-- edited with the rest of the event's details.
--
-- WIDENING ONLY: one nullable column, no default, no backfill. Old code ignores
-- it, so this is safe to apply before or after the code that reads it (still:
-- ship this file to prod first, per the README's promotion order).
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS. No new table, so no RLS change
-- (events already has deny-all RLS from rls_lockdown.sql).
-- =============================================================================

BEGIN;

ALTER TABLE events
    ADD COLUMN IF NOT EXISTS keynote_speaker varchar(255);

COMMENT ON COLUMN events.keynote_speaker IS
    'Optional keynote speaker name, plain text (not a profile link). api#695.';

COMMIT;

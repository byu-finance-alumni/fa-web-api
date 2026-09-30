-- =============================================================================
-- Migration: record how long an alum spent filling the survey
-- Date: 2026-09-30
-- -----------------------------------------------------------------------------
-- The survey console can say how MANY alumni replied and what happened to each
-- submission, but not how long it took them to fill the thing in. This adds the
-- one nullable column the frontend's active-time timer writes at submit, so the
-- Progress table can surface a median time-to-complete per campaign.
--
-- ACTIVE time, not wall time: the browser accumulates seconds only while the tab
-- is visible and pauses on `visibilitychange`, so "opened it, went to lunch,
-- came back" does not inflate the number.
--
-- ⚠️ ATTACKER-CONTROLLABLE. The value rides the PUBLIC, token-gated submit, so it
-- is whatever the poster sends. It is NEVER trusted: `survey_responses`
-- (`_sane_fill_seconds`) clamps it and DROPS anything negative or absurd to NULL
-- before it is ever staged. The CHECK below is a defensive floor only — it
-- refuses a stored negative — not the real bound, which lives in the service so
-- it can move without a migration.
--
-- WHY NULLABLE, AND WHY NO BACKFILL. Like `cycle_seq`/`stage` (#497), the value
-- is unknowable for a row that predates it: no timer ran when the response was
-- submitted, so there is no figure to backfill and a guessed one would be
-- indistinguishable from a measured one in a report. A confirmation ("yes,
-- everything is correct") also carries no timer and stays NULL. NULL reads as
-- "not measured", which is the truth, and is trivially excluded from the median.
--
-- WIDENING ONLY, so it is BACKWARD-COMPATIBLE with the code already running and
-- safe in the CI migrate-trails-Vercel gap:
--
--   * OLD code + NEW schema: fine. Nothing before this change writes the column;
--     a nullable ADD COLUMN touches, reads and rewrites no existing row.
--   * NEW code + OLD schema: a submission carrying `fill_seconds` would fail to
--     INSERT for the length of the gap. So ship THIS migration to prod on its
--     own, BEFORE the code that writes the column. Same rule as every other
--     schema-dependent change here.
--
-- SAFE ON EXISTING ROWS: one `ADD COLUMN IF NOT EXISTS` of a nullable column
-- (Postgres 11+ adds it without a table rewrite) and a CHECK re-created with a
-- predicate every existing row already satisfies (they are all NULL). Re-runnable
-- (`IF NOT EXISTS`, `DROP CONSTRAINT IF EXISTS` before the ADD).
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

-- Active seconds the alum spent filling the survey. NULL = not measured or not
-- usable; the service clamps the public, attacker-supplied value and drops
-- anything out of range to NULL before it reaches here.
ALTER TABLE survey_responses
    ADD COLUMN IF NOT EXISTS fill_seconds integer;

ALTER TABLE survey_responses
    DROP CONSTRAINT IF EXISTS ck_survey_responses_fill_seconds;
ALTER TABLE survey_responses
    ADD CONSTRAINT ck_survey_responses_fill_seconds
        CHECK (fill_seconds IS NULL OR fill_seconds >= 0);

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand). Non-destructive to anything that existed before this
-- migration -- it only discards fill times captured after it, which stop being
-- recoverable once dropped, so export them first if any campaign has run since.
--   ALTER TABLE survey_responses DROP CONSTRAINT IF EXISTS ck_survey_responses_fill_seconds;
--   ALTER TABLE survey_responses DROP COLUMN IF EXISTS fill_seconds;
-- =============================================================================

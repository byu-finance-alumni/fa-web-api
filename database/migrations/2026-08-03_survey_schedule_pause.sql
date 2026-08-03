-- =============================================================================
-- Migration: survey campaign PAUSE / RESUME
-- Date: 2026-08-03
-- -----------------------------------------------------------------------------
-- Adds a reversible stop to `survey_schedule`. Until now the only way to halt a
-- running campaign was `cancelled`, which is terminal — a cancelled campaign
-- never resumes and has to be re-scheduled by hand. `paused` is the stop you
-- actually want when sending just needs to stop for a few days.
--
-- Three changes:
--
--   1. `ck_survey_schedule_status` gains 'paused'. The daily cron only picks up
--      'scheduled'/'active' (see `_load_schedules_due`), so a paused campaign
--      stops emailing the moment the status flips — no scheduler change needed.
--
--   2. `paused_at` — when the pause happened. This is NOT just an audit stamp:
--      the campaign's send STAGE is derived from `today - start_date`, so
--      without it a pause would silently eat the rest of the campaign (pause 3
--      weeks, resume, and `elapsed` has drifted past the 2-week reminder window
--      → the campaign marks itself `completed` and the reminders never go out).
--      On resume the service pushes `start_date` forward by the paused duration
--      so `elapsed` continues from where it stopped.
--
--   3. `paused_from_status` — the status the campaign was in when it was paused,
--      so resume restores it exactly instead of guessing. It cannot be derived
--      reliably: `last_run_at` (and `survey_send_log`) survive a re-schedule
--      (`_upsert_schedule` resets status to 'scheduled' but leaves both), so a
--      re-scheduled year would resume as 'active' when it had never started.
--      Constrained to the two runnable statuses — those are the only ones that
--      can be paused.
--
-- Both columns are NULL whenever the campaign is not paused; resume clears them.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS + DROP CONSTRAINT IF EXISTS before
-- each ADD CONSTRAINT.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

ALTER TABLE survey_schedule
    ADD COLUMN IF NOT EXISTS paused_at          timestamptz,
    ADD COLUMN IF NOT EXISTS paused_from_status varchar(20);

-- Extend the status CHECK to allow 'paused'. Postgres has no "ALTER CONSTRAINT"
-- for a CHECK, so it is dropped and re-added; both statements are in the same
-- transaction, so the table is never briefly unconstrained to another session.
ALTER TABLE survey_schedule
    DROP CONSTRAINT IF EXISTS ck_survey_schedule_status;
ALTER TABLE survey_schedule
    ADD CONSTRAINT ck_survey_schedule_status
        CHECK (status IN ('scheduled', 'active', 'paused', 'completed', 'cancelled'));

-- Only a runnable campaign can be paused, so the remembered status can only ever
-- be one of those two (or NULL when the campaign is not paused).
ALTER TABLE survey_schedule
    DROP CONSTRAINT IF EXISTS ck_survey_schedule_paused_from_status;
ALTER TABLE survey_schedule
    ADD CONSTRAINT ck_survey_schedule_paused_from_status
        CHECK (paused_from_status IS NULL
               OR paused_from_status IN ('scheduled', 'active'));

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand). Any currently-paused campaign must be moved off
-- 'paused' FIRST or the restored CHECK will fail:
--   UPDATE survey_schedule
--      SET status = COALESCE(paused_from_status, 'scheduled'),
--          paused_at = NULL, paused_from_status = NULL
--    WHERE status = 'paused';
--   ALTER TABLE survey_schedule DROP CONSTRAINT IF EXISTS ck_survey_schedule_paused_from_status;
--   ALTER TABLE survey_schedule DROP CONSTRAINT IF EXISTS ck_survey_schedule_status;
--   ALTER TABLE survey_schedule ADD CONSTRAINT ck_survey_schedule_status
--       CHECK (status IN ('scheduled', 'active', 'completed', 'cancelled'));
--   ALTER TABLE survey_schedule DROP COLUMN IF EXISTS paused_from_status;
--   ALTER TABLE survey_schedule DROP COLUMN IF EXISTS paused_at;
-- =============================================================================

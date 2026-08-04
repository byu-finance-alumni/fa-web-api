-- =============================================================================
-- Migration: survey campaign CYCLE key (#357)
-- Date: 2026-08-03
-- -----------------------------------------------------------------------------
-- `survey_send_log` was UNIQUE (graduation_year, alumni_id, stage) with no
-- notion of WHICH campaign a row belonged to, and nothing ever deletes from it.
-- So "has this alum had stage 0?" was a question about ALL TIME. Re-surveying a
-- graduation year therefore selected zero targets at every stage while the
-- campaign still reported 'active' then 'completed', with the console showing
-- the PREVIOUS cycle's counters as though they were this one's. It failed
-- silently, and annual re-surveying is the product's core loop.
--
-- `cycle_seq` is the campaign's identity: 1 for a year's first campaign,
-- incremented each time a NEW cycle is started for that year.
--
-- Why a counter and not a date or the schedule id:
--
--   * `survey_schedule.graduation_year` is UNIQUE and `_upsert_schedule` UPDATES
--     the existing row, so `survey_schedule_id` is IDENTICAL across cycles — it
--     cannot distinguish them.
--   * A date-derived cycle (`start_date`/`sent_at` year) FLIPS MID-CAMPAIGN: a
--     campaign starting in late December sends its reminders in January, and the
--     January run would see a new cycle, find nobody logged for stage 0, and
--     re-send the initial email to the whole cohort. Resume shifts `start_date`
--     forward by the paused duration, so it can cross a year boundary too.
--
-- An opaque counter cannot drift under either.
--
-- BACKFILL: every existing row in both tables becomes cycle 1 — they are all the
-- first (and so far only) campaign for their year. NOT NULL DEFAULT 1 on ADD
-- COLUMN does this in one pass, so there is no separate UPDATE to get wrong.
--
-- The unique constraint is REPLACED, not added alongside: keeping the old
-- 3-column one would defeat the whole change (it would still forbid a second
-- cycle's row for the same alum + stage).
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS + DROP CONSTRAINT IF EXISTS before
-- ADD CONSTRAINT.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

-- The campaign counter. A year's first campaign is cycle 1; starting a new cycle
-- for that year increments it. Never decreases, never reused.
ALTER TABLE survey_schedule
    ADD COLUMN IF NOT EXISTS cycle_seq integer NOT NULL DEFAULT 1;

ALTER TABLE survey_schedule
    DROP CONSTRAINT IF EXISTS ck_survey_schedule_cycle_seq;
ALTER TABLE survey_schedule
    ADD CONSTRAINT ck_survey_schedule_cycle_seq CHECK (cycle_seq >= 1);

-- Which campaign each delivered email belonged to. Existing rows are all cycle 1
-- (the only campaign their year has had).
ALTER TABLE survey_send_log
    ADD COLUMN IF NOT EXISTS cycle_seq integer NOT NULL DEFAULT 1;

ALTER TABLE survey_send_log
    DROP CONSTRAINT IF EXISTS ck_survey_send_log_cycle_seq;
ALTER TABLE survey_send_log
    ADD CONSTRAINT ck_survey_send_log_cycle_seq CHECK (cycle_seq >= 1);

-- Replace the double-send guard with the cycle-scoped one. Same protection
-- WITHIN a campaign (an alum still cannot be sent the same stage twice), but a
-- later cycle is now a distinct row rather than a conflict.
ALTER TABLE survey_send_log
    DROP CONSTRAINT IF EXISTS uq_survey_send_log_year_alumni_stage;
ALTER TABLE survey_send_log
    ADD CONSTRAINT uq_survey_send_log_year_alumni_stage
        UNIQUE (graduation_year, alumni_id, stage, cycle_seq);

-- The scheduler and console both read the log scoped to (year, cycle), which the
-- unique index above does not serve well on its own (alumni_id sits second).
CREATE INDEX IF NOT EXISTS ix_survey_send_log_year_cycle_stage
    ON survey_send_log (graduation_year, cycle_seq, stage);

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand). DESTRUCTIVE: if any year has reached cycle 2+, its
-- later-cycle rows collide with the restored 3-column constraint and MUST be
-- removed first — that permanently loses the record of those sends, so export
-- them before running this.
--   DELETE FROM survey_send_log WHERE cycle_seq > 1;
--   DROP INDEX IF EXISTS ix_survey_send_log_year_cycle_stage;
--   ALTER TABLE survey_send_log DROP CONSTRAINT IF EXISTS uq_survey_send_log_year_alumni_stage;
--   ALTER TABLE survey_send_log ADD CONSTRAINT uq_survey_send_log_year_alumni_stage
--       UNIQUE (graduation_year, alumni_id, stage);
--   ALTER TABLE survey_send_log DROP CONSTRAINT IF EXISTS ck_survey_send_log_cycle_seq;
--   ALTER TABLE survey_send_log DROP COLUMN IF EXISTS cycle_seq;
--   ALTER TABLE survey_schedule DROP CONSTRAINT IF EXISTS ck_survey_schedule_cycle_seq;
--   ALTER TABLE survey_schedule DROP COLUMN IF EXISTS cycle_seq;
-- =============================================================================

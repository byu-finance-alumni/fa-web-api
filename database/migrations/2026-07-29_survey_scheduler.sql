-- =============================================================================
-- Migration: survey scheduler (auto-send the annual "confirm your info" survey)
-- Date: 2026-07-29  (issue #542)
-- -----------------------------------------------------------------------------
-- Two tables drive the scheduled send:
--
--   survey_schedule  — one row per graduation year: the initial send date and
--                      the campaign's state. A daily Vercel cron scans these for
--                      due campaigns and sends the current stage.
--
--   survey_send_log  — append-only record of every (year, alumni, stage) email
--                      actually delivered. Its UNIQUE (graduation_year,
--                      alumni_id, stage) is the guardrail that prevents
--                      double-emailing across cron runs: the scheduler inserts a
--                      row per recipient right after each successful Resend batch
--                      and skips anyone already logged on later runs, so a crash
--                      or rate-limit mid-run never re-emails the delivered ones.
--
-- SECURITY: both NEW tables get `ENABLE ROW LEVEL SECURITY` with NO policies —
-- the deny-all lockdown (mirrors #51 and survey_responses). The app connects as
-- the table owner and bypasses RLS; the Supabase anon/authenticated API roles
-- are denied.
--
-- SAFE TO RE-RUN: IF NOT EXISTS on tables + indexes.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS survey_schedule (
    survey_schedule_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    graduation_year     int NOT NULL UNIQUE,
    start_date          date NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'scheduled',
    created_by_user_id  bigint,
    last_run_at         timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_survey_schedule_created_by FOREIGN KEY (created_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_schedule_status
        CHECK (status IN ('scheduled', 'active', 'completed', 'cancelled'))
);

CREATE TABLE IF NOT EXISTS survey_send_log (
    survey_send_log_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    graduation_year     int NOT NULL,
    alumni_id           bigint NOT NULL,
    stage               smallint NOT NULL,
    sent_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_survey_send_log_alumni FOREIGN KEY (alumni_id)
        REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_survey_send_log_year_alumni_stage
        UNIQUE (graduation_year, alumni_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_survey_send_log_year_stage
    ON survey_send_log (graduation_year, stage);

ALTER TABLE survey_schedule ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_send_log ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if the tables must be dropped):
--   DROP TABLE IF EXISTS survey_send_log;
--   DROP TABLE IF EXISTS survey_schedule;
-- =============================================================================

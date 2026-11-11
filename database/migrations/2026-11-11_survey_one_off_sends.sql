-- =============================================================================
-- Migration: one-off survey sends (api#669)
-- Date: 2026-11-11
-- -----------------------------------------------------------------------------
-- WHY. Staff can now send the survey to ONE alum from their profile
-- (POST /survey/alumni/{id}/one-off). Those emails are logged HERE and never in
-- `survey_send_log`:
--
--   * `survey_send_log` is the CLASS campaign's double-send guard, keyed by
--     (year, alumni, stage, cycle, reset). A one-off row there would read as "the
--     class's stage 0 already went to this person", and the later class send
--     would silently skip them. Jake decided it must not (2026-11-11).
--   * The one-off still counts toward the daily/monthly Resend budget:
--     `survey_email.get_send_usage` adds one row of `survey_one_off_send_log`
--     per email, the same way it adds the digest and new-people invite ledgers.
--
-- `survey_one_off`           one row per "Send survey" press (day 0).
-- `survey_one_off_send_log`  one row per email sent for it: stage 0 = the send,
--                            1 = day-7 reminder, 2 = day-14 reminder. UNIQUE
--                            (survey_one_off_id, stage) is the claim guard that
--                            stops the daily cron sending a reminder twice.
--
-- SECURITY: deny-all RLS (no policies), registered in database/rls_lockdown.sql
-- like every other table. The app connects as the table owner.
--
-- PURELY ADDITIVE: two new tables, nothing existing touched, nothing backfilled.
-- Every read of these tables in the app is wrapped (SAVEPOINT + fall back), so
-- code that reaches an un-migrated schema still sends class surveys as before.
--
-- SAFE TO RE-RUN: CREATE ... IF NOT EXISTS; constraints are inline; the RLS
-- enable is idempotent.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS survey_one_off (
    survey_one_off_id  bigserial PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    graduation_year    integer NOT NULL,
    sent_by_user_id    bigint,
    started_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_survey_one_off_alumni
        FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_survey_one_off_sent_by
        FOREIGN KEY (sent_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- "Last sent" for one alum, and "latest one-off per alum" for the reminder run.
CREATE INDEX IF NOT EXISTS idx_survey_one_off_alumni_started
    ON survey_one_off (alumni_id, started_at DESC);
-- The reminder run only looks at the last few weeks.
CREATE INDEX IF NOT EXISTS idx_survey_one_off_started
    ON survey_one_off (started_at);

CREATE TABLE IF NOT EXISTS survey_one_off_send_log (
    survey_one_off_send_log_id  bigserial PRIMARY KEY,
    survey_one_off_id           bigint NOT NULL,
    stage                       smallint NOT NULL,
    sent_at                     timestamptz NOT NULL DEFAULT now(),
    resend_email_id             varchar(100),
    sent_to                     varchar(320),
    CONSTRAINT fk_survey_one_off_send_log_one_off
        FOREIGN KEY (survey_one_off_id)
        REFERENCES survey_one_off (survey_one_off_id) ON DELETE CASCADE,
    CONSTRAINT ck_survey_one_off_send_log_stage CHECK (stage IN (0, 1, 2)),
    CONSTRAINT uq_survey_one_off_send_log_stage UNIQUE (survey_one_off_id, stage)
);

-- The shared usage count (today / this month).
CREATE INDEX IF NOT EXISTS idx_survey_one_off_send_log_sent_at
    ON survey_one_off_send_log (sent_at);

ALTER TABLE survey_one_off ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_one_off_send_log ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS survey_one_off_send_log;
--   DROP TABLE IF EXISTS survey_one_off;
-- One-off emails already sent then stop counting toward the Resend budget for
-- the rest of their day/month, and any reminders still owed are not sent.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public'
--    AND tablename IN ('survey_one_off', 'survey_one_off_send_log');
-- SELECT count(*) AS one_offs, max(started_at) AS last_sent FROM survey_one_off;

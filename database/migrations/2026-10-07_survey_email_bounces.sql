-- =============================================================================
-- Migration: record which survey emails bounced (fa-web-app #858)
-- Date: 2026-10-07
-- -----------------------------------------------------------------------------
-- WHY. Staff want to see which alumni's survey email bounced so they can fix the
-- address. Resend knows; we did not. Two halves:
--
-- 1. survey_send_log gains the Resend message id and the address it went to.
--    `_send_batch` used to discard the ids Resend returns, so a bounce report
--    (which names only the message id) could not be tied back to an alum. Both
--    columns are NULLABLE and written best-effort right after a successful batch;
--    a send never fails because this bookkeeping failed. Rows that predate this
--    migration stay NULL forever -- the ids were never kept, so there is NOTHING
--    TO BACKFILL (owner decision: no backfill).
--
--    This is the first time a send-log row is ever UPDATED after its claim. The
--    update touches only these two new columns; the claim/release/unique-key
--    behaviour that makes the table the double-send guard is unchanged.
--
-- 2. survey_email_events: one row per Resend webhook delivery we accept
--    (`email.bounced`, `email.complained`). `svix_id` is UNIQUE so a redelivered
--    webhook is a no-op (Svix retries until it sees a 2xx). `alumni_id` is
--    resolved at receipt -- via resend_email_id -> survey_send_log, falling back
--    to the alumni_id tag carried on the email -- and is NULL when neither
--    matches. ON DELETE SET NULL so deleting an alum never fails on this table.
--    NO EMAIL ADDRESS AND NO RAW PAYLOAD IS STORED HERE; the address shown to
--    staff comes from survey_send_log.sent_to.
--
--    Temporary/soft bounces are stored (bounce_type 'transient'/'undetermined')
--    but the console lists PERMANENT bounces only (owner decision). Nothing here
--    marks an alum unreachable or changes alumni data -- it is a list, by design.
--
-- SECURITY: new table -> `ENABLE ROW LEVEL SECURITY` with NO policies, the
-- deny-all lockdown every table in this schema gets (mirrors #51), and it is
-- registered in database/rls_lockdown.sql. The app connects as the table owner
-- and bypasses RLS; anon/authenticated are denied.
--
-- PURELY ADDITIVE / BACKWARD-COMPATIBLE:
--   * OLD code + NEW schema: fine -- nullable columns nothing old writes, and a
--     table nothing old reads.
--   * NEW code + OLD schema: the id write-back would fail, but it is wrapped and
--     non-fatal (the send still succeeds); the webhook and the bounced list
--     WOULD error. So ship THIS migration to prod on its own, BEFORE the code,
--     as with every schema-dependent change here.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS / CREATE ... IF NOT EXISTS; the RLS
-- enable is idempotent.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

-- Resend's id for the email this row recorded, and the address it went to.
-- NULL = sent before this change, or the best-effort write-back failed.
ALTER TABLE survey_send_log
    ADD COLUMN IF NOT EXISTS resend_email_id varchar(100);
ALTER TABLE survey_send_log
    ADD COLUMN IF NOT EXISTS sent_to varchar(320);

-- The webhook resolves every bounce through this lookup.
CREATE INDEX IF NOT EXISTS ix_survey_send_log_resend_email_id
    ON survey_send_log (resend_email_id);

CREATE TABLE IF NOT EXISTS survey_email_events (
    survey_email_event_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    resend_email_id        varchar(100),
    alumni_id              bigint,
    graduation_year        int,
    -- 'email.bounced' | 'email.complained'
    event_type             varchar(40) NOT NULL,
    -- Lowercased Resend bounce.type: 'permanent' | 'transient' | 'undetermined'.
    -- NULL for a complaint.
    bounce_type            varchar(40),
    -- Resend bounce.subType as sent (e.g. 'General', 'NoEmail', 'Suppressed').
    bounce_subtype         varchar(60),
    -- When Resend says it happened (payload created_at); receipt time if absent.
    occurred_at            timestamptz NOT NULL DEFAULT now(),
    -- The webhook delivery id. UNIQUE = idempotent on redelivery.
    svix_id                varchar(100) NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_survey_email_events_alumni FOREIGN KEY (alumni_id)
        REFERENCES alumni (alumni_id) ON DELETE SET NULL,
    CONSTRAINT uq_survey_email_events_svix_id UNIQUE (svix_id)
);

-- The console's bounced list: one year, permanent bounces, newest first.
CREATE INDEX IF NOT EXISTS ix_survey_email_events_year_type
    ON survey_email_events (graduation_year, event_type, bounce_type);
CREATE INDEX IF NOT EXISTS ix_survey_email_events_resend_email_id
    ON survey_email_events (resend_email_id);

ALTER TABLE survey_email_events ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone). Discards the bounce history and
-- the stored message ids; neither can be recovered afterwards.
--   DROP TABLE IF EXISTS survey_email_events;
--   DROP INDEX IF EXISTS ix_survey_send_log_resend_email_id;
--   ALTER TABLE survey_send_log DROP COLUMN IF EXISTS sent_to;
--   ALTER TABLE survey_send_log DROP COLUMN IF EXISTS resend_email_id;
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public' AND tablename = 'survey_email_events';
-- SELECT column_name FROM information_schema.columns
--  WHERE table_name = 'survey_send_log'
--    AND column_name IN ('resend_email_id', 'sent_to');

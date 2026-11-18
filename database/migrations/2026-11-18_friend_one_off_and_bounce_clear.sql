-- =============================================================================
-- Migration: friends can be sent the survey; bounces clear on a good send
-- Date: 2026-11-18   (fa-web-api #696, #697)
-- -----------------------------------------------------------------------------
-- WHY.
--
-- 1. #696 -- "Send survey" now works on a FRIEND's profile. Most friends have no
--    graduation year, and `survey_one_off.graduation_year` was NOT NULL, so their
--    one-off row could not be written. The column becomes NULLABLE. Nothing else
--    about the table changes; an alum's one-off still records their year.
--
-- 2. #697 -- the console's Bounced list now drops someone once the address that
--    bounced is no longer on their profile, or once a later survey email to their
--    CURRENT address was delivered (Resend's `email.delivered` webhook, now
--    stored for survey emails). One-off sends are matched to a webhook by their
--    Resend message id, so that id is indexed here, like the class send log's.
--    The delivered lookup is one alum at a time, so it gets an index too.
--
-- NO DATA FIX IS NEEDED, ON PURPOSE. "Bounced" is not a stored flag anywhere: the
-- list is DERIVED on every read from `survey_email_events` + the send logs + the
-- profile's current addresses. The people from the 2026-11-18 meeting (address
-- fixed, survey re-sent) drop off the list as soon as this code is live, because
-- the address that bounced is no longer on their profile. No row is updated or
-- deleted by this file.
--
-- PURELY ADDITIVE / BACKWARD-COMPATIBLE:
--   * OLD code + NEW schema: fine -- old code always writes a year; it never
--     reads the new indexes.
--   * NEW code + OLD schema: an alum's one-off and every bounce still work; only
--     a friend with no year would fail to send (500, nothing emailed -- the row
--     is claimed before Resend is called). Ship THIS migration to prod first, as
--     with every schema-dependent change here.
--
-- SAFE TO RE-RUN: DROP NOT NULL on an already-nullable column is a no-op;
-- CREATE INDEX IF NOT EXISTS.
--
-- NOT RUN by the agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

ALTER TABLE survey_one_off
    ALTER COLUMN graduation_year DROP NOT NULL;

-- The webhook ties a bounce / delivery of a one-off email back to its row.
CREATE INDEX IF NOT EXISTS idx_survey_one_off_send_log_resend_email_id
    ON survey_one_off_send_log (resend_email_id);

-- "Was a later email to them delivered?" -- asked per alum by the Bounced list.
CREATE INDEX IF NOT EXISTS idx_survey_email_events_alumni_type
    ON survey_email_events (alumni_id, event_type);

COMMIT;

-- =============================================================================
-- ROLLBACK (by hand). The NOT NULL can only come back once no NULL is left:
--   DROP INDEX IF EXISTS idx_survey_email_events_alumni_type;
--   DROP INDEX IF EXISTS idx_survey_one_off_send_log_resend_email_id;
--   -- Only if no friend one-off exists (SELECT count(*) FROM survey_one_off
--   -- WHERE graduation_year IS NULL) = 0:
--   ALTER TABLE survey_one_off ALTER COLUMN graduation_year SET NOT NULL;
-- =============================================================================

-- =============================================================================
-- VERIFY:
--   SELECT is_nullable FROM information_schema.columns
--    WHERE table_name = 'survey_one_off' AND column_name = 'graduation_year';
--   -- expect YES
--   SELECT indexname FROM pg_indexes
--    WHERE indexname IN ('idx_survey_one_off_send_log_resend_email_id',
--                        'idx_survey_email_events_alumni_type');
-- =============================================================================

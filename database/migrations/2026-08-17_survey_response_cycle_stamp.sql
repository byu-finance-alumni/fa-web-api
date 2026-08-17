-- =============================================================================
-- Migration: stamp the campaign cycle onto `survey_responses` (#497)
-- Date: 2026-08-17
-- -----------------------------------------------------------------------------
-- A survey response records WHAT an alum said and WHEN, but not WHICH CAMPAIGN
-- asked. Every console count joins `survey_schedule.cycle_seq` to
-- `survey_send_log.cycle_seq`, which scopes it to the year's CURRENT cycle, so
-- the moment a graduation year starts its second campaign the first one's
-- numbers stop being reportable -- and they cannot be reconstructed afterwards,
-- because nothing in the row says which cycle it belonged to. `submitted_at`
-- looks like it would answer that, but it CANNOT: see the "never from a date"
-- note below.
--
-- This migration is CAPTURE ONLY. It adds two nullable columns that
-- `survey_responses.submit_response` fills in at submit time. No existing query,
-- count or console panel reads them yet; the reporting path is a separate piece
-- of work. Stamping now is what makes that later work possible at all -- history
-- only starts accumulating once the column exists.
--
-- WHY NULLABLE, AND WHY NO BACKFILL. Unlike #357 -- where every existing
-- `survey_send_log` row provably belonged to its year's one and only campaign,
-- so `NOT NULL DEFAULT 1` was a statement of fact -- a response's cycle is NOT
-- knowable after the fact:
--
--   * The response carries no link to the email that prompted it. The send log
--     can be consulted, but a year whose campaign was deleted (#398) keeps only
--     a `survey_campaign_retirement` row, and an alum reset (#395) can leave two
--     plausible sends.
--   * Defaulting every historical row to cycle 1 would assert something we did
--     not observe, and the whole point of the column is to be trustworthy in a
--     report. A NULL reads as "we do not know" -- which is the truth for every
--     row that predates this change, and is easy to exclude from a query. A
--     wrong number is not, because nothing downstream can tell it from a right
--     one.
--
-- So: NULL for history, a real value from here on. `stage` follows the same
-- rule for the same reason.
--
-- NEVER DERIVED FROM A DATE (#357, and repeated here because the temptation is
-- strongest exactly at this table). `cycle_seq` is an OPAQUE COUNTER. A campaign
-- starting in late December sends its 1- and 2-week reminders in January, and
-- resume shifts `start_date` forward by the paused duration, so any cycle
-- inferred from `submitted_at`'s calendar year flips MID-CAMPAIGN and splits one
-- campaign's responses across two "cycles". The value written here is read from
-- `survey_send_log` -- the row recording the email this alum was actually sent
-- -- and is never computed.
--
-- SAFE ON EXISTING ROWS: two `ADD COLUMN IF NOT EXISTS` of nullable columns.
-- Postgres 11+ adds a nullable column without a table rewrite, so this is a
-- catalog-only change even on a large table, and no row is modified. Re-runnable
-- (`IF NOT EXISTS` throughout, `DROP CONSTRAINT IF EXISTS` before each ADD).
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

-- Which campaign this response answers -- the `cycle_seq` of the survey email
-- the alum was actually sent, copied from `survey_send_log` at submit time.
-- NULL = unknown (every row predating this migration, plus any submission that
-- cannot be matched to a logged send).
ALTER TABLE survey_responses
    ADD COLUMN IF NOT EXISTS cycle_seq integer;

ALTER TABLE survey_responses
    DROP CONSTRAINT IF EXISTS ck_survey_responses_cycle_seq;
ALTER TABLE survey_responses
    ADD CONSTRAINT ck_survey_responses_cycle_seq
        CHECK (cycle_seq IS NULL OR cycle_seq >= 1);

-- Which email in that campaign the alum had most recently been sent when they
-- replied: 0 = initial, 1 = 1-week reminder, 2 = 2-week reminder. Mirrors
-- `survey_send_log.stage`. It costs nothing -- it comes off the same row as
-- `cycle_seq` -- and it is the missing half of "did the reminders earn their
-- keep?". NULL under exactly the same conditions as `cycle_seq`.
ALTER TABLE survey_responses
    ADD COLUMN IF NOT EXISTS stage smallint;

ALTER TABLE survey_responses
    DROP CONSTRAINT IF EXISTS ck_survey_responses_stage;
ALTER TABLE survey_responses
    ADD CONSTRAINT ck_survey_responses_stage
        CHECK (stage IS NULL OR stage BETWEEN 0 AND 2);

-- The shape every future per-cycle report will read: "this year's cycle N".
-- The existing `idx_survey_responses_status_year` leads with `status`, so it
-- does not serve a cycle-scoped scan. Cheap to add now, and adding it with the
-- columns keeps the index and the thing it indexes in one migration.
CREATE INDEX IF NOT EXISTS ix_survey_responses_year_cycle
    ON survey_responses (graduation_year, cycle_seq);

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand). Non-destructive to anything that existed before this
-- migration -- it only discards stamps written after it, which stop being
-- recoverable once dropped, so export them first if any campaign has run since.
--   DROP INDEX IF EXISTS ix_survey_responses_year_cycle;
--   ALTER TABLE survey_responses DROP CONSTRAINT IF EXISTS ck_survey_responses_stage;
--   ALTER TABLE survey_responses DROP COLUMN IF EXISTS stage;
--   ALTER TABLE survey_responses DROP CONSTRAINT IF EXISTS ck_survey_responses_cycle_seq;
--   ALTER TABLE survey_responses DROP COLUMN IF EXISTS cycle_seq;
-- =============================================================================

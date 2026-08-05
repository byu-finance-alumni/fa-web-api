-- Per-alumnus survey reset WITHOUT DELETING ANYTHING (#395, revised 2026-08-05).
--
-- The shipped reset deleted the alum's `survey_responses` rows, their
-- `survey_send_log` rows and their staged survey photos. That threw away real
-- submitted answers -- including ones nobody had reviewed -- to solve what is
-- only an eligibility problem. Jake, 2026-08-05: "when you reset the campaign
-- the responses should not be reset, they should still be in the db."
--
-- So a reset becomes an EVENT that is recorded, not rows that are removed.
-- `survey_reset_log` holds one row per reset, and every query that asks "has
-- this person already replied / already been emailed?" now ignores anything
-- that predates their latest reset. The answers stay in the database, stay
-- reviewable, and keep rendering in the profile's Surveys tab.
--
-- Two things had to change, because the block has two independent causes:
--
-- 1. survey_responses -- a `pending`/`applied` row inside 365 days. Nothing is
--    added to this table AT ALL: supersession is decided by comparing
--    `submitted_at` against the alum's latest `reset_at`, so the response rows
--    are untouched, which is exactly what was asked for.
--
-- 2. survey_send_log -- UNIQUE (graduation_year, alumni_id, stage, cycle_seq).
--    A timestamp comparison is NOT enough here: the constraint physically
--    refuses the new row, so `_claim_batch`'s ON CONFLICT DO NOTHING would
--    silently drop the recipient and they would never be emailed however
--    "eligible" the console called them. The unique key therefore learns
--    `reset_seq`: sends made after the alum's Nth reset carry N, so the new row
--    no longer collides with the old one and the old one is never touched.
--    `reset_seq` is written once at insert and never updated; the log stays
--    append-only.
--
-- EXISTING ROWS KEEP THEIR CURRENT BEHAVIOUR. Every send-log row backfills to
-- reset_seq = 0 ("no reset had happened when this went out"), which with an
-- empty `survey_reset_log` makes the new unique key identical in effect to the
-- old one, and makes every "not superseded" predicate below true for everyone.
-- Nothing already in the database becomes superseded or unblocked.

BEGIN;

-- One row per reset. `reset_seq` is per-alumnus and starts at 1, so it doubles
-- as "how many times has this person been reset". The counts are what the
-- operator was told the reset would do, kept alongside the event so the audit
-- trail can be read without re-deriving it.
CREATE TABLE IF NOT EXISTS survey_reset_log (
    survey_reset_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id            bigint NOT NULL,
    reset_seq            int NOT NULL,
    reset_at             timestamptz NOT NULL DEFAULT now(),
    reset_by_user_id     bigint,
    -- How many rows this reset MOVED OUT OF THE WAY (not removed) -- send-log
    -- rows that were still blocking, and responses submitted before it.
    sends_superseded     int NOT NULL DEFAULT 0,
    responses_superseded int NOT NULL DEFAULT 0,
    CONSTRAINT fk_survey_reset_log_alumni FOREIGN KEY (alumni_id)
        REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_survey_reset_log_user FOREIGN KEY (reset_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_reset_log_seq CHECK (reset_seq >= 1),
    -- Two concurrent resets of the same alumnus cannot both claim a sequence
    -- number; the loser errors instead of quietly producing a duplicate that
    -- would make the send log's unique key ambiguous.
    CONSTRAINT uq_survey_reset_log_alumni_seq UNIQUE (alumni_id, reset_seq)
);

-- The shape every exclusion query reads: "the latest reset for this alumnus".
CREATE INDEX IF NOT EXISTS ix_survey_reset_log_alumni_at
    ON survey_reset_log (alumni_id, reset_at DESC);

-- New table => deny-all RLS, matching rls_lockdown.sql (no policies = deny all).
ALTER TABLE survey_reset_log ENABLE ROW LEVEL SECURITY;

-- Which reset generation a delivered email belongs to. 0 = sent before this
-- alumnus was ever reset, which is every row that exists today.
ALTER TABLE survey_send_log
    ADD COLUMN IF NOT EXISTS reset_seq int NOT NULL DEFAULT 0;

-- DROP-then-ADD rather than a bare ADD, so re-running the file lands in the
-- same state instead of erroring on an existing constraint.
ALTER TABLE survey_send_log
    DROP CONSTRAINT IF EXISTS ck_survey_send_log_reset_seq;
ALTER TABLE survey_send_log
    ADD CONSTRAINT ck_survey_send_log_reset_seq CHECK (reset_seq >= 0);

-- Re-key the double-send guard to include it. The NAME is unchanged on purpose:
-- `survey_email._claim_batch` names this constraint in its ON CONFLICT clause,
-- and renaming it would make every claim raise instead of dedupe.
ALTER TABLE survey_send_log
    DROP CONSTRAINT IF EXISTS uq_survey_send_log_year_alumni_stage;
ALTER TABLE survey_send_log
    ADD CONSTRAINT uq_survey_send_log_year_alumni_stage
    UNIQUE (graduation_year, alumni_id, stage, cycle_seq, reset_seq);

COMMIT;

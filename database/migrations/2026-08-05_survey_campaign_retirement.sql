-- Delete ANY survey campaign, whatever its status, without losing the emails
-- (#398, revised 2026-08-05).
--
-- What shipped this morning could only delete a campaign that had never emailed
-- anyone; everything else was offered "cancel", and an already-cancelled
-- campaign got no control at all. Jake: "it still won't let me delete a campaign
-- in the engineer dashboard" -- his campaigns have all either sent or are
-- already cancelled. Asked, he chose: delete any campaign, keep the emails.
--
-- WHY THE DELETE WAS REFUSED, AND WHAT THIS TABLE FIXES
-- ----------------------------------------------------
-- `survey_schedule` is the SOLE holder of a graduation year's `cycle_seq`, and
-- `survey_send_log` rows are scoped by it. Drop the schedule row and the year
-- has no cycle any more, so it reads as cycle 1 again -- the existing log rows
-- become the CURRENT cycle's, the next campaign for that year finds everyone
-- already emailed, `select_stage_targets` returns nobody at every stage, and the
-- campaign "completes" having sent zero emails. That is #357 verbatim, a bug
-- this codebase has already paid for once, and it fails SILENTLY.
--
-- So the cycle number is the one thing that must outlive the schedule row. This
-- table is where it goes: one row per deleted campaign, recording the cycle it
-- was on. Nothing else about the campaign matters to the send log.
--
-- The shape is deliberately the same as `survey_reset_log` (#395, the same day):
-- an append-only EVENT that supersedes, rather than a rewrite of the rows it
-- supersedes. A reset retires one alumnus's sends by `reset_seq`; this retires
-- one campaign's sends by `cycle_seq`. Neither deletes or updates a single
-- `survey_send_log` or `survey_responses` row -- they are read as history and
-- are simply no longer current.
--
-- HOW A NEW CAMPAIGN FOR THAT YEAR BEHAVES AFTERWARDS
-- --------------------------------------------------
-- `survey_email.current_cycle_seq` resolves a year with no schedule row to
-- `max(retired cycle_seq) + 1` instead of 1, and a newly created schedule row
-- starts there. So the fresh campaign is a cycle ABOVE every retired row:
--
--   * the double-send guard (`logged_alumni_ids`, cycle-scoped) sees none of
--     them, so the alumni the deleted campaign emailed are eligible again;
--   * the claim's UNIQUE (graduation_year, alumni_id, stage, cycle_seq,
--     reset_seq) cannot collide with them, so `_claim_batch`'s ON CONFLICT DO
--     NOTHING has nothing to swallow and the recipients are really claimed.
--
-- Alumni who ANSWERED are still held out by the 365-day annual re-survey window,
-- exactly as they are after `start_new_cycle`. Deleting a campaign is not a way
-- to re-ask someone who already replied; the per-alumnus reset is.
--
-- ENTIRELY ADDITIVE. No existing table is touched and no row changes meaning: an
-- empty `survey_campaign_retirement` leaves `current_cycle_seq` returning
-- exactly what it returns today for every year.

BEGIN;

-- One row per campaign that has been deleted. `cycle_seq` is the campaign's own
-- cycle at the moment it was removed -- the value `survey_schedule` would have
-- carried on -- so the next campaign for the year can start above it.
CREATE TABLE IF NOT EXISTS survey_campaign_retirement (
    survey_campaign_retirement_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    graduation_year     int NOT NULL,
    cycle_seq           int NOT NULL,
    retired_at          timestamptz NOT NULL DEFAULT now(),
    retired_by_user_id  bigint,
    -- The campaign as it was, kept because the schedule row that held it is
    -- gone: without these two, "a campaign for 2019 was deleted" is all that
    -- survives and nobody can say which one.
    previous_status     varchar(20),
    start_date          date,
    -- What this retirement MOVED OUT OF THE WAY (not what it removed): send-log
    -- rows belonging to the retired cycle, and the year's survey responses,
    -- every one of which is still in the database. Stored beside the event so
    -- "what did that button do?" is answerable later without re-deriving it from
    -- tables that have since moved on.
    sends_retired       int NOT NULL DEFAULT 0,
    responses_kept      int NOT NULL DEFAULT 0,
    CONSTRAINT fk_survey_campaign_retirement_user FOREIGN KEY (retired_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_campaign_retirement_cycle CHECK (cycle_seq >= 1),
    -- A (year, cycle) can only be retired once. Two concurrent deletes of the
    -- same campaign make the loser error rather than quietly write a second
    -- tombstone for a cycle that is already retired.
    CONSTRAINT uq_survey_campaign_retirement_year_cycle
        UNIQUE (graduation_year, cycle_seq)
);

-- The only read shape there is: "the highest cycle retired for this year".
CREATE INDEX IF NOT EXISTS ix_survey_campaign_retirement_year_cycle
    ON survey_campaign_retirement (graduation_year, cycle_seq DESC);

-- New table => deny-all RLS, matching rls_lockdown.sql (no policies = deny all).
ALTER TABLE survey_campaign_retirement ENABLE ROW LEVEL SECURITY;

COMMIT;

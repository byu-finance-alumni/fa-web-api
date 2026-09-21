-- =============================================================================
-- Migration: survey_email_message.reminder_note (the "in case you missed this"
--            line the REMINDER emails carry, #560)
-- Date: 2026-09-21
-- -----------------------------------------------------------------------------
-- WHY. All three survey emails were byte for byte identical. The cadence is
-- stage 0 on day 0, stage 1 on day 7, stage 2 on day 14, but the staff-editable
-- copy from #524 is a single row — one subject, one intro, one closing — and
-- render_survey_email took no stage argument at all. So a reminder arrived
-- looking exactly like the original, with nothing to say it was a second ask.
--
-- Amy (for Tanya), 2026-09-21: the 2nd and 3rd emails should open with something
-- like "In case you missed this survey, we really value your update and would
-- appreciate you filling it out."
--
-- Jake, 2026-09-21: a line ON TOP of the current message — not per-stage
-- rewrites of the whole email. Hence ONE column, not three copies of the body.
--
-- -----------------------------------------------------------------------------
-- NULL AND '' MEAN DIFFERENT THINGS HERE. READ THIS BEFORE CHANGING IT.
-- -----------------------------------------------------------------------------
-- Every other column on this table uses "blank -> fall back to the built-in
-- default". This one CANNOT, because blank is a value someone might mean:
--
--   NULL  -> never set. Use survey_message.DEFAULT_REMINDER_NOTE.
--            This is what every pre-migration row is, so a cohort whose copy was
--            already customised still gets the directors' reminder line.
--   ''    -> deliberately cleared in the console. The reminders carry NO extra
--            line and read exactly as they did before this migration. This is
--            the OFF switch, and it must survive a save.
--   text  -> that text, on stages 1 and 2 only.
--
-- Collapsing '' into NULL would make the off switch un-saveable: clearing the
-- box would put the default sentence straight back.
--
-- -----------------------------------------------------------------------------
-- STAGE 0 NEVER SEES IT
-- -----------------------------------------------------------------------------
-- The note is prepended by render_survey_email for stage >= 1 only. The initial
-- email is unchanged no matter what is stored here, so a badly worded note can
-- never make a first contact read like a chase-up.
--
-- 1-2000 chars by CHECK when not NULL — a sentence or two, not a second body;
-- the API validates the same bound and additionally rejects control and
-- invisible characters, exactly like the intro beside it.
--
-- ADDITIVE AND SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS, no backfill, no
-- existing column touched, no seed. Applying it changes nothing until the code
-- that reads the column is deployed.
--
-- RLS is already enabled on this table (deny-all, no policies) and a new column
-- inherits it. Nothing to do here.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path — and ON ITS OWN COMMIT, BEFORE the code that reads it: the
-- migrate job trails Vercel by minutes and schema-dependent code landing first
-- has caused an outage before.
-- =============================================================================

BEGIN;

ALTER TABLE survey_email_message
    ADD COLUMN IF NOT EXISTS reminder_note text;

-- Idempotent: re-running the migration must not fail on the constraint already
-- being there, and ALTER TABLE has no ADD CONSTRAINT IF NOT EXISTS.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_survey_email_message_reminder_note_len'
    ) THEN
        ALTER TABLE survey_email_message
            ADD CONSTRAINT ck_survey_email_message_reminder_note_len
            CHECK (reminder_note IS NULL OR char_length(reminder_note) <= 2000);
    END IF;
END $$;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   ALTER TABLE survey_email_message
--       DROP CONSTRAINT IF EXISTS ck_survey_email_message_reminder_note_len;
--   ALTER TABLE survey_email_message DROP COLUMN IF EXISTS reminder_note;
-- Dropping the column restores the pre-#560 email exactly. Roll the CODE back
-- first: survey_message reads this column, and get_for_send's catch-all would
-- otherwise turn every send into the built-in wording (a working email, but not
-- the customised one) until the deploy caught up.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type, is_nullable
--   FROM information_schema.columns
--  WHERE table_name = 'survey_email_message' AND column_name = 'reminder_note';
-- SELECT id, reminder_note IS NULL AS never_set, reminder_note
--   FROM survey_email_message;
-- -- The length rule, proved (this must ERROR):
-- -- UPDATE survey_email_message SET reminder_note = repeat('x', 2001) WHERE id = 1;
-- -- Both legal, and they mean different things:
-- -- UPDATE survey_email_message SET reminder_note = ''   WHERE id = 1;  -- off
-- -- UPDATE survey_email_message SET reminder_note = NULL WHERE id = 1;  -- default

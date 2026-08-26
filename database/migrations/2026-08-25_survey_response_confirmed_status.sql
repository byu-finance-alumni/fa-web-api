-- =============================================================================
-- Migration: a fourth `survey_responses.status` -- 'confirmed' (#755)
-- Date: 2026-08-25
-- -----------------------------------------------------------------------------
-- "Yes, everything is correct" recorded NOTHING. It was a client-side state
-- change on the public survey page, so the alumni who answer FASTEST -- the ones
-- with nothing to correct -- were the only ones invisible to every reply tally,
-- kept receiving both reminders, and stayed on the manual-follow-up call sheet
-- forever. Jake (2026-08-25): confirming should record a response.
--
-- WHY A NEW STATUS AND NOT ONE OF THE THREE. A confirmation is a REPLY but it is
-- not a FIELD CHANGE, and none of the existing values can say both:
--
--   * `pending`  would put an empty submission in the staff review queue and
--     inflate `awaiting_review` -- the console's ACTIONABLE number -- with rows
--     that have nothing to apply and would sit there forever.
--   * `applied`  asserts staff accepted it and it was WRITTEN TO THE RECORD.
--     Nothing was written; the console reads `applied` as "how much of what came
--     back was usable", which a confirmation would silently inflate.
--   * `rejected` is not a reply at all -- the alum would stay surveyable and keep
--     getting reminders, which is the exact bug this fixes.
--
-- So `confirmed` joins `survey_email.RESPONDED_STATUSES` (it IS a reply: it holds
-- the alum out of reminders and counts toward the response rate) while staying
-- out of the pending/applied/rejected review-outcome columns, which continue to
-- mean exactly what they meant. `_cycle_progress` gains its own `confirmed`
-- column so the replies it accounts for are visible rather than unexplained.
--
-- WIDENING ONLY, so it is BACKWARD-COMPATIBLE with the code already running.
-- The CI migrate job trails the Vercel deploy by minutes, and the direction of
-- that gap is what matters here:
--
--   * OLD code against the NEW constraint: fine. Nothing before this change ever
--     writes 'confirmed', and a widened CHECK admits everything the narrow one
--     did. No existing row is touched, read or rewritten.
--   * NEW code against the OLD constraint: a confirming alum's INSERT would be
--     refused by the CHECK for the length of that gap. Nothing else in the
--     release depends on this value.
--
-- ==> SHIP THIS MIGRATION TO prod ON ITS OWN, BEFORE the code that writes
--     'confirmed'. Same rule as the top-nav batch.
--
-- SAFE ON EXISTING ROWS: a CHECK re-created with a strictly wider predicate.
-- Postgres validates it against the table, and every existing row already
-- satisfies the narrower version, so the scan can only pass. Re-runnable
-- (`DROP CONSTRAINT IF EXISTS` before the ADD).
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

ALTER TABLE survey_responses
    DROP CONSTRAINT IF EXISTS ck_survey_responses_status;
ALTER TABLE survey_responses
    ADD CONSTRAINT ck_survey_responses_status
        CHECK (status IN ('pending', 'applied', 'rejected', 'confirmed'));

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand). NOT safe once any alum has confirmed: re-narrowing the
-- CHECK fails while a 'confirmed' row exists, and there is no correct value to
-- move those rows to -- they are replies that changed nothing, which is the
-- distinction the fourth status exists to record. Export them first, and expect
-- to decide deliberately what happens to each.
--   ALTER TABLE survey_responses DROP CONSTRAINT IF EXISTS ck_survey_responses_status;
--   ALTER TABLE survey_responses ADD CONSTRAINT ck_survey_responses_status
--       CHECK (status IN ('pending', 'applied', 'rejected'));
-- =============================================================================

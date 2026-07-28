-- =============================================================================
-- Migration: survey_responses (alum survey submissions, pending admin review)
-- Date: 2026-07-27
-- -----------------------------------------------------------------------------
-- Alumni submit their "confirm your info" updates from the public survey link.
-- Per the email's promise ("your response will be reviewed before any changes
-- are applied"), a submission is STAGED here as a pending row rather than
-- written to the record. Staff review each response in the console and apply or
-- reject it; `payload` holds the submitted values keyed by the survey field keys
-- (table.column), e.g. {"employment.current_employer": "Acme"}.
--
-- SECURITY: this NEW table gets `ENABLE ROW LEVEL SECURITY` with NO policies —
-- the deny-all lockdown (mirrors #51). The app connects as the table owner and
-- bypasses RLS; the Supabase anon/authenticated API roles are denied.
--
-- SAFE TO RE-RUN: IF NOT EXISTS on table + indexes.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS survey_responses (
    survey_response_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id           bigint NOT NULL,
    graduation_year     int,
    payload             jsonb NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'pending',
    submitted_at        timestamptz NOT NULL DEFAULT now(),
    reviewed_by_user_id bigint,
    reviewed_at         timestamptz,
    CONSTRAINT fk_survey_responses_alumni_id FOREIGN KEY (alumni_id)
        REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_survey_responses_reviewer FOREIGN KEY (reviewed_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_responses_status
        CHECK (status IN ('pending', 'applied', 'rejected'))
);

CREATE INDEX IF NOT EXISTS idx_survey_responses_status_year
    ON survey_responses (status, graduation_year);
CREATE INDEX IF NOT EXISTS idx_survey_responses_alumni_id
    ON survey_responses (alumni_id);

ALTER TABLE survey_responses ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if the table must be dropped):
--   DROP TABLE IF EXISTS survey_responses;
-- =============================================================================

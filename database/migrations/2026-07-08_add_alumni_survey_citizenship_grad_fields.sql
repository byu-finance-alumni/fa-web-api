-- =============================================================================
-- Migration: add survey / citizenship / graduation-detail fields to alumni
-- Date: 2026-07-08
-- -----------------------------------------------------------------------------
-- Adds a batch of OPTIONAL / NULLABLE additive fields to the alumni record:
--
--   citizenship             varchar(100)  -- citizenship / nationality
--   marital_status          varchar(50)   -- Married / Single / ...
--   home_country            varchar(100)  -- country of ORIGIN (distinct from
--                                            the current-address country)
--   employment_status       varchar(50)   -- person-level: Employed / Retired /
--                                            Student / Seeking / ...
--   other_designations      text          -- free-text extra designations
--                                            (e.g. "Series 7, Series 63")
--   survey_completed_date   date          -- date the alum filled the survey
--   graduation_semester     varchar(20)   -- Fall / Winter / Spring / Summer
--   graduation_class        int           -- graduating cohort/class, DISTINCT
--                                            from graduation_year
--   profile_updated_date    date          -- date of the last manual profile
--                                            update ("updated by Amy")
--   profile_updated_by_user_id bigint     -- user who made that update; FK ->
--                                            users(user_id) ON DELETE SET NULL
--
-- graduation_semester + graduation_class supersede the raw graduation_month in
-- the API read schema. The graduation_month column is intentionally LEFT IN
-- PLACE (dormant) -- this migration does NOT drop or rename it.
--
-- All columns live on the existing `alumni` table, which already has RLS enabled
-- (see rls_lockdown.sql), so no new RLS / lockdown step is needed here -- adding
-- columns inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS is idempotent; the FK + index are
-- guarded so re-running is a no-op.
-- =============================================================================

BEGIN;

ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS citizenship                varchar(100),
    ADD COLUMN IF NOT EXISTS marital_status             varchar(50),
    ADD COLUMN IF NOT EXISTS home_country               varchar(100),
    ADD COLUMN IF NOT EXISTS employment_status          varchar(50),
    ADD COLUMN IF NOT EXISTS other_designations         text,
    ADD COLUMN IF NOT EXISTS survey_completed_date       date,
    ADD COLUMN IF NOT EXISTS graduation_semester        varchar(20),
    ADD COLUMN IF NOT EXISTS graduation_class           int,
    ADD COLUMN IF NOT EXISTS profile_updated_date        date,
    ADD COLUMN IF NOT EXISTS profile_updated_by_user_id bigint;

-- FK: profile_updated_by_user_id -> users(user_id) ON DELETE SET NULL. Guarded
-- so a re-run does not error on the already-present constraint.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_alumni_profile_updated_by_user_id'
    ) THEN
        ALTER TABLE alumni
            ADD CONSTRAINT fk_alumni_profile_updated_by_user_id
            FOREIGN KEY (profile_updated_by_user_id)
            REFERENCES users (user_id) ON DELETE SET NULL;
    END IF;
END$$;

CREATE INDEX IF NOT EXISTS idx_alumni_profile_updated_by_user_id
    ON alumni (profile_updated_by_user_id);

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type, character_maximum_length
-- FROM information_schema.columns
-- WHERE table_name = 'alumni'
--   AND column_name IN ('citizenship','marital_status','home_country',
--       'employment_status','other_designations','survey_completed_date',
--       'graduation_semester','graduation_class','profile_updated_date',
--       'profile_updated_by_user_id')
-- ORDER BY column_name;

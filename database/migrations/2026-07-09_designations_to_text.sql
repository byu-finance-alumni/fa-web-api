-- =============================================================================
-- Migration: convert CFP/CFA/CPA designations from boolean to free-text
-- Date: 2026-07-09
-- -----------------------------------------------------------------------------
-- cfp_designation, cfa_designation and cpa_designation on
-- alumni_program_engagement were boolean NOT NULL DEFAULT false. They become
-- nullable varchar(100) free-text so values like 'CFP Level 1',
-- 'CFA all 3 levels' or 'CPA (Utah)' can be stored and displayed.
--
-- Existing data converts: true -> the label ('CFP'/'CFA'/'CPA'), false -> NULL.
--
-- SAFE TO RE-RUN: each column is guarded on data_type = 'boolean', so once a
-- column has already been converted to varchar the block is skipped.
-- =============================================================================

BEGIN;

-- cfp_designation
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alumni_program_engagement'
          AND column_name = 'cfp_designation'
          AND data_type = 'boolean'
    ) THEN
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cfp_designation DROP DEFAULT;
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cfp_designation TYPE varchar(100)
            USING (CASE WHEN cfp_designation THEN 'CFP' ELSE NULL END);
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cfp_designation DROP NOT NULL;
    END IF;
END $$;

-- cfa_designation
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alumni_program_engagement'
          AND column_name = 'cfa_designation'
          AND data_type = 'boolean'
    ) THEN
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cfa_designation DROP DEFAULT;
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cfa_designation TYPE varchar(100)
            USING (CASE WHEN cfa_designation THEN 'CFA' ELSE NULL END);
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cfa_designation DROP NOT NULL;
    END IF;
END $$;

-- cpa_designation
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alumni_program_engagement'
          AND column_name = 'cpa_designation'
          AND data_type = 'boolean'
    ) THEN
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cpa_designation DROP DEFAULT;
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cpa_designation TYPE varchar(100)
            USING (CASE WHEN cpa_designation THEN 'CPA' ELSE NULL END);
        ALTER TABLE alumni_program_engagement
            ALTER COLUMN cpa_designation DROP NOT NULL;
    END IF;
END $$;

COMMIT;

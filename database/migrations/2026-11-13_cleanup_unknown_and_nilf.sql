-- =============================================================================
-- Migration: one-time data cleanup for #672, #671, #673 (run by CI, not by hand)
-- Date: 2026-11-13
-- -----------------------------------------------------------------------------
-- WHY. Jake asked (2026-11-13) for the three hand-run maintenance scripts in
-- database/maintenance/ (672-, 671-, 673-) to run as a migration instead. This
-- file is Section 0 + Section 2 of each of them, in the required order, in ONE
-- transaction, with every changed value copied to `data_cleanup_backup` first so
-- the change can be reversed (see ROLLBACK at the bottom).
--
--   1. #672  every spelling of unknown ("UNKNOWN", "unkown", " Unknown ") in the
--            listed text fields becomes exactly 'Unknown'. Whole-value only:
--            "Unknown startup" is left alone.
--   2. #671  a job whose company is 'Unknown' gets industry 'Unknown' -- ALWAYS,
--            even over a real industry (Jake's decision, 2026-11-11).
--   3. #673  a job whose company is any spelling of "Not in the Labor Force"
--            gets company 'Not in the Labor Force', industry 'Other', secondary
--            'Not in the Labor Force' (current job only), and the alum's
--            employment status 'Not in the Labor Force' (current job only).
--
-- The rules are the same ones the app now applies on every save
-- (app/core/data_rules.py), so once this runs nothing brings old values back.
--
-- Requires 2026-11-11_add_not_in_labor_force_industry.sql (lexically earlier).
-- Runs in one psql session (migrate.sh runs each file with `psql -f`), so the
-- pg_temp helpers live for the whole file and vanish afterwards.
--
-- SAFE TO RE-RUN: every UPDATE only touches rows not already in the target
-- state, so a second run changes and backs up nothing.
-- =============================================================================

BEGIN;

-- ----------------------------------------------------------------------------
-- Backup of every value this cleanup changes. Deny-all RLS like every table
-- (registered in database/rls_lockdown.sql). Kept so the change is reversible;
-- drop it once Jake is happy with the result.
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_cleanup_backup (
    data_cleanup_backup_id  bigserial PRIMARY KEY,
    cleanup                 text NOT NULL,
    table_name              text NOT NULL,
    row_id                  bigint NOT NULL,
    column_name             text NOT NULL,
    old_value               text,
    old_updated_at          timestamptz,
    backed_up_at            timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE data_cleanup_backup ENABLE ROW LEVEL SECURITY;

-- ----------------------------------------------------------------------------
-- Helpers (session-only, identical to the maintenance scripts' Section 0).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pg_temp.fa_fold(v text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(regexp_replace(btrim(v, E' \t\n\r\f' || chr(11) || chr(160)), '\s+', ' ', 'g'))
$$;

CREATE OR REPLACE FUNCTION pg_temp.fa_is_unknown(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT pg_temp.fa_fold(v) ~ '^u[unkow]{4,9}$'
       AND strpos(pg_temp.fa_fold(v), 'k') > 0
       AND strpos(pg_temp.fa_fold(v), 'n') > 0
$$;

CREATE OR REPLACE FUNCTION pg_temp.fa_is_nlf(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT pg_temp.fa_fold(v) ~ '^not in (the )?labou?r[ -]?force$'
$$;

-- ----------------------------------------------------------------------------
-- 1. #672 -- unify every spelling of unknown to 'Unknown'.
--    Same 33 fields as 672-unify-unknown.sql. updated_at is not touched
--    (spelling-only, as in the maintenance script).
-- ----------------------------------------------------------------------------
DO $$
DECLARE
    f record;
BEGIN
    FOR f IN
        SELECT * FROM (VALUES
            ('alumni', 'alumni_id', 'employment_status'),
            ('alumni', 'alumni_id', 'citizenship'),
            ('alumni', 'alumni_id', 'marital_status'),
            ('alumni', 'alumni_id', 'hometown'),
            ('alumni', 'alumni_id', 'home_country'),
            ('alumni', 'alumni_id', 'graduate_degree'),
            ('alumni', 'alumni_id', 'graduate_school'),
            ('alumni', 'alumni_id', 'mba_program'),
            ('alumni', 'alumni_id', 'law_school'),
            ('alumni', 'alumni_id', 'medical_school'),
            ('alumni_contact_info', 'contact_info_id', 'city'),
            ('alumni_contact_info', 'contact_info_id', 'state'),
            ('alumni_contact_info', 'contact_info_id', 'country'),
            ('alumni_contact_info', 'contact_info_id', 'region'),
            ('current_employment', 'current_employment_id', 'current_employer'),
            ('current_employment', 'current_employment_id', 'current_title'),
            ('current_employment', 'current_employment_id', 'current_industry'),
            ('current_employment', 'current_employment_id', 'current_industry_secondary'),
            ('current_employment', 'current_employment_id', 'current_city'),
            ('current_employment', 'current_employment_id', 'current_state'),
            ('current_employment', 'current_employment_id', 'current_country'),
            ('current_employment', 'current_employment_id', 'seniority_level'),
            ('employment_history', 'employment_history_id', 'employer_name'),
            ('employment_history', 'employment_history_id', 'employment_title'),
            ('employment_history', 'employment_history_id', 'employment_industry'),
            ('employment_history', 'employment_history_id', 'city'),
            ('employment_history', 'employment_history_id', 'state'),
            ('education_history', 'education_id', 'university'),
            ('education_history', 'education_id', 'college'),
            ('education_history', 'education_id', 'department'),
            ('education_history', 'education_id', 'degree'),
            ('education_history', 'education_id', 'major'),
            ('education_history', 'education_id', 'degree_status')
        ) AS t(tbl, pk, col)
    LOOP
        EXECUTE format(
            'INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
             SELECT %L, %L, %I, %L, %I::text, updated_at FROM %I
              WHERE pg_temp.fa_is_unknown(%I) AND %I <> %L',
            '672', f.tbl, f.pk, f.col, f.col, f.tbl, f.col, f.col, 'Unknown');
        EXECUTE format(
            'UPDATE %I SET %I = %L WHERE pg_temp.fa_is_unknown(%I) AND %I <> %L',
            f.tbl, f.col, 'Unknown', f.col, f.col, 'Unknown');
    END LOOP;
END
$$;

-- ----------------------------------------------------------------------------
-- 2. #671 -- company 'Unknown' -> industry 'Unknown' (overwrites a real one).
-- ----------------------------------------------------------------------------
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '671', 'current_employment', current_employment_id, 'current_industry', current_industry, updated_at
  FROM current_employment
 WHERE current_employer = 'Unknown'
   AND current_industry IS DISTINCT FROM 'Unknown';

UPDATE current_employment
   SET current_industry = 'Unknown',
       updated_at = now()
 WHERE current_employer = 'Unknown'
   AND current_industry IS DISTINCT FROM 'Unknown';

INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '671', 'employment_history', employment_history_id, 'employment_industry', employment_industry, updated_at
  FROM employment_history
 WHERE employer_name = 'Unknown'
   AND employment_industry IS DISTINCT FROM 'Unknown';

UPDATE employment_history
   SET employment_industry = 'Unknown',
       updated_at = now()
 WHERE employer_name = 'Unknown'
   AND employment_industry IS DISTINCT FROM 'Unknown';

-- ----------------------------------------------------------------------------
-- 3. #673 -- Not in the Labor Force. Same four steps as 673-not-in-labor-force.sql.
--    alumni.updated_at is NOT bumped (it drives "Last updated <date> by <name>").
-- ----------------------------------------------------------------------------

-- Step 1: the alum's employment status, from their CURRENT job's company (or a
-- status that is itself a variant).
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '673', 'alumni', a.alumni_id, 'employment_status', a.employment_status, a.updated_at
  FROM alumni a
 WHERE a.employment_status IS DISTINCT FROM 'Not in the Labor Force'
   AND (pg_temp.fa_is_nlf(a.employment_status)
        OR EXISTS (SELECT 1 FROM current_employment ce
                   WHERE ce.alumni_id = a.alumni_id
                     AND pg_temp.fa_is_nlf(ce.current_employer)));

UPDATE alumni a
   SET employment_status = 'Not in the Labor Force'
 WHERE a.employment_status IS DISTINCT FROM 'Not in the Labor Force'
   AND (pg_temp.fa_is_nlf(a.employment_status)
        OR EXISTS (SELECT 1 FROM current_employment ce
                   WHERE ce.alumni_id = a.alumni_id
                     AND pg_temp.fa_is_nlf(ce.current_employer)));

-- Step 2: current jobs whose company matches (back up all three fields).
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '673', 'current_employment', ce.current_employment_id, v.col, v.val, ce.updated_at
  FROM current_employment ce
 CROSS JOIN LATERAL (VALUES
        ('current_employer', ce.current_employer),
        ('current_industry', ce.current_industry),
        ('current_industry_secondary', ce.current_industry_secondary)
      ) AS v(col, val)
 WHERE pg_temp.fa_is_nlf(ce.current_employer)
   AND (ce.current_employer <> 'Not in the Labor Force'
        OR ce.current_industry IS DISTINCT FROM 'Other'
        OR ce.current_industry_secondary IS DISTINCT FROM 'Not in the Labor Force');

UPDATE current_employment
   SET current_employer = 'Not in the Labor Force',
       current_industry = 'Other',
       current_industry_secondary = 'Not in the Labor Force',
       updated_at = now()
 WHERE pg_temp.fa_is_nlf(current_employer)
   AND (current_employer <> 'Not in the Labor Force'
        OR current_industry IS DISTINCT FROM 'Other'
        OR current_industry_secondary IS DISTINCT FROM 'Not in the Labor Force');

-- Step 3: a secondary industry that is itself a variant (spelling only).
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '673', 'current_employment', current_employment_id, 'current_industry_secondary',
       current_industry_secondary, updated_at
  FROM current_employment
 WHERE pg_temp.fa_is_nlf(current_industry_secondary)
   AND current_industry_secondary <> 'Not in the Labor Force';

UPDATE current_employment
   SET current_industry_secondary = 'Not in the Labor Force',
       updated_at = now()
 WHERE pg_temp.fa_is_nlf(current_industry_secondary)
   AND current_industry_secondary <> 'Not in the Labor Force';

-- Step 4: past jobs whose company matches.
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '673', 'employment_history', eh.employment_history_id, v.col, v.val, eh.updated_at
  FROM employment_history eh
 CROSS JOIN LATERAL (VALUES
        ('employer_name', eh.employer_name),
        ('employment_industry', eh.employment_industry)
      ) AS v(col, val)
 WHERE pg_temp.fa_is_nlf(eh.employer_name)
   AND (eh.employer_name <> 'Not in the Labor Force'
        OR eh.employment_industry IS DISTINCT FROM 'Other');

UPDATE employment_history
   SET employer_name = 'Not in the Labor Force',
       employment_industry = 'Other',
       updated_at = now()
 WHERE pg_temp.fa_is_nlf(employer_name)
   AND (employer_name <> 'Not in the Labor Force'
        OR employment_industry IS DISTINCT FROM 'Other');

-- What changed, by cleanup and field (printed in the CI migrate log).
SELECT cleanup, table_name, column_name, count(*) AS values_changed
  FROM data_cleanup_backup
 WHERE backed_up_at = now()   -- this transaction's rows
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3;

COMMIT;

-- =============================================================================
-- ROLLBACK (by hand, only if the cleanup must be undone). Restores every value
-- in the order it was changed, newest first, then the job rows' updated_at:
--
-- BEGIN;
-- DO $$
-- DECLARE b record; pk text;
-- BEGIN
--   FOR b IN SELECT * FROM data_cleanup_backup ORDER BY data_cleanup_backup_id DESC LOOP
--     pk := CASE b.table_name
--             WHEN 'alumni' THEN 'alumni_id'
--             WHEN 'alumni_contact_info' THEN 'contact_info_id'
--             WHEN 'current_employment' THEN 'current_employment_id'
--             WHEN 'employment_history' THEN 'employment_history_id'
--             WHEN 'education_history' THEN 'education_id' END;
--     EXECUTE format('UPDATE %I SET %I = $1 WHERE %I = $2', b.table_name, b.column_name, pk)
--       USING b.old_value, b.row_id;
--   END LOOP;
-- END $$;
-- COMMIT;
--
-- When Jake is happy with the result:  DROP TABLE data_cleanup_backup;
-- =============================================================================

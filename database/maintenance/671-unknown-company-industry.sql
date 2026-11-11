-- ============================================================================
-- 671-unknown-company-industry.sql
-- Issue fa-web-api #671 -- a job whose company is Unknown gets industry Unknown
--              (not Other). Decisions, Jake 2026-11-11:
--                * both this one-time fix AND the same rule on every save (the
--                  code shipped with this file does the "on save" half);
--                * ALWAYS Unknown -- even when the job already has a real
--                  industry such as Investment Banking. Overwrite it.
--
-- Drafted:   2026-11-11
-- Target:    PRODUCTION database (njobhhdopwdodvzosrns).
-- Author:    drafted by Claude from the fa-web-api models. Never run against
--            prod by Claude.
-- Runner:    Jake, by hand (Supabase SQL editor or psql).
--
-- This file lives in database/maintenance/, which database/migrate.sh does NOT
-- glob (it only applies database/migrations/*.sql). CI will never run it.
--
-- RUN 672-unify-unknown.sql FIRST. This file matches the company on EXACTLY
-- 'Unknown', which is only every spelling of it once 672 has been committed.
-- Section 0 lists any company that still looks like a spelling of unknown but
-- is not exactly 'Unknown' -- expect none; if any show up, 672 has not run.
--
-- WHICH JOBS -- every job row, not just the current one:
--   current_employment  (one per alum):  current_employer  -> current_industry
--   employment_history  (past jobs):     employer_name     -> employment_industry
--   The secondary industry is NOT touched.
--
-- THE RULE: company = 'Unknown'  ->  industry = 'Unknown'
--   (whatever it held: blank, Other, or a real industry).
--
-- updated_at is set on the job rows that change (as in 875-...). The alumni row
-- is not touched.
--
-- HOW TO RUN
--   1. Run SECTION 0 and SECTION 1 (read-only). Check the rows and counts.
--   2. Run SECTION 2 as one block. It opens a transaction, updates, and shows
--      the counts AFTER the change. Nothing is saved yet.
--   3. If the after-counts look right, run COMMIT;   If not, run ROLLBACK;
--   NOTE (Supabase SQL editor): the editor runs each "Run" as its own
--   transaction, so BEGIN/COMMIT across separate runs does not hold there.
--   Use psql for the check-then-commit step, or run Section 2 in the editor
--   with ROLLBACK first to look, then again with COMMIT.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- SECTION 0 (read-only): companies that still look like a spelling of unknown
-- but are not exactly 'Unknown'. Expect 0 rows (672 fixes them). Same test as
-- 672's pg_temp.fa_is_unknown, written inline here.
-- ----------------------------------------------------------------------------
SELECT 'current_employment' AS table_name, current_employer AS company, count(*) AS n
FROM current_employment
WHERE lower(regexp_replace(btrim(current_employer, E' \t\n\r\f' || chr(11) || chr(160)), '\s+', ' ', 'g')) ~ '^u[unkow]{4,9}$'
  AND current_employer <> 'Unknown'
GROUP BY current_employer
UNION ALL
SELECT 'employment_history', employer_name, count(*)
FROM employment_history
WHERE lower(regexp_replace(btrim(employer_name, E' \t\n\r\f' || chr(11) || chr(160)), '\s+', ' ', 'g')) ~ '^u[unkow]{4,9}$'
  AND employer_name <> 'Unknown'
GROUP BY employer_name;


-- ----------------------------------------------------------------------------
-- SECTION 1 (read-only): PREVIEW.
-- 1a. The jobs that will change, with the industry they hold now.
-- ----------------------------------------------------------------------------
SELECT 'current_employment' AS table_name,
       ce.current_employment_id AS row_id,
       ce.alumni_id,
       a.first_name,
       a.last_name,
       ce.current_employer AS company,
       ce.current_industry AS industry_before,
       'Unknown' AS industry_after
FROM current_employment ce
JOIN alumni a ON a.alumni_id = ce.alumni_id
WHERE ce.current_employer = 'Unknown'
  AND ce.current_industry IS DISTINCT FROM 'Unknown'
UNION ALL
SELECT 'employment_history',
       eh.employment_history_id,
       eh.alumni_id,
       a.first_name,
       a.last_name,
       eh.employer_name,
       eh.employment_industry,
       'Unknown'
FROM employment_history eh
JOIN alumni a ON a.alumni_id = eh.alumni_id
WHERE eh.employer_name = 'Unknown'
  AND eh.employment_industry IS DISTINCT FROM 'Unknown'
ORDER BY 1, 5, 4, 3;

-- 1b. Counts per field, by the industry each will be changed FROM (before
--     counts for the PR). industry_before NULL = blank.
SELECT 'current_employment.current_industry' AS field,
       current_industry AS industry_before,
       count(*) AS rows_to_change
FROM current_employment
WHERE current_employer = 'Unknown'
  AND current_industry IS DISTINCT FROM 'Unknown'
GROUP BY current_industry
UNION ALL
SELECT 'employment_history.employment_industry',
       employment_industry,
       count(*)
FROM employment_history
WHERE employer_name = 'Unknown'
  AND employment_industry IS DISTINCT FROM 'Unknown'
GROUP BY employment_industry
ORDER BY 1, 3 DESC;


-- ----------------------------------------------------------------------------
-- SECTION 2: the change, inside a transaction. Run this whole block together.
-- psql prints "UPDATE n" after each statement -- n should match the totals from
-- Section 1b for that field.
-- ----------------------------------------------------------------------------
BEGIN;

UPDATE current_employment
   SET current_industry = 'Unknown',
       updated_at = now()
 WHERE current_employer = 'Unknown'
   AND current_industry IS DISTINCT FROM 'Unknown';

UPDATE employment_history
   SET employment_industry = 'Unknown',
       updated_at = now()
 WHERE employer_name = 'Unknown'
   AND employment_industry IS DISTINCT FROM 'Unknown';

-- CHECK: per field AFTER the change. rows_left_to_change must be 0;
-- unknown_company_jobs is how many jobs have company Unknown (all of them now
-- with industry Unknown).
SELECT 'current_employment.current_industry' AS field,
       count(*) FILTER (WHERE current_industry IS DISTINCT FROM 'Unknown') AS rows_left_to_change,
       count(*) AS unknown_company_jobs
FROM current_employment
WHERE current_employer = 'Unknown'
UNION ALL
SELECT 'employment_history.employment_industry',
       count(*) FILTER (WHERE employment_industry IS DISTINCT FROM 'Unknown'),
       count(*)
FROM employment_history
WHERE employer_name = 'Unknown';

-- Decide.
--   Looks right?   run:  COMMIT;
--   Anything off?  run:  ROLLBACK;   (nothing is saved, the rows go back)
-- COMMIT;

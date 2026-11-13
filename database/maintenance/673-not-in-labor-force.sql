-- ============================================================================
-- NOTE: SUPERSEDED 2026-11-13: this now runs automatically as
--    database/migrations/2026-11-13_cleanup_unknown_and_nilf.sql (Jake asked).
--    Kept for reference / the read-only preview. Do NOT also run it by hand.
-- 673-not-in-labor-force.sql
-- Issue fa-web-api #673 -- a job whose company is listed as "Not in the Labor
--              Force" gets (Jake, 2026-11-11):
--                * company text        -> 'Not in the Labor Force' (one spelling)
--                * industry            -> 'Other'
--                * secondary industry  -> 'Not in the Labor Force'
--                * employment status   -> 'Not in the Labor Force'  (the alum's,
--                  from their CURRENT job only -- status describes the person
--                  now, not a past role)
--              Both this one-time fix AND the same rule on every save (the code
--              shipped with this file does the "on save" half).
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
-- RUN ORDER: 672-unify-unknown.sql, then 671-..., then this file. Ship the code
-- first: it carries the migration that adds 'Not in the Labor Force' to the
-- secondary-industry dropdown, so the new secondary values are on the list.
--
-- MATCHING THE COMPANY (any way it is written): surrounding spaces trimmed,
-- inner runs of spaces collapsed, any case, with or without "the", labor or
-- labour, "labor force" / "labor-force" / "laborforce". Precisely the regex
--   '^not in (the )?labou?r[ -]?force$'
-- on the folded value (the same test the app uses). "Not in the Labor Force
-- yet" or "Labor Force Inc" do NOT match. Section 0 lists every distinct
-- company that matches, so you can see the variants before anything changes.
--
-- WHICH JOBS:
--   current_employment  -> company, industry, secondary industry + the alum's
--                          employment_status
--   employment_history  -> company, industry (past jobs have no secondary
--                          column, and do not change the status)
--
-- ALSO (same spirit as #672, spelling only): an employment_status or a
-- secondary industry that is itself a variant of "not in labor force" is set
-- to the canonical 'Not in the Labor Force'. Section 1 counts those apart.
--
-- A secondary industry the job already had (e.g. 'Consulting') IS replaced by
-- 'Not in the Labor Force' -- that is the rule. Section 1a shows each one.
--
-- updated_at is set on the job rows that change (as in 875-...). alumni.updated_at
-- is NOT bumped: it drives the profile's "Last updated <date> by <name>", and a
-- hand-run fix would wrongly credit whoever edited the record last.
--
-- HOW TO RUN
--   1. Run SECTION 0 (session-only helper in pg_temp; creates nothing permanent).
--   2. Run SECTION 1 (read-only). Check the rows and counts (before, for the PR).
--   3. Run SECTION 2 as one block. It opens a transaction, updates, and shows
--      the counts AFTER the change. Nothing is saved yet.
--   4. If the after-counts look right, run COMMIT;   If not, run ROLLBACK;
--   NOTE (Supabase SQL editor): the editor runs each "Run" as its own
--   session/transaction, so pg_temp functions and BEGIN/COMMIT do not survive
--   between runs there. In the editor, paste SECTION 0 together with whichever
--   section you run; for the change, run SECTION 0 + SECTION 2 with ROLLBACK
--   first to look, then again with COMMIT. psql keeps one session throughout.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- SECTION 0: session-only helper (pg_temp -- nothing is created permanently).
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION pg_temp.fa_is_nlf(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(regexp_replace(btrim(v, E' \t\n\r\f' || chr(11) || chr(160)), '\s+', ' ', 'g'))
           ~ '^not in (the )?labou?r[ -]?force$'
$$;


-- ----------------------------------------------------------------------------
-- SECTION 1 (read-only): PREVIEW.
-- 1a. Every distinct company that matches, and how many jobs hold it.
-- ----------------------------------------------------------------------------
SELECT 'current_employment' AS table_name, current_employer AS company, count(*) AS jobs
FROM current_employment
WHERE pg_temp.fa_is_nlf(current_employer)
GROUP BY current_employer
UNION ALL
SELECT 'employment_history', employer_name, count(*)
FROM employment_history
WHERE pg_temp.fa_is_nlf(employer_name)
GROUP BY employer_name
ORDER BY 1, 3 DESC;

-- 1b. The CURRENT jobs that match, before -> after, with the alum's status.
SELECT ce.current_employment_id,
       ce.alumni_id,
       a.first_name,
       a.last_name,
       ce.current_employer            AS company_before,
       ce.current_industry            AS industry_before,
       ce.current_industry_secondary  AS secondary_before,
       a.employment_status            AS status_before,
       'Not in the Labor Force'       AS company_after,
       'Other'                        AS industry_after,
       'Not in the Labor Force'       AS secondary_after,
       'Not in the Labor Force'       AS status_after
FROM current_employment ce
JOIN alumni a ON a.alumni_id = ce.alumni_id
WHERE pg_temp.fa_is_nlf(ce.current_employer)
ORDER BY a.last_name, a.first_name, ce.alumni_id;

-- 1c. The PAST jobs that match, before -> after.
SELECT eh.employment_history_id,
       eh.alumni_id,
       a.first_name,
       a.last_name,
       eh.employer_name        AS company_before,
       eh.employment_industry  AS industry_before,
       'Not in the Labor Force' AS company_after,
       'Other'                 AS industry_after
FROM employment_history eh
JOIN alumni a ON a.alumni_id = eh.alumni_id
WHERE pg_temp.fa_is_nlf(eh.employer_name)
ORDER BY a.last_name, a.first_name, eh.employment_history_id;

-- 1d. How many rows will change, per field (before counts for the PR).
SELECT 'current_employment.current_employer' AS field,
       count(*) AS rows_to_change
FROM current_employment
WHERE pg_temp.fa_is_nlf(current_employer)
  AND current_employer <> 'Not in the Labor Force'
UNION ALL
SELECT 'current_employment.current_industry', count(*)
FROM current_employment
WHERE pg_temp.fa_is_nlf(current_employer)
  AND current_industry IS DISTINCT FROM 'Other'
UNION ALL
SELECT 'current_employment.current_industry_secondary', count(*)
FROM current_employment
WHERE (pg_temp.fa_is_nlf(current_employer) OR pg_temp.fa_is_nlf(current_industry_secondary))
  AND current_industry_secondary IS DISTINCT FROM 'Not in the Labor Force'
UNION ALL
SELECT 'alumni.employment_status (company rule)', count(*)
FROM alumni a
WHERE EXISTS (SELECT 1 FROM current_employment ce
              WHERE ce.alumni_id = a.alumni_id AND pg_temp.fa_is_nlf(ce.current_employer))
  AND a.employment_status IS DISTINCT FROM 'Not in the Labor Force'
UNION ALL
SELECT 'alumni.employment_status (spelling only)', count(*)
FROM alumni a
WHERE pg_temp.fa_is_nlf(a.employment_status)
  AND a.employment_status <> 'Not in the Labor Force'
  AND NOT EXISTS (SELECT 1 FROM current_employment ce
                  WHERE ce.alumni_id = a.alumni_id AND pg_temp.fa_is_nlf(ce.current_employer))
UNION ALL
SELECT 'employment_history.employer_name', count(*)
FROM employment_history
WHERE pg_temp.fa_is_nlf(employer_name)
  AND employer_name <> 'Not in the Labor Force'
UNION ALL
SELECT 'employment_history.employment_industry', count(*)
FROM employment_history
WHERE pg_temp.fa_is_nlf(employer_name)
  AND employment_industry IS DISTINCT FROM 'Other';


-- ----------------------------------------------------------------------------
-- SECTION 2: the change, inside a transaction. Run this whole block together.
-- psql prints "UPDATE n" after each statement. A row counts once per UPDATE even
-- when several of its fields change, so n can be lower than the field counts.
-- ----------------------------------------------------------------------------
BEGIN;

-- Step 1: the alum's employment status, from their CURRENT job's company (and
-- the spelling-only fix of a status that is itself a variant). Runs first only
-- for readability -- the company still matches after step 2 either way.
UPDATE alumni a
   SET employment_status = 'Not in the Labor Force'
 WHERE a.employment_status IS DISTINCT FROM 'Not in the Labor Force'
   AND (
        pg_temp.fa_is_nlf(a.employment_status)
        OR EXISTS (SELECT 1 FROM current_employment ce
                   WHERE ce.alumni_id = a.alumni_id
                     AND pg_temp.fa_is_nlf(ce.current_employer))
   );

-- Step 2: current jobs whose company matches.
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
UPDATE current_employment
   SET current_industry_secondary = 'Not in the Labor Force',
       updated_at = now()
 WHERE pg_temp.fa_is_nlf(current_industry_secondary)
   AND current_industry_secondary <> 'Not in the Labor Force';

-- Step 4: past jobs whose company matches.
UPDATE employment_history
   SET employer_name = 'Not in the Labor Force',
       employment_industry = 'Other',
       updated_at = now()
 WHERE pg_temp.fa_is_nlf(employer_name)
   AND (employer_name <> 'Not in the Labor Force'
        OR employment_industry IS DISTINCT FROM 'Other');

-- CHECK: per field AFTER the change. rows_left_to_change must be 0 on every
-- row; rows_now is how many rows hold the target value for that rule.
SELECT 'current_employment (company rule)' AS field,
       count(*) FILTER (WHERE current_employer <> 'Not in the Labor Force'
                          OR current_industry IS DISTINCT FROM 'Other'
                          OR current_industry_secondary IS DISTINCT FROM 'Not in the Labor Force')
           AS rows_left_to_change,
       count(*) AS rows_now
FROM current_employment
WHERE pg_temp.fa_is_nlf(current_employer)
UNION ALL
SELECT 'current_employment.current_industry_secondary (spelling)',
       count(*) FILTER (WHERE current_industry_secondary <> 'Not in the Labor Force'),
       count(*) FILTER (WHERE current_industry_secondary = 'Not in the Labor Force')
FROM current_employment
WHERE pg_temp.fa_is_nlf(current_industry_secondary)
UNION ALL
SELECT 'alumni.employment_status',
       count(*) FILTER (WHERE a.employment_status IS DISTINCT FROM 'Not in the Labor Force'
                          AND (pg_temp.fa_is_nlf(a.employment_status)
                               OR EXISTS (SELECT 1 FROM current_employment ce
                                          WHERE ce.alumni_id = a.alumni_id
                                            AND pg_temp.fa_is_nlf(ce.current_employer)))),
       count(*) FILTER (WHERE a.employment_status = 'Not in the Labor Force')
FROM alumni a
UNION ALL
SELECT 'employment_history (company rule)',
       count(*) FILTER (WHERE employer_name <> 'Not in the Labor Force'
                          OR employment_industry IS DISTINCT FROM 'Other'),
       count(*)
FROM employment_history
WHERE pg_temp.fa_is_nlf(employer_name);

-- Decide.
--   Looks right?   run:  COMMIT;
--   Anything off?  run:  ROLLBACK;   (nothing is saved, the rows go back)
-- COMMIT;

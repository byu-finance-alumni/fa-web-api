-- ============================================================================
-- 672-unify-unknown.sql
-- Issue fa-web-api #672 -- every spelling of "unknown" becomes exactly "Unknown"
--              (Jake, 2026-11-11): capital U, the rest lowercase -- what the
--              dropdowns already save, so filters, exports and the dashboard
--              group them together.
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
-- RUN ORDER: this file FIRST, then 671-unknown-company-industry.sql (which
-- matches the company on exactly 'Unknown'), then 673-not-in-labor-force.sql.
-- The code that ships with these files applies the same rules on every save
-- (app/core/data_rules.py), so once these are run nothing brings them back.
--
-- THE RULE
--   A value is rewritten ONLY when the WHOLE value (surrounding spaces trimmed,
--   any case) is a spelling of unknown: "UNKNOWN", "unknown", " Unknown ",
--   "UNKOWN", "unkown", "unknwn", "Unknow", "uknown", ... Precisely: after
--   trimming, collapsing spaces and lowercasing, 5-10 letters drawn only from
--   u/n/k/o/w, starting with "u" and containing a "k" and an "n" (the same test
--   the app uses -- see pg_temp.fa_is_unknown below). No real word passes it.
--   "Unknown startup", "Unknown Co." etc. are left alone.
--   Same idea as the employment-status cleanup: fix the spelling, change nothing
--   else. Off-list values that are not a spelling of unknown are untouched.
--
-- THE COLUMNS (every free-text column where "unknown" plausibly appears; the
-- app's list is data_rules.UNKNOWN_COLUMNS and a test keeps the two in step):
--   alumni.employment_status
--   alumni.citizenship
--   alumni.marital_status
--   alumni.hometown
--   alumni.home_country
--   alumni.graduate_degree
--   alumni.graduate_school
--   alumni.mba_program
--   alumni.law_school
--   alumni.medical_school
--   alumni_contact_info.city
--   alumni_contact_info.state
--   alumni_contact_info.country
--   alumni_contact_info.region
--   current_employment.current_employer
--   current_employment.current_title
--   current_employment.current_industry
--   current_employment.current_industry_secondary
--   current_employment.current_city
--   current_employment.current_state
--   current_employment.current_country
--   current_employment.seniority_level
--   employment_history.employer_name
--   employment_history.employment_title
--   employment_history.employment_industry
--   employment_history.city
--   employment_history.state
--   education_history.university
--   education_history.college
--   education_history.department
--   education_history.degree
--   education_history.major
--   education_history.degree_status
--   Deliberately NOT touched: names, emails, phones, URLs, notes, gender (a
--   validated list without Unknown), and every id / date / number column.
--
-- updated_at IS NOT BUMPED. This is a spelling fix, not an edit by a person;
-- alumni.updated_at drives the profile's "Last updated <date> by <name>", and a
-- bulk cleanup would wrongly credit whoever edited each record last.
-- No audit_logs rows are written (same as the earlier hand-run cleanups).
--
-- HOW TO RUN
--   1. Run SECTION 0 (creates two session-only helper functions in pg_temp; they
--      vanish when the session ends and change nothing in the database).
--   2. Run SECTION 1 (read-only). It lists every variant found and how many rows
--      hold it, then the per-field "will change" counts. Put those counts in the
--      PR (before).
--   3. Run SECTION 2 as one block. It opens a transaction, updates, and shows
--      the per-field counts AFTER the change (rows_left_to_fix must be 0
--      everywhere). Nothing is saved yet.
--   4. If the after-counts look right, run COMMIT;   If not, run ROLLBACK;
--   NOTE (Supabase SQL editor): the editor runs each "Run" as its own
--   session/transaction, so pg_temp functions and BEGIN/COMMIT do not survive
--   between runs there. In the editor, paste SECTION 0 together with whichever
--   section you run; for the change, run SECTION 0 + SECTION 2 with ROLLBACK
--   first to look, then again with COMMIT. psql keeps one session throughout.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- SECTION 0: session-only helpers (pg_temp -- nothing is created permanently).
-- fa_fold    : trim surrounding spaces, collapse inner runs of spaces, lowercase.
-- fa_is_unknown : the "is this just a spelling of unknown" test.
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


-- ----------------------------------------------------------------------------
-- SECTION 1 (read-only): PREVIEW.
-- 1a. Every variant that will be rewritten, with how many rows hold it.
--     Use this to sanity-check that only spellings of unknown are listed.
-- ----------------------------------------------------------------------------
SELECT 'alumni' AS table_name, 'employment_status' AS column_name, employment_status AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(employment_status) AND employment_status <> 'Unknown'
GROUP BY employment_status
UNION ALL
SELECT 'alumni' AS table_name, 'citizenship' AS column_name, citizenship AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(citizenship) AND citizenship <> 'Unknown'
GROUP BY citizenship
UNION ALL
SELECT 'alumni' AS table_name, 'marital_status' AS column_name, marital_status AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(marital_status) AND marital_status <> 'Unknown'
GROUP BY marital_status
UNION ALL
SELECT 'alumni' AS table_name, 'hometown' AS column_name, hometown AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(hometown) AND hometown <> 'Unknown'
GROUP BY hometown
UNION ALL
SELECT 'alumni' AS table_name, 'home_country' AS column_name, home_country AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(home_country) AND home_country <> 'Unknown'
GROUP BY home_country
UNION ALL
SELECT 'alumni' AS table_name, 'graduate_degree' AS column_name, graduate_degree AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(graduate_degree) AND graduate_degree <> 'Unknown'
GROUP BY graduate_degree
UNION ALL
SELECT 'alumni' AS table_name, 'graduate_school' AS column_name, graduate_school AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(graduate_school) AND graduate_school <> 'Unknown'
GROUP BY graduate_school
UNION ALL
SELECT 'alumni' AS table_name, 'mba_program' AS column_name, mba_program AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(mba_program) AND mba_program <> 'Unknown'
GROUP BY mba_program
UNION ALL
SELECT 'alumni' AS table_name, 'law_school' AS column_name, law_school AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(law_school) AND law_school <> 'Unknown'
GROUP BY law_school
UNION ALL
SELECT 'alumni' AS table_name, 'medical_school' AS column_name, medical_school AS value_now, count(*) AS rows_holding_it
FROM alumni
WHERE pg_temp.fa_is_unknown(medical_school) AND medical_school <> 'Unknown'
GROUP BY medical_school
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'city' AS column_name, city AS value_now, count(*) AS rows_holding_it
FROM alumni_contact_info
WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown'
GROUP BY city
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'state' AS column_name, state AS value_now, count(*) AS rows_holding_it
FROM alumni_contact_info
WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown'
GROUP BY state
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'country' AS column_name, country AS value_now, count(*) AS rows_holding_it
FROM alumni_contact_info
WHERE pg_temp.fa_is_unknown(country) AND country <> 'Unknown'
GROUP BY country
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'region' AS column_name, region AS value_now, count(*) AS rows_holding_it
FROM alumni_contact_info
WHERE pg_temp.fa_is_unknown(region) AND region <> 'Unknown'
GROUP BY region
UNION ALL
SELECT 'current_employment' AS table_name, 'current_employer' AS column_name, current_employer AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(current_employer) AND current_employer <> 'Unknown'
GROUP BY current_employer
UNION ALL
SELECT 'current_employment' AS table_name, 'current_title' AS column_name, current_title AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(current_title) AND current_title <> 'Unknown'
GROUP BY current_title
UNION ALL
SELECT 'current_employment' AS table_name, 'current_industry' AS column_name, current_industry AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(current_industry) AND current_industry <> 'Unknown'
GROUP BY current_industry
UNION ALL
SELECT 'current_employment' AS table_name, 'current_industry_secondary' AS column_name, current_industry_secondary AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(current_industry_secondary) AND current_industry_secondary <> 'Unknown'
GROUP BY current_industry_secondary
UNION ALL
SELECT 'current_employment' AS table_name, 'current_city' AS column_name, current_city AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(current_city) AND current_city <> 'Unknown'
GROUP BY current_city
UNION ALL
SELECT 'current_employment' AS table_name, 'current_state' AS column_name, current_state AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(current_state) AND current_state <> 'Unknown'
GROUP BY current_state
UNION ALL
SELECT 'current_employment' AS table_name, 'current_country' AS column_name, current_country AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(current_country) AND current_country <> 'Unknown'
GROUP BY current_country
UNION ALL
SELECT 'current_employment' AS table_name, 'seniority_level' AS column_name, seniority_level AS value_now, count(*) AS rows_holding_it
FROM current_employment
WHERE pg_temp.fa_is_unknown(seniority_level) AND seniority_level <> 'Unknown'
GROUP BY seniority_level
UNION ALL
SELECT 'employment_history' AS table_name, 'employer_name' AS column_name, employer_name AS value_now, count(*) AS rows_holding_it
FROM employment_history
WHERE pg_temp.fa_is_unknown(employer_name) AND employer_name <> 'Unknown'
GROUP BY employer_name
UNION ALL
SELECT 'employment_history' AS table_name, 'employment_title' AS column_name, employment_title AS value_now, count(*) AS rows_holding_it
FROM employment_history
WHERE pg_temp.fa_is_unknown(employment_title) AND employment_title <> 'Unknown'
GROUP BY employment_title
UNION ALL
SELECT 'employment_history' AS table_name, 'employment_industry' AS column_name, employment_industry AS value_now, count(*) AS rows_holding_it
FROM employment_history
WHERE pg_temp.fa_is_unknown(employment_industry) AND employment_industry <> 'Unknown'
GROUP BY employment_industry
UNION ALL
SELECT 'employment_history' AS table_name, 'city' AS column_name, city AS value_now, count(*) AS rows_holding_it
FROM employment_history
WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown'
GROUP BY city
UNION ALL
SELECT 'employment_history' AS table_name, 'state' AS column_name, state AS value_now, count(*) AS rows_holding_it
FROM employment_history
WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown'
GROUP BY state
UNION ALL
SELECT 'education_history' AS table_name, 'university' AS column_name, university AS value_now, count(*) AS rows_holding_it
FROM education_history
WHERE pg_temp.fa_is_unknown(university) AND university <> 'Unknown'
GROUP BY university
UNION ALL
SELECT 'education_history' AS table_name, 'college' AS column_name, college AS value_now, count(*) AS rows_holding_it
FROM education_history
WHERE pg_temp.fa_is_unknown(college) AND college <> 'Unknown'
GROUP BY college
UNION ALL
SELECT 'education_history' AS table_name, 'department' AS column_name, department AS value_now, count(*) AS rows_holding_it
FROM education_history
WHERE pg_temp.fa_is_unknown(department) AND department <> 'Unknown'
GROUP BY department
UNION ALL
SELECT 'education_history' AS table_name, 'degree' AS column_name, degree AS value_now, count(*) AS rows_holding_it
FROM education_history
WHERE pg_temp.fa_is_unknown(degree) AND degree <> 'Unknown'
GROUP BY degree
UNION ALL
SELECT 'education_history' AS table_name, 'major' AS column_name, major AS value_now, count(*) AS rows_holding_it
FROM education_history
WHERE pg_temp.fa_is_unknown(major) AND major <> 'Unknown'
GROUP BY major
UNION ALL
SELECT 'education_history' AS table_name, 'degree_status' AS column_name, degree_status AS value_now, count(*) AS rows_holding_it
FROM education_history
WHERE pg_temp.fa_is_unknown(degree_status) AND degree_status <> 'Unknown'
GROUP BY degree_status
ORDER BY 1, 2, 4 DESC;

-- 1b. Per field: how many rows will change, and how many already say Unknown.
--     (before counts for the PR)
SELECT 'alumni' AS table_name, 'employment_status' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employment_status) AND employment_status <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE employment_status = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'citizenship' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(citizenship) AND citizenship <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE citizenship = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'marital_status' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(marital_status) AND marital_status <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE marital_status = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'hometown' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(hometown) AND hometown <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE hometown = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'home_country' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(home_country) AND home_country <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE home_country = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'graduate_degree' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(graduate_degree) AND graduate_degree <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE graduate_degree = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'graduate_school' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(graduate_school) AND graduate_school <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE graduate_school = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'mba_program' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(mba_program) AND mba_program <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE mba_program = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'law_school' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(law_school) AND law_school <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE law_school = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'medical_school' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(medical_school) AND medical_school <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE medical_school = 'Unknown') AS already_unknown
FROM alumni
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'city' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE city = 'Unknown') AS already_unknown
FROM alumni_contact_info
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'state' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE state = 'Unknown') AS already_unknown
FROM alumni_contact_info
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'country' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(country) AND country <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE country = 'Unknown') AS already_unknown
FROM alumni_contact_info
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'region' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(region) AND region <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE region = 'Unknown') AS already_unknown
FROM alumni_contact_info
UNION ALL
SELECT 'current_employment' AS table_name, 'current_employer' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_employer) AND current_employer <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE current_employer = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_title' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_title) AND current_title <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE current_title = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_industry' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_industry) AND current_industry <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE current_industry = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_industry_secondary' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_industry_secondary) AND current_industry_secondary <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE current_industry_secondary = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_city' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_city) AND current_city <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE current_city = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_state' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_state) AND current_state <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE current_state = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_country' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_country) AND current_country <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE current_country = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'seniority_level' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(seniority_level) AND seniority_level <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE seniority_level = 'Unknown') AS already_unknown
FROM current_employment
UNION ALL
SELECT 'employment_history' AS table_name, 'employer_name' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employer_name) AND employer_name <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE employer_name = 'Unknown') AS already_unknown
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'employment_title' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employment_title) AND employment_title <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE employment_title = 'Unknown') AS already_unknown
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'employment_industry' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employment_industry) AND employment_industry <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE employment_industry = 'Unknown') AS already_unknown
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'city' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE city = 'Unknown') AS already_unknown
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'state' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE state = 'Unknown') AS already_unknown
FROM employment_history
UNION ALL
SELECT 'education_history' AS table_name, 'university' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(university) AND university <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE university = 'Unknown') AS already_unknown
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'college' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(college) AND college <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE college = 'Unknown') AS already_unknown
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'department' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(department) AND department <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE department = 'Unknown') AS already_unknown
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'degree' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(degree) AND degree <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE degree = 'Unknown') AS already_unknown
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'major' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(major) AND major <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE major = 'Unknown') AS already_unknown
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'degree_status' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(degree_status) AND degree_status <> 'Unknown') AS rows_to_fix,
       count(*) FILTER (WHERE degree_status = 'Unknown') AS already_unknown
FROM education_history
ORDER BY 1, 2;


-- ----------------------------------------------------------------------------
-- SECTION 2: the change, inside a transaction. Run this whole block together.
-- psql prints "UPDATE n" after each statement -- n should match rows_to_fix
-- for that field in 1b.
-- ----------------------------------------------------------------------------
BEGIN;

UPDATE alumni
   SET employment_status = 'Unknown'
 WHERE pg_temp.fa_is_unknown(employment_status) AND employment_status <> 'Unknown';

UPDATE alumni
   SET citizenship = 'Unknown'
 WHERE pg_temp.fa_is_unknown(citizenship) AND citizenship <> 'Unknown';

UPDATE alumni
   SET marital_status = 'Unknown'
 WHERE pg_temp.fa_is_unknown(marital_status) AND marital_status <> 'Unknown';

UPDATE alumni
   SET hometown = 'Unknown'
 WHERE pg_temp.fa_is_unknown(hometown) AND hometown <> 'Unknown';

UPDATE alumni
   SET home_country = 'Unknown'
 WHERE pg_temp.fa_is_unknown(home_country) AND home_country <> 'Unknown';

UPDATE alumni
   SET graduate_degree = 'Unknown'
 WHERE pg_temp.fa_is_unknown(graduate_degree) AND graduate_degree <> 'Unknown';

UPDATE alumni
   SET graduate_school = 'Unknown'
 WHERE pg_temp.fa_is_unknown(graduate_school) AND graduate_school <> 'Unknown';

UPDATE alumni
   SET mba_program = 'Unknown'
 WHERE pg_temp.fa_is_unknown(mba_program) AND mba_program <> 'Unknown';

UPDATE alumni
   SET law_school = 'Unknown'
 WHERE pg_temp.fa_is_unknown(law_school) AND law_school <> 'Unknown';

UPDATE alumni
   SET medical_school = 'Unknown'
 WHERE pg_temp.fa_is_unknown(medical_school) AND medical_school <> 'Unknown';

UPDATE alumni_contact_info
   SET city = 'Unknown'
 WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown';

UPDATE alumni_contact_info
   SET state = 'Unknown'
 WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown';

UPDATE alumni_contact_info
   SET country = 'Unknown'
 WHERE pg_temp.fa_is_unknown(country) AND country <> 'Unknown';

UPDATE alumni_contact_info
   SET region = 'Unknown'
 WHERE pg_temp.fa_is_unknown(region) AND region <> 'Unknown';

UPDATE current_employment
   SET current_employer = 'Unknown'
 WHERE pg_temp.fa_is_unknown(current_employer) AND current_employer <> 'Unknown';

UPDATE current_employment
   SET current_title = 'Unknown'
 WHERE pg_temp.fa_is_unknown(current_title) AND current_title <> 'Unknown';

UPDATE current_employment
   SET current_industry = 'Unknown'
 WHERE pg_temp.fa_is_unknown(current_industry) AND current_industry <> 'Unknown';

UPDATE current_employment
   SET current_industry_secondary = 'Unknown'
 WHERE pg_temp.fa_is_unknown(current_industry_secondary) AND current_industry_secondary <> 'Unknown';

UPDATE current_employment
   SET current_city = 'Unknown'
 WHERE pg_temp.fa_is_unknown(current_city) AND current_city <> 'Unknown';

UPDATE current_employment
   SET current_state = 'Unknown'
 WHERE pg_temp.fa_is_unknown(current_state) AND current_state <> 'Unknown';

UPDATE current_employment
   SET current_country = 'Unknown'
 WHERE pg_temp.fa_is_unknown(current_country) AND current_country <> 'Unknown';

UPDATE current_employment
   SET seniority_level = 'Unknown'
 WHERE pg_temp.fa_is_unknown(seniority_level) AND seniority_level <> 'Unknown';

UPDATE employment_history
   SET employer_name = 'Unknown'
 WHERE pg_temp.fa_is_unknown(employer_name) AND employer_name <> 'Unknown';

UPDATE employment_history
   SET employment_title = 'Unknown'
 WHERE pg_temp.fa_is_unknown(employment_title) AND employment_title <> 'Unknown';

UPDATE employment_history
   SET employment_industry = 'Unknown'
 WHERE pg_temp.fa_is_unknown(employment_industry) AND employment_industry <> 'Unknown';

UPDATE employment_history
   SET city = 'Unknown'
 WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown';

UPDATE employment_history
   SET state = 'Unknown'
 WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown';

UPDATE education_history
   SET university = 'Unknown'
 WHERE pg_temp.fa_is_unknown(university) AND university <> 'Unknown';

UPDATE education_history
   SET college = 'Unknown'
 WHERE pg_temp.fa_is_unknown(college) AND college <> 'Unknown';

UPDATE education_history
   SET department = 'Unknown'
 WHERE pg_temp.fa_is_unknown(department) AND department <> 'Unknown';

UPDATE education_history
   SET degree = 'Unknown'
 WHERE pg_temp.fa_is_unknown(degree) AND degree <> 'Unknown';

UPDATE education_history
   SET major = 'Unknown'
 WHERE pg_temp.fa_is_unknown(major) AND major <> 'Unknown';

UPDATE education_history
   SET degree_status = 'Unknown'
 WHERE pg_temp.fa_is_unknown(degree_status) AND degree_status <> 'Unknown';

-- CHECK: per field AFTER the change. rows_left_to_fix must be 0 on every row;
-- unknown_now should equal already_unknown + rows_to_fix from 1b.
SELECT 'alumni' AS table_name, 'employment_status' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employment_status) AND employment_status <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE employment_status = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'citizenship' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(citizenship) AND citizenship <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE citizenship = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'marital_status' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(marital_status) AND marital_status <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE marital_status = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'hometown' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(hometown) AND hometown <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE hometown = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'home_country' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(home_country) AND home_country <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE home_country = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'graduate_degree' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(graduate_degree) AND graduate_degree <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE graduate_degree = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'graduate_school' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(graduate_school) AND graduate_school <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE graduate_school = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'mba_program' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(mba_program) AND mba_program <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE mba_program = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'law_school' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(law_school) AND law_school <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE law_school = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni' AS table_name, 'medical_school' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(medical_school) AND medical_school <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE medical_school = 'Unknown') AS unknown_now
FROM alumni
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'city' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE city = 'Unknown') AS unknown_now
FROM alumni_contact_info
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'state' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE state = 'Unknown') AS unknown_now
FROM alumni_contact_info
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'country' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(country) AND country <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE country = 'Unknown') AS unknown_now
FROM alumni_contact_info
UNION ALL
SELECT 'alumni_contact_info' AS table_name, 'region' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(region) AND region <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE region = 'Unknown') AS unknown_now
FROM alumni_contact_info
UNION ALL
SELECT 'current_employment' AS table_name, 'current_employer' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_employer) AND current_employer <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE current_employer = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_title' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_title) AND current_title <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE current_title = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_industry' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_industry) AND current_industry <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE current_industry = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_industry_secondary' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_industry_secondary) AND current_industry_secondary <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE current_industry_secondary = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_city' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_city) AND current_city <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE current_city = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_state' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_state) AND current_state <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE current_state = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'current_country' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(current_country) AND current_country <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE current_country = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'current_employment' AS table_name, 'seniority_level' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(seniority_level) AND seniority_level <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE seniority_level = 'Unknown') AS unknown_now
FROM current_employment
UNION ALL
SELECT 'employment_history' AS table_name, 'employer_name' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employer_name) AND employer_name <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE employer_name = 'Unknown') AS unknown_now
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'employment_title' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employment_title) AND employment_title <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE employment_title = 'Unknown') AS unknown_now
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'employment_industry' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(employment_industry) AND employment_industry <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE employment_industry = 'Unknown') AS unknown_now
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'city' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(city) AND city <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE city = 'Unknown') AS unknown_now
FROM employment_history
UNION ALL
SELECT 'employment_history' AS table_name, 'state' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(state) AND state <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE state = 'Unknown') AS unknown_now
FROM employment_history
UNION ALL
SELECT 'education_history' AS table_name, 'university' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(university) AND university <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE university = 'Unknown') AS unknown_now
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'college' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(college) AND college <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE college = 'Unknown') AS unknown_now
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'department' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(department) AND department <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE department = 'Unknown') AS unknown_now
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'degree' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(degree) AND degree <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE degree = 'Unknown') AS unknown_now
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'major' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(major) AND major <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE major = 'Unknown') AS unknown_now
FROM education_history
UNION ALL
SELECT 'education_history' AS table_name, 'degree_status' AS column_name,
       count(*) FILTER (WHERE pg_temp.fa_is_unknown(degree_status) AND degree_status <> 'Unknown') AS rows_left_to_fix,
       count(*) FILTER (WHERE degree_status = 'Unknown') AS unknown_now
FROM education_history
ORDER BY 1, 2;

-- Decide.
--   Looks right?   run:  COMMIT;
--   Anything off?  run:  ROLLBACK;   (nothing is saved, the rows go back)
-- COMMIT;

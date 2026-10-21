-- ============================================================================
-- 875-fpa-to-corporate-finance.sql
-- Issue fa-web-app #875 -- FP&A is no longer a PRIMARY industry (Jake,
--              2026-10-21). Move the alumni whose CURRENT job has FP&A as the
--              primary industry over to "Corporate Finance", and keep FP&A as
--              their secondary industry so nothing is lost.
--
-- Drafted:   2026-10-21
-- Target:    PRODUCTION database (njobhhdopwdodvzosrns). Expect about 5 rows.
-- Author:    drafted by Claude from the fa-web-api models/migrations.
--            Dry-run on DEV only (tnnhhnzglyfqolxdojyb), inside a transaction
--            that was rolled back. Never run against prod by Claude.
-- Runner:    Jake, by hand (Supabase SQL editor or psql).
--
-- This file lives in database/maintenance/, which database/migrate.sh does NOT
-- glob (it only applies database/migrations/*.sql). CI will never run it.
--
-- WHAT IS "THE CURRENT JOB"
--   Table current_employment -- exactly one row per alum
--   (uq_current_employment_alumni_id). Columns used here:
--     current_employment_id, alumni_id, current_industry,
--     current_industry_secondary (varchar 255), updated_at.
--   Past jobs live in employment_history (employment_industry) and are NOT
--   touched by this script. Section 0 below only LISTS any history rows that
--   still say FP&A, for your information.
--
-- THE RULE (per row whose current industry is FP&A, case-insensitive, trimmed)
--   * primary   -> 'Corporate Finance'
--   * secondary empty / NULL           -> 'FP&A'
--   * secondary already FP&A           -> left as it is
--     (also when FP&A is already one item of a comma list, e.g.
--      'Consulting, FP&A' -- so it never gets doubled)
--   * secondary is something else      -> '<existing>, FP&A'  (both kept)
--
-- HOW TO RUN
--   1. Run SECTION 0 and SECTION 1 (read-only). Check the rows and the count.
--   2. Run SECTION 2 as one block. It opens a transaction, updates, and shows
--      the rows AFTER the change. Nothing is saved yet.
--   3. If the after-rows look right, run COMMIT;   If not, run ROLLBACK;
--   NOTE (Supabase SQL editor): the editor runs each "Run" as its own
--   transaction, so BEGIN/COMMIT across separate runs does not hold there.
--   Use psql for the check-then-commit step, or run Section 2 in the editor
--   with ROLLBACK first to look, then again with COMMIT.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- SECTION 0 (read-only, information only): past/history jobs that say FP&A.
-- These are NOT changed by this script. Listed so you know they exist.
-- ----------------------------------------------------------------------------
SELECT eh.employment_history_id,
       eh.alumni_id,
       a.first_name,
       a.last_name,
       eh.employer_name,
       eh.employment_industry,
       eh.is_current
FROM employment_history eh
JOIN alumni a ON a.alumni_id = eh.alumni_id
WHERE lower(btrim(eh.employment_industry)) = 'fp&a'
ORDER BY a.last_name, a.first_name, eh.employment_history_id;


-- ----------------------------------------------------------------------------
-- SECTION 1 (read-only): PREVIEW the current jobs that will change.
-- "secondary_after" shows what the secondary industry will become.
-- "secondary_too_long" must be false on every row (the column holds 255 chars).
-- ----------------------------------------------------------------------------
SELECT ce.current_employment_id,
       ce.alumni_id,
       a.first_name,
       a.last_name,
       ce.current_industry            AS current_industry_before,
       ce.current_industry_secondary  AS secondary_before,
       CASE
           WHEN ce.current_industry_secondary IS NULL
                OR btrim(ce.current_industry_secondary) = ''
               THEN 'FP&A'
           WHEN 'fp&a' = ANY (
                    SELECT lower(btrim(part))
                    FROM unnest(string_to_array(ce.current_industry_secondary, ',')) AS part)
               THEN ce.current_industry_secondary
           ELSE btrim(ce.current_industry_secondary) || ', FP&A'
       END                            AS secondary_after,
       length(btrim(coalesce(ce.current_industry_secondary, ''))) + 6 > 255
                                      AS secondary_too_long
FROM current_employment ce
JOIN alumni a ON a.alumni_id = ce.alumni_id
WHERE lower(btrim(ce.current_industry)) = 'fp&a'
ORDER BY a.last_name, a.first_name, ce.alumni_id;

-- How many rows will change (expect about 5 on prod).
SELECT count(*) AS rows_to_change
FROM current_employment
WHERE lower(btrim(current_industry)) = 'fp&a';


-- ----------------------------------------------------------------------------
-- SECTION 2: the change, inside a transaction. Run this whole block together.
-- ----------------------------------------------------------------------------
BEGIN;

-- Step 1: rewrite the current job only. The secondary is worked out from the
-- OLD row values (Postgres evaluates every SET expression against the row as
-- it was before the update), so the order of the two SET lines does not matter.
UPDATE current_employment
SET current_industry_secondary = CASE
        -- No secondary yet: FP&A becomes the secondary.
        WHEN current_industry_secondary IS NULL
             OR btrim(current_industry_secondary) = ''
            THEN 'FP&A'
        -- Secondary already has FP&A (alone or in a comma list): leave it.
        WHEN 'fp&a' = ANY (
                 SELECT lower(btrim(part))
                 FROM unnest(string_to_array(current_industry_secondary, ',')) AS part)
            THEN current_industry_secondary
        -- Secondary is something else: keep it and add FP&A after it.
        ELSE btrim(current_industry_secondary) || ', FP&A'
    END,
    current_industry = 'Corporate Finance',
    updated_at = now()
WHERE lower(btrim(current_industry)) = 'fp&a';
-- psql prints "UPDATE n" here -- n should match rows_to_change from Section 1.

-- Step 2: look at the result BEFORE saving. Lists every current job that now
-- has FP&A in its secondary and Corporate Finance as primary (this includes
-- any that were already like that before; the updated_at column tells them
-- apart -- the rows just changed show this transaction's time).
SELECT ce.current_employment_id,
       ce.alumni_id,
       a.first_name,
       a.last_name,
       ce.current_industry            AS current_industry_after,
       ce.current_industry_secondary  AS secondary_after,
       ce.updated_at
FROM current_employment ce
JOIN alumni a ON a.alumni_id = ce.alumni_id
WHERE ce.current_industry = 'Corporate Finance'
  AND ce.current_industry_secondary ILIKE '%fp&a%'
ORDER BY ce.updated_at DESC, a.last_name, a.first_name;

-- Step 3: there must be NO current job left with FP&A as primary. Expect 0.
SELECT count(*) AS fpa_primary_left
FROM current_employment
WHERE lower(btrim(current_industry)) = 'fp&a';

-- Step 4: decide.
--   Looks right?   run:  COMMIT;
--   Anything off?  run:  ROLLBACK;   (nothing is saved, the rows go back)
-- COMMIT;

-- =============================================================================
-- Migration: Not in the Labor Force by EMPLOYMENT STATUS (#681, follow-up to #673)
-- Date: 2026-11-17
-- -----------------------------------------------------------------------------
-- WHY. The 11-13 cleanup moved jobs whose COMPANY said "Not in the Labor Force"
-- -- and on prod none did, so nothing moved. Jake decided (2026-11-17) the rule
-- fires when the company OR the employment status is Not in the Labor Force,
-- so everyone in that status lands in one place: industry 'Other', secondary
-- 'Not in the Labor Force', status 'Not in the Labor Force'. The app applies
-- the same rule on every save (app/core/data_rules.py); this file catches the
-- records that already exist.
--
--   1. The alum's employment status: any spelling of Not in the Labor Force
--      becomes exactly 'Not in the Labor Force'.
--   2. Their CURRENT job (current_employment) gets industry 'Other' and secondary
--      'Not in the Labor Force'. The COMPANY IS NOT TOUCHED (a blank company
--      stays blank; #673 only rewrites a company that is itself a spelling of
--      Not in the Labor Force, and the 11-13 cleanup already did that).
--      This overrides #671's company-'Unknown' -> industry 'Unknown' for these
--      people, as the app now does.
--   3. An alum in that status with NO current job row gets one holding only
--      those two values (company blank), so the dashboard counts them.
--   Past jobs (employment_history) are not touched: status describes the person
--   now, not a past role.
--
-- Every changed value is copied to `data_cleanup_backup` (created by
-- 2026-11-13_cleanup_unknown_and_nilf.sql, lexically earlier) under cleanup
-- '681' first. A row this file INSERTS is recorded with column_name
-- '(row inserted)' and old_value NULL, so the undo can delete it. (The 11-13
-- file's ROLLBACK loops over the WHOLE backup table: if it is ever run, add
-- `WHERE cleanup <> '681'` to it, or undo this file first with the snippet below.)
-- alumni.updated_at is NOT bumped (it drives "Last updated <date> by <name>").
--
-- SAFE TO RE-RUN: every statement only touches rows not already in the target
-- state, so a second run changes and backs up nothing.
-- =============================================================================

BEGIN;

-- Same table as 11-13 (no-op if it exists); repeated so this file stands alone.
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

-- Session-only helpers, identical to the 11-13 migration's.
CREATE OR REPLACE FUNCTION pg_temp.fa_fold(v text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT lower(regexp_replace(btrim(v, E' \t\n\r\f' || chr(11) || chr(160)), '\s+', ' ', 'g'))
$$;

CREATE OR REPLACE FUNCTION pg_temp.fa_is_nlf(v text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT pg_temp.fa_fold(v) ~ '^not in (the )?labou?r[ -]?force$'
$$;

-- ----------------------------------------------------------------------------
-- 1. Employment status spelling -> 'Not in the Labor Force'.
-- ----------------------------------------------------------------------------
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '681', 'alumni', a.alumni_id, 'employment_status', a.employment_status, a.updated_at
  FROM alumni a
 WHERE pg_temp.fa_is_nlf(a.employment_status)
   AND a.employment_status <> 'Not in the Labor Force';

UPDATE alumni
   SET employment_status = 'Not in the Labor Force'
 WHERE pg_temp.fa_is_nlf(employment_status)
   AND employment_status <> 'Not in the Labor Force';

-- ----------------------------------------------------------------------------
-- 2. Their current job -> industry 'Other', secondary 'Not in the Labor Force'.
--    Only the two industry columns are backed up and changed.
-- ----------------------------------------------------------------------------
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '681', 'current_employment', ce.current_employment_id, v.col, v.val, ce.updated_at
  FROM current_employment ce
  JOIN alumni a ON a.alumni_id = ce.alumni_id
 CROSS JOIN LATERAL (VALUES
        ('current_industry', ce.current_industry, 'Other'),
        ('current_industry_secondary', ce.current_industry_secondary, 'Not in the Labor Force')
      ) AS v(col, val, target)
 WHERE a.employment_status = 'Not in the Labor Force'
   AND v.val IS DISTINCT FROM v.target;

UPDATE current_employment ce
   SET current_industry = 'Other',
       current_industry_secondary = 'Not in the Labor Force',
       updated_at = now()
  FROM alumni a
 WHERE a.alumni_id = ce.alumni_id
   AND a.employment_status = 'Not in the Labor Force'
   AND (ce.current_industry IS DISTINCT FROM 'Other'
        OR ce.current_industry_secondary IS DISTINCT FROM 'Not in the Labor Force');

-- ----------------------------------------------------------------------------
-- 3. In that status with no current job row: add one (company left blank).
--    current_employment has a unique index on alumni_id, so NOT EXISTS is the
--    whole guard.
-- ----------------------------------------------------------------------------
WITH inserted AS (
    INSERT INTO current_employment (alumni_id, current_industry, current_industry_secondary)
    SELECT a.alumni_id, 'Other', 'Not in the Labor Force'
      FROM alumni a
     WHERE a.employment_status = 'Not in the Labor Force'
       AND NOT EXISTS (SELECT 1 FROM current_employment ce WHERE ce.alumni_id = a.alumni_id)
    RETURNING current_employment_id
)
INSERT INTO data_cleanup_backup (cleanup, table_name, row_id, column_name, old_value, old_updated_at)
SELECT '681', 'current_employment', current_employment_id, '(row inserted)', NULL, NULL
  FROM inserted;

-- What changed (printed in the CI migrate log).
SELECT cleanup, table_name, column_name, count(*) AS values_changed
  FROM data_cleanup_backup
 WHERE cleanup = '681'
   AND backed_up_at = now()   -- rows from this transaction
 GROUP BY 1, 2, 3
 ORDER BY 1, 2, 3;

COMMIT;

-- =============================================================================
-- UNDO (by hand, only if this migration must be reversed). Deletes the job rows
-- it inserted, then restores every value it changed, newest first. Touches only
-- cleanup '681' rows, so the 11-13 backup is left alone.
--
-- BEGIN;
-- DELETE FROM current_employment ce
--  USING data_cleanup_backup b
--  WHERE b.cleanup = '681'
--    AND b.table_name = 'current_employment'
--    AND b.column_name = '(row inserted)'
--    AND ce.current_employment_id = b.row_id;
-- DO $$
-- DECLARE b record; pk text;
-- BEGIN
--   FOR b IN SELECT * FROM data_cleanup_backup
--             WHERE cleanup = '681' AND column_name <> '(row inserted)'
--             ORDER BY data_cleanup_backup_id DESC LOOP
--     pk := CASE b.table_name
--             WHEN 'alumni' THEN 'alumni_id'
--             WHEN 'current_employment' THEN 'current_employment_id' END;
--     EXECUTE format('UPDATE %I SET %I = $1 WHERE %I = $2', b.table_name, b.column_name, pk)
--       USING b.old_value, b.row_id;
--   END LOOP;
-- END $$;
-- DELETE FROM data_cleanup_backup WHERE cleanup = '681';
-- COMMIT;
-- =============================================================================

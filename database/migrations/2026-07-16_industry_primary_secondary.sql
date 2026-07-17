-- #282 — Industry vocabulary: primary/secondary split + alphabetize + reconcile.
--
-- Requested by Tanya 2026-07-16 while editing the 2025 cohort: move "Financial
-- Services" into alphabetical order and stop offering Law, Corporate Banking,
-- Sales and Trading and Credit Risk as an alumnus's PRIMARY industry. They are
-- NOT deleted -- they remain valid SECONDARY industries, so this migration only
-- reorders the vocabulary and folds existing PRIMARY uses into "Other".
--
-- Data-only (no DDL, no schema.sql change). Runs in one transaction.
--
-- Three parts:
--   1. Reconcile vocabulary_terms sort_order with app/core/dropdowns.py
--      INDUSTRIES (the two sources of truth had already drifted: "Financial
--      Services" was index 14 in the tuple but sort_order 19 in the DB).
--      tests/test_industry_vocab.py now fails CI if they drift again.
--   2. Fold the four non-primary industries out of current_industry into
--      current_industry_secondary, ONLY where secondary is empty.
--   3. Report the skipped conflict rows (secondary already populated) for Tanya
--      to reconcile by hand -- those records are left completely untouched.
--
-- The primary/secondary split itself is enforced in the APPLICATION layer
-- (app/core/dropdowns.py PRIMARY_INDUSTRIES, surfaced via
-- GET /vocabulary/industry?scope=primary). current_industry is a free-text
-- varchar with no FK to vocabulary_terms, which is also why every match below is
-- CASE-INSENSITIVE and trimmed: casing drifts through CSV imports.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Vocabulary order == app/core/dropdowns.py INDUSTRIES order.
--    sort_order = the tuple index; "Other" stays pinned last at 99.
--    Upsert (not UPDATE) so the set is guaranteed complete and the migration is
--    idempotent. DO UPDATE deliberately touches ONLY sort_order -- it must not
--    resurrect a term an admin has soft-deleted (active=false).
-- ---------------------------------------------------------------------------
INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('industry', 'Asset Management',     0),
    ('industry', 'Commercial Banking',   1),
    ('industry', 'Consulting',           2),
    ('industry', 'Corporate Banking',    3),
    ('industry', 'Corporate Finance',    4),
    ('industry', 'Credit Risk',          5),
    ('industry', 'Equity Research',      6),
    ('industry', 'Financial Services',   7),
    ('industry', 'FP&A',                 8),
    ('industry', 'Investment Banking',   9),
    ('industry', 'Law',                 10),
    ('industry', 'Private Banking',     11),
    ('industry', 'Private Credit',      12),
    ('industry', 'Private Equity',      13),
    ('industry', 'Real Estate',         14),
    ('industry', 'Sales',               15),
    ('industry', 'Sales and Trading',   16),
    ('industry', 'Valuation & Advisory', 17),
    ('industry', 'Venture Capital',     18),
    ('industry', 'Wealth Management',   19),
    ('industry', 'Other',               99)
ON CONFLICT (category, value) DO UPDATE
    SET sort_order = EXCLUDED.sort_order,
        updated_at = now();

-- ---------------------------------------------------------------------------
-- 2. Report BEFORE mutating: the conflict rows this migration will NOT touch.
--    Secondary is already populated, so folding the primary into it would
--    destroy real data. These are left exactly as-is for manual cleanup.
--    Re-runnable as a plain SELECT afterwards (see the note at the bottom).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    r        record;
    n_skip   integer := 0;
    n_move   integer := 0;
BEGIN
    FOR r IN
        SELECT a.alumni_id,
               a.first_name,
               a.last_name,
               a.net_id,
               ce.current_industry,
               ce.current_industry_secondary
        FROM current_employment ce
        JOIN alumni a ON a.alumni_id = ce.alumni_id
        WHERE lower(btrim(ce.current_industry)) IN
                  ('law', 'corporate banking', 'sales and trading', 'credit risk')
          AND ce.current_industry_secondary IS NOT NULL
          AND btrim(ce.current_industry_secondary) <> ''
        ORDER BY a.last_name, a.first_name, a.alumni_id
    LOOP
        n_skip := n_skip + 1;
        RAISE NOTICE
            '[#282 SKIPPED] alumni_id=% net_id=% name=% % primary=% secondary=% '
            '(secondary already populated -- left untouched, needs manual review)',
            r.alumni_id, coalesce(r.net_id, '-'), r.first_name, r.last_name,
            r.current_industry, r.current_industry_secondary;
    END LOOP;

    SELECT count(*) INTO n_move
    FROM current_employment ce
    WHERE lower(btrim(ce.current_industry)) IN
              ('law', 'corporate banking', 'sales and trading', 'credit risk')
      AND (ce.current_industry_secondary IS NULL
           OR btrim(ce.current_industry_secondary) = '');

    RAISE NOTICE '[#282] % row(s) will be folded into Other; % conflict row(s) skipped.',
        n_move, n_skip;
END $$;

-- ---------------------------------------------------------------------------
-- 3. The fold. Primary -> 'Other', old primary preserved in secondary using the
--    CANONICAL casing from INDUSTRIES (the stored value may be lower-cased or
--    otherwise drifted from an import).
--
--    These alumni ALREADY counted in the dashboard's "Other" slice (all four are
--    in _NON_WHEEL_INDUSTRIES), so rewriting the value to the literal 'Other'
--    does not move anyone between dashboard buckets.
--
--    The WHERE clause is the exact complement of the skip report above: rows
--    whose secondary is already populated are excluded and stay untouched.
-- ---------------------------------------------------------------------------
UPDATE current_employment
SET current_industry_secondary = CASE lower(btrim(current_industry))
        WHEN 'law'               THEN 'Law'
        WHEN 'corporate banking' THEN 'Corporate Banking'
        WHEN 'sales and trading' THEN 'Sales and Trading'
        WHEN 'credit risk'       THEN 'Credit Risk'
    END,
    current_industry = 'Other',
    updated_at = now()
WHERE lower(btrim(current_industry)) IN
          ('law', 'corporate banking', 'sales and trading', 'credit risk')
  AND (current_industry_secondary IS NULL
       OR btrim(current_industry_secondary) = '');

COMMIT;

-- ---------------------------------------------------------------------------
-- Retrieving the skipped conflict rows for Tanya (safe, read-only, re-runnable
-- after the migration -- the skipped rows are exactly those that still hold one
-- of the four as their primary industry):
--
--   SELECT a.alumni_id, a.net_id, a.first_name, a.last_name,
--          ce.current_industry AS primary_industry,
--          ce.current_industry_secondary AS secondary_industry
--   FROM current_employment ce
--   JOIN alumni a ON a.alumni_id = ce.alumni_id
--   WHERE lower(btrim(ce.current_industry)) IN
--             ('law', 'corporate banking', 'sales and trading', 'credit risk')
--   ORDER BY a.last_name, a.first_name;
--
-- (Run migrate.sh without -q, or with psql's NOTICE output visible, to capture
-- the same list from the migration run itself.)
-- ---------------------------------------------------------------------------

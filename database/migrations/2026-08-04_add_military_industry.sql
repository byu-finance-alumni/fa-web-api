-- #608 — Add "Military" as an industry option (category 'industry').
--
-- Jake, 2026-08-04: "for the military make sure someone can be in the military
-- and have a job." Nothing ever blocked a Military alumnus from holding an
-- employer/title (verified end to end on app #608) — the gap was that there was
-- no INDUSTRY that fits military service. A service member had to be recorded as
-- "Other" or "Unknown", which is precisely what made them vanish from the
-- dashboard's industry breakdown.
--
-- It is added to the backend INDUSTRIES tuple (app/core/dropdowns.py) as a
-- NON-WHEEL industry (i.e. IN _NON_WHEEL_INDUSTRIES) — it is not one of Tanya's
-- 15 finance industries. Jake chose to keep the dashboard industry chart about
-- FINANCE SECTORS, so unlike "Graduate Student" (#294) it gets NO bar of its own
-- and simply folds into the "Other" catch-all, exactly like Law or FP&A. That is
-- the default behaviour of _NON_WHEEL_INDUSTRIES, so there is deliberately no
-- special case for it in the breakdown code.
--
-- It is valid as EITHER the primary or the secondary industry (NOT in
-- _PRIMARY_EXCLUDED_INDUSTRIES). Jake's reservist case is why: someone can serve
-- AND hold a civilian job — primary = Investment Banking, secondary = Military.
-- employment_status cannot express that (it is a single value); industry has two
-- slots, which is the whole reason Military lives here as well as there.
--
-- Consequently a ?industry=Military search matches BOTH slots, unlike every
-- other industry, which stays primary-only per the 2026-08-03 decision (#584).
-- That widening is Military-only and is pinned by tests/test_alumni_search.py.
--
-- ORDERING. Unlike "Graduate Student"/"Unknown"/"Other" (pinned at 98/97/99),
-- "Military" belongs in the ALPHABETICAL BODY, between "Law" and "Private
-- Banking" — someone scanning the dropdown looks for it under M. The body's
-- contract (tests/test_industry_vocab.py) is sort_order == the INDUSTRIES tuple
-- index, so inserting at index 11 shifts "Private Banking".."Wealth Management"
-- from 11..19 to 12..20. This migration therefore re-upserts the WHOLE body
-- (mirroring 2026-07-16_industry_primary_secondary.sql) instead of appending one
-- row, so the DB order cannot drift from the tuple.
--
-- Data-only (no DDL, no schema.sql change). Idempotent: upsert on the
-- (category, value) unique constraint, and DO UPDATE deliberately touches ONLY
-- sort_order so it can never resurrect a term an admin has soft-deleted
-- (active=false). The three pinned tail options are left alone.
BEGIN;

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
    ('industry', 'Military',            11),
    ('industry', 'Private Banking',     12),
    ('industry', 'Private Credit',      13),
    ('industry', 'Private Equity',      14),
    ('industry', 'Real Estate',         15),
    ('industry', 'Sales',               16),
    ('industry', 'Sales and Trading',   17),
    ('industry', 'Valuation & Advisory', 18),
    ('industry', 'Venture Capital',     19),
    ('industry', 'Wealth Management',   20)
ON CONFLICT (category, value) DO UPDATE
    SET sort_order = EXCLUDED.sort_order,
        updated_at = now();

COMMIT;

-- NOTE. No data backfill. Alumni currently recorded as "Other"/"Unknown" who are
-- actually serving are NOT reassigned: nothing in the database identifies them
-- (employment_status = 'Military' is a separate, independently-entered field and
-- rewriting industry from it would be a guess). Reclassification is Tanya's call
-- on real records, matching the casing-only rule Jake set for the #568
-- employment-status cleanup. To find candidates for a manual review:
--
--   SELECT a.alumni_id, a.net_id, a.first_name, a.last_name,
--          a.employment_status, ce.current_industry
--   FROM alumni a
--   JOIN current_employment ce ON ce.alumni_id = a.alumni_id
--   WHERE lower(btrim(a.employment_status)) = 'military'
--     AND lower(btrim(coalesce(ce.current_industry, ''))) IN ('', 'other', 'unknown')
--   ORDER BY a.last_name, a.first_name;

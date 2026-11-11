-- api#673 — Add "Not in the Labor Force" as a SECONDARY industry option
-- (category 'industry').
--
-- Jake, 2026-11-11: a job whose company is listed as "Not in the Labor Force"
-- is stored as industry "Other" with secondary industry "Not in the Labor
-- Force" (and the alum's employment status "Not in the Labor Force"). The
-- secondary-industry dropdown needs the value as an option or every such record
-- would show an off-list secondary.
--
-- SECONDARY ONLY. It is added to the backend INDUSTRIES tuple
-- (app/core/dropdowns.py) and to _PRIMARY_EXCLUDED_INDUSTRIES, so
-- `GET /vocabulary/industry?scope=primary` hides it from the PRIMARY dropdown,
-- exactly like Law / FP&A. It is also a NON-WHEEL industry: on the dashboard it
-- folds into "Other" (no bar of its own).
--
-- sort_order 96 pins it at the head of the pinned tail, immediately BEFORE
-- "Unknown" (97), "Graduate Student" (98) and "Other" (99): like Graduate
-- Student it is a status rather than a finance sector. The alphabetical body is
-- untouched, so nothing else needs re-sorting.
--
-- Data-only (no DDL, no schema.sql change). Idempotent via the (category, value)
-- unique constraint; DO NOTHING so it never resurrects a term an admin has
-- soft-deleted.
--
-- The existing-record changes (#671/#672/#673) are NOT here: they are hand-run
-- maintenance SQL in database/maintenance/ (672-, 671-, 673-).
BEGIN;

INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('industry', 'Not in the Labor Force', 96)
ON CONFLICT (category, value) DO NOTHING;

COMMIT;

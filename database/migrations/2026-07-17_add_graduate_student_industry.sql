-- #294 — Add "Graduate Student" as an industry option (category 'industry').
--
-- So alumni currently in graduate school stop landing in the "Other" catch-all,
-- add "Graduate Student" as a selectable current industry. It is ALSO added to
-- the backend INDUSTRIES tuple (app/core/dropdowns.py) as a NON-WHEEL industry
-- (i.e. IN _NON_WHEEL_INDUSTRIES), so it does NOT create a new dashboard wheel
-- slice — instead the frontend dashboard renders it as its own clickable
-- indicator at the BOTTOM of the industry breakdown, linking to the alumni list
-- filtered to current_industry = 'Graduate Student'.
--
-- It stays a valid PRIMARY industry (NOT in _PRIMARY_EXCLUDED_INDUSTRIES), so it
-- can be picked as an alumnus's current industry and the controlled-vocab write
-- validation accepts it on profile edits/imports.
--
-- sort_order 98 pins it in the dropdown immediately BEFORE the "Other" catch-all
-- (99), matching the pinned-tail convention (both are held out of the otherwise
-- alphabetical order). Data-only (no DDL, no schema.sql change). Idempotent via
-- the (category, value) unique constraint.
BEGIN;

INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('industry', 'Graduate Student', 98)
ON CONFLICT (category, value) DO NOTHING;

COMMIT;

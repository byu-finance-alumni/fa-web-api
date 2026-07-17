-- #295 — Add "Unknown" as an industry option (category 'industry').
--
-- "Unknown" is DISTINCT from a blank/unset current industry. Blank means "not
-- yet collected"; "Unknown" means "we checked and it is genuinely unknown". Add
-- it as a selectable current industry so that state can be recorded explicitly.
-- It is ALSO added to the backend INDUSTRIES tuple (app/core/dropdowns.py) as a
-- NON-WHEEL industry (i.e. IN _NON_WHEEL_INDUSTRIES), so it does NOT create a new
-- dashboard wheel slice — it simply folds into the "Other" catch-all on the
-- wheel. Unlike "Graduate Student" it gets NO separate dashboard indicator.
--
-- It stays a valid PRIMARY industry (NOT in _PRIMARY_EXCLUDED_INDUSTRIES), so it
-- can be picked as an alumnus's current industry and the controlled-vocab write
-- validation accepts it on profile edits/imports.
--
-- sort_order 97 pins it in the dropdown immediately BEFORE "Graduate Student"
-- (98) and the "Other" catch-all (99), matching the pinned-tail convention (all
-- three are held out of the otherwise alphabetical order). Data-only (no DDL, no
-- schema.sql change). Idempotent via the (category, value) unique constraint.
BEGIN;

INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('industry', 'Unknown', 97)
ON CONFLICT (category, value) DO NOTHING;

COMMIT;

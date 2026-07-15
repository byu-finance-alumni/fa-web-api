-- Add "Financial Services" as an industry option (category 'industry').
-- Requested at the 2026-07-14 stakeholder meeting: "Financial Services" should
-- be its own bar on the dashboard industry breakdown. It is ALSO added to the
-- backend INDUSTRIES tuple (app/core/dropdowns.py) as a WHEEL industry (i.e. NOT
-- in _NON_WHEEL_INDUSTRIES), so it renders as its own dashboard slice/bar and
-- the controlled-vocab write validation accepts it on profile edits/imports.
-- Data-only (no DDL, no schema.sql change). sort_order 19 places it after the
-- existing industries and before the "Other" catch-all (99). Idempotent via the
-- (category, value) unique constraint.
BEGIN;

INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('industry', 'Financial Services', 19)
ON CONFLICT (category, value) DO NOTHING;

COMMIT;

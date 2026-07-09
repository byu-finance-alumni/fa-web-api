-- Add new industry options to the vocabulary dropdown (category 'industry').
-- These are ALSO added to the backend INDUSTRIES tuple (app/core/dropdowns.py)
-- so the controlled-vocab write validation accepts them. Data-only (no DDL, no
-- schema.sql change). Appended after the existing 0-13 industries and before
-- the "Other" catch-all (sort_order 99). Idempotent via the (category, value)
-- unique constraint.
BEGIN;

INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('industry', 'Law',               14),
    ('industry', 'Corporate Banking', 15),
    ('industry', 'FP&A',              16),
    ('industry', 'Sales and Trading', 17),
    ('industry', 'Credit Risk',       18)
ON CONFLICT (category, value) DO NOTHING;

COMMIT;

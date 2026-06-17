-- =============================================================================
-- Editable controlled-vocabulary store (#82).
--
-- A single generic lookup table so engineer/super_admin can add, rename, and
-- deactivate dropdown values at runtime (no code deploy) for the categories that
-- were previously hardcoded constants or free text: industry, event_type,
-- attendance_status, interaction_type. (tags and status_labels keep their own
-- existing tables and join semantics — managing those is a separate follow-up.)
--
-- Deletes are SOFT (active=false) so a value still referenced by existing records
-- stays valid and historical; it is just hidden from new-entry dropdowns.
--
-- New table => must get deny-all RLS to match database/rls_lockdown.sql (the
-- backend bypasses RLS via its privileged role; the Supabase anon/auth keys must
-- never reach it). Idempotent: IF NOT EXISTS + ON CONFLICT.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS vocabulary_terms (
    term_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category    varchar(50) NOT NULL,
    value       varchar(100) NOT NULL,
    sort_order  integer NOT NULL DEFAULT 0,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_vocabulary_terms_category_value UNIQUE (category, value)
);

-- Fast "active options for a category" reads (the dropdown query).
CREATE INDEX IF NOT EXISTS ix_vocabulary_terms_category_active
    ON vocabulary_terms (category, active);

ALTER TABLE vocabulary_terms ENABLE ROW LEVEL SECURITY;

-- --- Seed: industries (mirrors app/core/dropdowns.py INDUSTRIES) -------------
INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('industry', 'Asset Management', 0),
    ('industry', 'Commercial Banking', 1),
    ('industry', 'Consulting', 2),
    ('industry', 'Corporate Finance', 3),
    ('industry', 'Equity Research', 4),
    ('industry', 'Investment Banking', 5),
    ('industry', 'Private Banking', 6),
    ('industry', 'Private Credit', 7),
    ('industry', 'Private Equity', 8),
    ('industry', 'Real Estate', 9),
    ('industry', 'Sales', 10),
    ('industry', 'Valuation & Advisory', 11),
    ('industry', 'Venture Capital', 12),
    ('industry', 'Wealth Management', 13),
    ('industry', 'Other', 99)
ON CONFLICT (category, value) DO NOTHING;

-- --- Seed: attendance statuses ----------------------------------------------
INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('attendance_status', 'Registered', 0),
    ('attendance_status', 'Attended', 1),
    ('attendance_status', 'No Show', 2),
    ('attendance_status', 'Cancelled', 3)
ON CONFLICT (category, value) DO NOTHING;

-- --- Seed: interaction types ------------------------------------------------
INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('interaction_type', 'Phone Call', 0),
    ('interaction_type', 'Meeting', 1),
    ('interaction_type', 'Networking', 2),
    ('interaction_type', 'Event Follow-Up', 3),
    ('interaction_type', 'Recruiting Discussion', 4),
    ('interaction_type', 'General Outreach', 5)
ON CONFLICT (category, value) DO NOTHING;

-- --- Seed: event types -------------------------------------------------------
-- A sensible default set...
INSERT INTO vocabulary_terms (category, value, sort_order) VALUES
    ('event_type', 'Networking', 0),
    ('event_type', 'Recruiting', 1),
    ('event_type', 'Career Fair', 2),
    ('event_type', 'Speaker Event', 3),
    ('event_type', 'Workshop', 4),
    ('event_type', 'Conference', 5),
    ('event_type', 'Social', 6),
    ('event_type', 'Other', 99)
ON CONFLICT (category, value) DO NOTHING;

-- ...plus any event_type values already present in the data, so existing events
-- keep a managed term (sorted after the defaults). NULL/blank are skipped.
INSERT INTO vocabulary_terms (category, value, sort_order)
SELECT DISTINCT 'event_type', trim(event_type), 50
FROM events
WHERE event_type IS NOT NULL AND trim(event_type) <> ''
ON CONFLICT (category, value) DO NOTHING;

COMMIT;

-- Engineer / super-admin-managed dashboard quick-filter presets shown on the
-- dashboard's Quick search tab. Each row is a label + a relative in-app deep
-- link (href) into a pre-filtered list. Admins curate the list directly (no
-- active flag); seeded once with a few common compound searches that they can
-- then edit / reorder / remove.
BEGIN;

CREATE TABLE IF NOT EXISTS dashboard_presets (
    dashboard_preset_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label               varchar(200) NOT NULL,
    href                varchar(500) NOT NULL,
    sort_order          integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Deny-all RLS like every other public table (Supabase auto-exposes the public
-- schema via its Data API; the backend bypasses RLS with a privileged role).
-- Mirrors database/rls_lockdown.sql. Idempotent.
ALTER TABLE dashboard_presets ENABLE ROW LEVEL SECURITY;

-- One-time seed (only when the table is empty, so a re-run never duplicates).
INSERT INTO dashboard_presets (label, href, sort_order)
SELECT v.label, v.href, v.sort_order
FROM (VALUES
    ('Recent grads near Provo in Investment Banking',
     '/alumni?ymin=2022&ymax=2026&city=Provo&industry=Investment%20Banking', 1),
    ('CFAs near Salt Lake City',
     '/alumni?cfa=1&city=Salt%20Lake%20City', 2),
    ('Mentors in Private Equity',
     '/alumni?mentor=1&industry=Private%20Equity', 3),
    ('CPAs in California',
     '/alumni?cpa=1&state=CA', 4),
    ('Recent grads in Utah',
     '/alumni?ymin=2022&ymax=2026&state=UT', 5)
) AS v(label, href, sort_order)
WHERE NOT EXISTS (SELECT 1 FROM dashboard_presets);

COMMIT;

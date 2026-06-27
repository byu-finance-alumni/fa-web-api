-- Editable permission config (#164). Each row grants one capability to one
-- role; the PRESENCE of a row means the role holds that capability. The
-- capability codes themselves are defined in code (app/core/capabilities.py) —
-- the engineer toggles which roles hold which via the permission editor.
--
-- Seeds the historical hardcoded guard mapping so authorization is unchanged on
-- day one:
--   engineer    -> every capability (and always, via a code-level override)
--   super_admin -> view, alumni.edit, alumni.full, user_admin
--   full_access -> view, alumni.edit, alumni.full
--   student     -> view, alumni.edit
--   view_only   -> view
BEGIN;

CREATE TABLE IF NOT EXISTS role_capabilities (
    role_capability_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role_id             bigint NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    capability_code     varchar(100) NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_role_capabilities UNIQUE (role_id, capability_code)
);

CREATE INDEX IF NOT EXISTS ix_role_capabilities_role_id
    ON role_capabilities (role_id);

-- Deny-all RLS like every other public table (the backend bypasses RLS with a
-- privileged role; Supabase auto-exposes the public schema otherwise). Mirrors
-- database/rls_lockdown.sql. Idempotent.
ALTER TABLE role_capabilities ENABLE ROW LEVEL SECURITY;

-- One-time seed of the default grants (only when the table is empty, so a re-run
-- never duplicates). Joins by role_name so it picks up whatever role_ids exist.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, g.capability_code
FROM roles r
JOIN (VALUES
    ('engineer',    'view'),
    ('engineer',    'alumni.edit'),
    ('engineer',    'alumni.full'),
    ('engineer',    'user_admin'),
    ('engineer',    'vocab_admin'),
    ('engineer',    'engineer'),
    ('super_admin', 'view'),
    ('super_admin', 'alumni.edit'),
    ('super_admin', 'alumni.full'),
    ('super_admin', 'user_admin'),
    ('full_access', 'view'),
    ('full_access', 'alumni.edit'),
    ('full_access', 'alumni.full'),
    ('student',     'view'),
    ('student',     'alumni.edit'),
    ('view_only',   'view')
) AS g(role_name, capability_code) ON g.role_name = r.role_name
WHERE NOT EXISTS (SELECT 1 FROM role_capabilities);

COMMIT;

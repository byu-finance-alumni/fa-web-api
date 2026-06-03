-- =============================================================================
-- Seed the two canonical roles.
--
-- Data-only (no DDL), so no schema.sql change and no RLS step is needed — the
-- `roles` table and its lockdown already exist. Idempotent via ON CONFLICT on
-- the unique role_name. These role_name values are the contract referenced by
-- app/core/roles.py (RoleName).
-- =============================================================================

BEGIN;

INSERT INTO roles (role_name, role_description) VALUES
    ('super_admin', 'Super admin: everything full_access can do, plus create user accounts, assign roles, and issue temporary one-time passwords.'),
    ('full_access', 'Full access: create, update, archive, import, export, manage events, upload attachments, merge duplicates.'),
    ('view_only',   'View only: read-only access to alumni and related data.')
ON CONFLICT (role_name) DO NOTHING;

COMMIT;

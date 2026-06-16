-- =============================================================================
-- Seed the two new canonical roles: `engineer` and `student`.
--
-- `engineer` is the top role (everything super_admin can do, plus database /
-- controlled-vocabulary administration). `student` is a narrow writer that may
-- edit EXISTING alumni records and their nested data, but may NOT create new
-- alumni, archive/restore, import, or administer users.
--
-- Data-only (no DDL), so no schema.sql change and no RLS step is needed — the
-- `roles` table and its lockdown already exist. Idempotent via ON CONFLICT on
-- the unique role_name. These role_name values are the contract referenced by
-- app/core/roles.py (RoleName). The `view_only` role is unchanged here; it is
-- only relabelled "Professor" in the UI, the machine id stays `view_only`.
-- =============================================================================

BEGIN;

INSERT INTO roles (role_name, role_description) VALUES
    ('engineer', 'Engineer: top role — everything super_admin can do, plus database and controlled-vocabulary administration (editable dropdowns).'),
    ('student',  'Student: may edit existing alumni records and their nested data (employment, education, leadership, interactions, tags, status labels, tasks, event attendance). Cannot create new alumni, archive/restore, import, or administer users.')
ON CONFLICT (role_name) DO NOTHING;

COMMIT;

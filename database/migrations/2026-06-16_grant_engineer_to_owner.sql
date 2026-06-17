-- =============================================================================
-- Provision the project owner (gunnjake@byu.edu) as `engineer` (#81).
--
-- The `engineer` role is the top of the hierarchy and, by design, can only be
-- granted by another engineer (app/api/routes/admin.py assign_role enforces the
-- ceiling). So the FIRST engineer cannot be created through the API — it must be
-- seeded directly here. This migration bootstraps that single owner account.
--
-- Self-contained + idempotent:
--   * ensures the `engineer` role row exists (also seeded in
--     2026-06-16_seed_student_engineer_roles.sql) so this runs regardless of
--     migration ordering;
--   * grants engineer to the user whose email is gunnjake@byu.edu, matched
--     case-insensitively; ON CONFLICT keeps it a no-op on re-run;
--   * if that account does not exist yet (e.g. dev hasn't provisioned it), the
--     INSERT...SELECT simply affects zero rows — safe, re-runnable once the
--     account exists.
--
-- Data-only (no DDL / RLS). Applies to dev by hand and rides to prod on the next
-- promotion, so the owner is engineer in every environment.
-- =============================================================================

BEGIN;

INSERT INTO roles (role_name, role_description) VALUES
    ('engineer', 'Engineer: top role — everything super_admin can do, plus database and controlled-vocabulary administration (editable dropdowns).')
ON CONFLICT (role_name) DO NOTHING;

INSERT INTO user_roles (user_id, role_id)
SELECT u.user_id, r.role_id
FROM users u
CROSS JOIN roles r
WHERE lower(u.email) = 'gunnjake@byu.edu'
  AND r.role_name = 'engineer'
ON CONFLICT (user_id, role_id) DO NOTHING;

COMMIT;

-- Profile-completeness capability (#164 registry addition).
-- Registers the default grant for the new "profile.completeness" capability so
-- the per-alumnus completeness tab is visible to super_admin (engineer holds
-- every capability via the runtime hard-override, but seed an explicit row so
-- the permission-editor matrix reflects it). Additive + idempotent; the
-- engineer can toggle this for any role from the permission editor afterward.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT role_id, 'profile.completeness'
FROM roles
WHERE role_name IN ('super_admin', 'engineer')
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

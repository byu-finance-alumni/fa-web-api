-- Dedicated donations.manage capability (#189).
-- Splits donation-ledger WRITE/IMPORT authorization out of the user_admin
-- capability so that delegating user administration (Admin -> Users/Audit) does
-- NOT silently also grant donation-ledger writes/imports. Seeds the default grant
-- to EXACTLY the roles that held user_admin before the split (super_admin +
-- engineer), so authorization is unchanged on day one. Additive + idempotent; the
-- engineer can toggle this for any role from the permission editor afterward.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT role_id, 'donations.manage'
FROM roles
WHERE role_name IN ('super_admin', 'engineer')
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

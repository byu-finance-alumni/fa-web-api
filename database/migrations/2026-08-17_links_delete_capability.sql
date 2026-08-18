-- =============================================================================
-- Migration: the `links.delete` capability
-- Date: 2026-08-17  (fa-web-api #441 follow-up)
-- -----------------------------------------------------------------------------
-- Staff can now multi-select opportunity links in the Links tab and delete them.
-- Deletion gets its OWN capability rather than riding on `surveys.manage`:
--
--   * `surveys.manage` keeps approve / reject / add / edit. Rejecting a link
--     takes it out of circulation and is REVERSIBLE — the row survives, and that
--     row is the record that we once saw the thing.
--   * `links.delete` is permanent. Once the row is gone the only thing left is
--     the audit snapshot taken immediately before the delete.
--
-- Different levels of trust, so a different toggle. It gates BOTH delete routes
-- (DELETE /opportunity-links/{id} and POST /opportunity-links/bulk-delete), so
-- there is one answer to "who can erase a link" instead of two that can drift.
--
-- SEED = super_admin ONLY, plus the explicit engineer row below. This is
-- deliberately NARROWER than the capability it was carved out of: full_access
-- holds `surveys.manage` and keeps it, but does NOT get `links.delete`. So this
-- migration is additive in rows and is NOT behaviour-preserving in one direction
-- — a full_access user who could delete a link yesterday cannot today. That is
-- the requested change, not an accident, and it is the only access that moves.
--
-- Unlike the #379 seeds, the grant is hardcoded rather than derived from who
-- currently holds `surveys.manage`: deriving it would hand deletion to exactly
-- the roles the split is meant to exclude.
--
-- Data-only and idempotent: no DDL, ON CONFLICT DO NOTHING, and nothing is ever
-- REVOKED here, so re-running is a no-op and it cannot lock anyone out.
--
-- ORDERING NOTE. `load_grants` falls back to the in-code DEFAULT_GRANTS only
-- when `role_capabilities` is EMPTY, and dev and prod both have rows — so on a
-- real database `links.delete` is denied to everyone until this file has run.
-- The engineer is never affected (the runtime hard-override in
-- `effective_capabilities` grants them every capability regardless of this
-- table), so the gap degrades to "engineer-only deletion" rather than to a
-- lockout. Apply it with — or before — the API deploy.
--
-- EXISTS GUARD. On a BRAND-NEW database (empty table) both statements are
-- no-ops, so the table stays empty and `load_grants` keeps using DEFAULT_GRANTS
-- — which already contains `links.delete` for super_admin. Without the guard,
-- inserting here would make the table non-empty and switch the fallback off,
-- stripping every other capability from every role. That is the exact failure
-- mode this project has hit before; see 2026-08-04_permission_capability_split.
-- =============================================================================

BEGIN;

-- 1. Super Admin. The one non-engineer role the owner asked for.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'links.delete'
FROM roles r
WHERE r.role_name = 'super_admin'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 2. The explicit engineer row. The engineer already holds everything via the
--    runtime hard-override, but the row keeps the permission matrix honest and
--    matches every earlier capability migration
--    (2026-08-04_event_capabilities, 2026-08-04_permission_capability_split).
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'links.delete'
FROM roles r
WHERE r.role_name = 'engineer'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

COMMIT;

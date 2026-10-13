-- =============================================================================
-- Migration: the `surveys.progress` capability + students may export
-- Date: 2026-10-13  (fa-web-api #605)
-- -----------------------------------------------------------------------------
-- Two grants, both additive:
--
--   1. `surveys.progress` ("View survey progress") — the survey Progress tab as
--      a READ: the per-year schedule list and the "who replied" hover. It
--      grants no send / pause / cancel / review power; every mutating survey
--      route stays on `surveys.manage`. Seeded to every role that holds
--      `surveys.manage` TODAY (derived from the live rows, so an engineer's
--      earlier edits to who manages surveys carry over) plus `student`.
--
--   2. `alumni.export` to `student` — Jake's decision (2026-10-13): all
--      students may export. This is the one deliberate widening of an
--      existing capability here; the API declares it in
--      POST_SPLIT_ROLE_WIDENINGS so the #379 preservation test still holds.
--
-- Data-only and idempotent: no DDL, ON CONFLICT DO NOTHING, and nothing is ever
-- REVOKED here, so re-running is a no-op and it cannot lock anyone out.
--
-- ORDERING NOTE. `load_grants` falls back to the in-code DEFAULT_GRANTS only
-- when `role_capabilities` is EMPTY, and dev and prod both have rows — so on a
-- real database nobody holds `surveys.progress` until this file has run. The
-- API's progress guard also admits `surveys.manage`, so survey managers keep
-- the Progress tab through that gap; only students wait for this file. The
-- engineer is never affected (runtime hard-override). Apply it with — or
-- before — the API deploy.
--
-- EXISTS GUARD. On a BRAND-NEW database (empty table) every statement is a
-- no-op, so the table stays empty and `load_grants` keeps using DEFAULT_GRANTS
-- — which already contains both grants. Without the guard, inserting here would
-- make the table non-empty and switch the fallback off, stripping every other
-- capability from every role. That is the exact failure mode this project has
-- hit before; see 2026-08-04_permission_capability_split and
-- 2026-08-17_links_delete_capability.
-- =============================================================================

BEGIN;

-- 1. `surveys.progress` for every role that currently manages surveys. A
--    holder of `surveys.manage` can already see progress (the API guard
--    accepts either), so this changes nothing for them; it keeps the
--    permission matrix honest about who can see the tab.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT DISTINCT rc.role_id, 'surveys.progress'
FROM role_capabilities rc
WHERE rc.capability_code = 'surveys.manage'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 2. `surveys.progress` for student — the role this capability exists for:
--    watch a campaign without being able to run one.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'surveys.progress'
FROM roles r
WHERE r.role_name = 'student'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 3. The explicit engineer row. The engineer already holds everything via the
--    runtime hard-override, but the row keeps the permission matrix honest and
--    matches every earlier capability migration. (Statement 1 usually covers
--    it already; ON CONFLICT makes the overlap harmless.)
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'surveys.progress'
FROM roles r
WHERE r.role_name = 'engineer'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 4. `alumni.export` for student (Jake, 2026-10-13).
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'alumni.export'
FROM roles r
WHERE r.role_name = 'student'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

COMMIT;

-- =============================================================================
-- Migration: the `notes.create` capability (students may add profile notes)
-- Date: 2026-11-03
-- -----------------------------------------------------------------------------
-- Finance Society student leaders log in as `student` and need to leave notes on
-- alumni profiles, so one club can see that another already reached out.
-- Jake's rule: students may ADD a note on any alumni profile, and may EDIT or
-- DELETE ONLY THE NOTES THEY WROTE. Roles holding `notes.manage` keep full
-- add / edit / delete-any.
--
-- `notes.create` ("Add notes") is that narrower power. The API guard on every
-- note write accepts `notes.create` OR `notes.manage`; editing or deleting a
-- note somebody else wrote still needs `notes.manage`. Holding only
-- `notes.create` also limits NEW notes to alumni profiles (not interaction or
-- event notes).
--
-- Seeded to:
--   1. every role that holds `notes.manage` TODAY (derived from the live rows,
--      so an engineer's earlier edits carry over). `notes.manage` already admits
--      every note write, so this changes nothing for them; it keeps the
--      permission matrix honest about who can add notes.
--   2. `student` — the role this capability exists for.
--   3. the explicit engineer row (the engineer already holds everything via the
--      runtime hard-override; the row matches every earlier capability
--      migration).
-- `view_only` is deliberately NOT granted.
--
-- Data-only and idempotent: no DDL, ON CONFLICT DO NOTHING, and nothing is ever
-- REVOKED here, so re-running is a no-op and it cannot lock anyone out.
--
-- ORDERING NOTE. `load_grants` falls back to the in-code DEFAULT_GRANTS only
-- when `role_capabilities` is EMPTY, and dev and prod both have rows — so on a
-- real database nobody holds `notes.create` until this file has run. Before it
-- runs, students simply stay unable to write notes (today's behaviour) and
-- note managers are unaffected (the guard also accepts `notes.manage`). Old code
-- ignores the new row. Safe in either order; ship it first anyway.
--
-- EXISTS GUARD. On a BRAND-NEW database (empty table) every statement is a
-- no-op, so the table stays empty and `load_grants` keeps using DEFAULT_GRANTS
-- — which already contains these grants. Without the guard, inserting here
-- would make the table non-empty and switch the fallback off, stripping every
-- other capability from every role. See 2026-08-04_permission_capability_split
-- and 2026-08-17_links_delete_capability.
-- =============================================================================

BEGIN;

-- 1. Every role that currently manages notes.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT DISTINCT rc.role_id, 'notes.create'
FROM role_capabilities rc
WHERE rc.capability_code = 'notes.manage'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 2. Student (Jake, 2026-11-03): add notes, edit/delete own.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'notes.create'
FROM roles r
WHERE r.role_name = 'student'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 3. The explicit engineer row (statement 1 usually covers it already; ON
--    CONFLICT makes the overlap harmless).
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'notes.create'
FROM roles r
WHERE r.role_name = 'engineer'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

COMMIT;

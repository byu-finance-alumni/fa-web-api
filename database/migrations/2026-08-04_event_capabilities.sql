-- =============================================================================
-- Migration: event authoring capabilities (events.create / events.import)
-- Date: 2026-08-04  (fa-web-api #378)
-- -----------------------------------------------------------------------------
-- Splits event AUTHORING out of the blanket `alumni.full` capability into two
-- separately grantable codes, so the engineer can widen (or narrow) who may add
-- events from the permission editor without also handing over alumni
-- create/archive/import:
--
--   * `events.create` — create a single event by hand (POST /events).
--   * `events.import` — bulk upload: the CSV template, the dry-run preview, and
--     the commit (GET /events/import/template, POST /events/import/preview,
--     POST /events/import).
--
-- TWO codes, not one, on purpose: a single hand-entered event is one row, while
-- one bad CSV creates an event plus its entire attendee roster in a single shot.
--
-- SEED = EXACTLY the roles that already held `alumni.full` (full_access +
-- super_admin), so authorization is UNCHANGED on day one. `engineer` is seeded
-- too — it holds every capability via the runtime hard-override in
-- `effective_capabilities`, but an explicit row keeps the permission-editor
-- matrix honest and matches the earlier capability migrations
-- (2026-06-30_profile_completeness_capability, 2026-07-06_donations_manage_capability).
--
-- Data-only and idempotent: no DDL, and ON CONFLICT DO NOTHING means re-running
-- is a no-op. It never REVOKES anything, so it cannot lock anyone out.
--
-- ORDERING NOTE: `load_grants` falls back to the in-code DEFAULT_GRANTS only
-- when `role_capabilities` is EMPTY. On a database that already has grant rows,
-- the new codes exist for a role only once this migration has run — so apply it
-- before/with the API deploy, or full_access/super_admin will 403 on event
-- create and bulk upload in the gap. (The engineer is never affected: the
-- hard-override grants them everything regardless of this table.)
-- =============================================================================

BEGIN;

INSERT INTO role_capabilities (role_id, capability_code)
SELECT role_id, 'events.create'
FROM roles
WHERE role_name IN ('full_access', 'super_admin', 'engineer')
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

INSERT INTO role_capabilities (role_id, capability_code)
SELECT role_id, 'events.import'
FROM roles
WHERE role_name IN ('full_access', 'super_admin', 'engineer')
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

COMMIT;

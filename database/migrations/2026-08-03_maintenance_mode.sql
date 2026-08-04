-- =============================================================================
-- Migration: site-wide maintenance mode switch
-- Date: 2026-08-03
-- -----------------------------------------------------------------------------
-- One engineer-only control that pauses the site: it force-logs-out every
-- signed-in non-engineer, refuses non-engineer sign-ins and API calls while it
-- is on, and drives the public maintenance page.
--
-- Single-row config table, the same shape as `survey_send_config`: `id` pinned
-- to 1 by a CHECK constraint, seeded here so the application always has a row to
-- read and never has to handle a "no config yet" branch in the hot path.
--
-- SHAPE NOTES
--
--   * `enabled` DEFAULTS TO FALSE and the seeded row is inserted with the
--     default. Applying this migration must never, under any circumstance, turn
--     the site off — a maintenance switch that arrives already flipped would take
--     production down the moment CI promotes it.
--   * `message` is engineer-authored copy shown to the PUBLIC on the maintenance
--     page. NULL means "use the application's default copy"
--     (app/services/maintenance.DEFAULT_MESSAGE). It must never be used to carry
--     internal detail — the public status endpoint returns it verbatim.
--   * `enabled_at` / `enabled_by_user_id` are for the engineer console only and
--     are excluded from the public payload by a separate response schema
--     (`MaintenanceStatus` vs `MaintenanceState`), not by convention.
--   * ON DELETE SET NULL on the actor FK, matching `survey_send_config`: deleting
--     a user must not be blocked by, or cascade into, this row. The durable
--     record of who flipped the switch is the audit trail
--     (`maintenance_mode_enabled` / `maintenance_mode_disabled`), which snapshots
--     the actor's email via the audit trigger and so survives the deletion.
--
-- NO INDEXES: the table holds exactly one row, always fetched by primary key.
--
-- NOTHING IS BACKFILLED and no existing table is touched — this is purely
-- additive, so applying it is a no-op for every live session and every login.
-- The force-logout mechanism reuses `users.active_session_id` (#147) and needs
-- no schema change of its own.
--
-- SAFE TO RE-RUN: CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING.
-- Re-running will NOT reset an in-progress maintenance window.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS maintenance_mode (
    id                  int PRIMARY KEY DEFAULT 1,
    -- The switch. False = normal operation. See the note above on why the
    -- default and the seeded value are both false.
    enabled             boolean NOT NULL DEFAULT false,
    -- Public copy for the maintenance page; NULL = use the application default.
    message             text,
    -- When the CURRENT window was opened; NULL while disabled. Engineer-only.
    enabled_at          timestamptz,
    -- Last actor to flip the switch either way. Engineer-only.
    enabled_by_user_id  bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_maintenance_mode_singleton CHECK (id = 1),
    CONSTRAINT fk_maintenance_mode_enabled_by FOREIGN KEY (enabled_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL
);

-- Seed the single row so the app always has a config to read. Explicitly OFF.
INSERT INTO maintenance_mode (id, enabled) VALUES (1, false)
ON CONFLICT (id) DO NOTHING;

ALTER TABLE maintenance_mode ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand). Non-destructive to anything else — the table holds a
-- single operational flag and no user data.
--
-- RUN THIS ONLY WITH MAINTENANCE MODE OFF. Dropping the table while it is ON
-- resolves the switch to "unreadable", which the application treats as OFF
-- (fail-open), so the site comes back up rather than staying down — but the
-- sessions ended by the switch stay ended, and every non-engineer has to sign in
-- again.
--   DROP TABLE IF EXISTS maintenance_mode;
-- =============================================================================

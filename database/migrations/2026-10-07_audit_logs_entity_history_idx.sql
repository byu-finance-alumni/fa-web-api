-- =============================================================================
-- Index backing per-record version history (#45, GET /alumni/{id}/history).
--
-- The history read filters audit_logs to ONE record (entity_type, entity_id)
-- and pages it newest-first by created_at. The existing idx_audit_logs_entity
-- (entity_type, entity_id) finds the record's rows but leaves the sort to a
-- separate step; on a frequently-viewed record most of those rows are
-- view_profile / search disclosure reads the history query discards, so the
-- sort runs over a pile it then throws away. Adding created_at DESC as the
-- third key lets the planner walk one record's rows already in history order.
--
-- This index is a strict superset (same leading columns) of
-- idx_audit_logs_entity, so it can serve every query that one does. The old
-- index is deliberately NOT dropped here: dropping is a separate, reviewable
-- decision, and an extra index on an append-only table is cheap.
--
-- Not CONCURRENTLY: database/migrate.sh runs each file through psql and every
-- migration in this directory wraps itself in BEGIN/COMMIT, and CREATE INDEX
-- CONCURRENTLY cannot run inside a transaction block. audit_logs is small
-- enough (thousands of rows) that the brief write lock of a plain CREATE INDEX
-- is not a concern, so this follows the house convention.
--
-- Index-only addition: no data change, no new tables, so no RLS step needed.
-- Idempotent via IF NOT EXISTS, so safe to re-run.
-- =============================================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_audit_logs_entity_created_at
    ON audit_logs (entity_type, entity_id, created_at DESC);

COMMIT;

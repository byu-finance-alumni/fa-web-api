-- =============================================================================
-- Index supporting the Audit log page's chronological listing + date-range
-- filtering.
--
-- The audit endpoint always orders by created_at DESC and now accepts a
-- date_from/date_to range. A descending created_at index serves both the sort
-- and the range scan; the existing idx_audit_logs_entity (entity_type,
-- entity_id) already covers entity filtering. action_type / user-email filters
-- are low-cardinality enough to ride on top without their own indexes.
--
-- Index-only addition: no data change, no new tables, so no RLS step needed.
-- Idempotent via IF NOT EXISTS, so safe to re-run.
-- =============================================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at
    ON audit_logs (created_at DESC);

COMMIT;

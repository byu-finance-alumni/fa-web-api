-- =============================================================================
-- Migration: partial unique indexes on alumni.byu_id / alumni.net_id
-- Date: 2026-06-12
-- -----------------------------------------------------------------------------
-- Closes a TOCTOU gap: duplicate detection lives only in the application layer
-- (see app/services/hygiene.py), so two concurrent writes — or a bulk import
-- racing a manual create — could both pass the in-app check and insert the same
-- byu_id / net_id. A DB-level unique constraint is the only authoritative guard.
--
-- The indexes are PARTIAL:
--   * WHERE archived = false  — archived ("ghost") records are excluded so a
--     soft-deleted alum doesn't block re-creating / re-importing the same id.
--     This matches the active-only duplicate semantics in detect_duplicates().
--   * WHERE <col> IS NOT NULL — only rows that actually carry an id participate;
--     many alumni have neither id, and NULLs must not collide.
--
-- byu_id is normalized to digits-only FIRST (the cleaner already digit-strips
-- byu_id on every write, so this is a no-op against app-written data; it only
-- matters for any legacy/mock rows that stored a formatted id like
-- "123-45-6789"). Without it, "123-45-6789" and "123456789" would be treated as
-- distinct and the index would not catch the duplicate.
--
-- !! NOT YET APPLIED to the shared Supabase database. Apply it BEFORE the first
-- real-data import. CREATE UNIQUE INDEX will FAIL if active duplicates already
-- exist — that failure is the intended signal to de-dup the data first.
--
-- SAFE TO RE-RUN: the UPDATE is idempotent (digit-strip of digits is itself);
-- the indexes use IF NOT EXISTS.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. Normalize any non-digit byu_id to digits-only (matches the app cleaner).
--    No-op for already-clean ids; only rewrites rows containing a non-digit.
-- -----------------------------------------------------------------------------

UPDATE alumni SET byu_id = regexp_replace(byu_id, '\D', '', 'g')
  WHERE byu_id IS NOT NULL AND byu_id ~ '\D';

-- -----------------------------------------------------------------------------
-- 2. Partial unique indexes over ACTIVE (non-archived) rows that carry an id.
-- -----------------------------------------------------------------------------

CREATE UNIQUE INDEX IF NOT EXISTS uq_alumni_byu_id_active
  ON alumni (byu_id) WHERE archived = false AND byu_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_alumni_net_id_active
  ON alumni (net_id) WHERE archived = false AND net_id IS NOT NULL;

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT indexname FROM pg_indexes
-- WHERE tablename = 'alumni'
--   AND indexname IN ('uq_alumni_byu_id_active', 'uq_alumni_net_id_active');
--
-- Pre-flight check for active duplicates BEFORE applying (these must return 0):
-- SELECT byu_id, count(*) FROM alumni
--   WHERE archived = false AND byu_id IS NOT NULL
--   GROUP BY byu_id HAVING count(*) > 1;
-- SELECT net_id, count(*) FROM alumni
--   WHERE archived = false AND net_id IS NOT NULL
--   GROUP BY net_id HAVING count(*) > 1;

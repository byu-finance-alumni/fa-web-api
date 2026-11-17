-- ============================================================================
-- 2026-11-17_purge_data_cleanup_backup.sql
-- Drop the data_cleanup_backup table (Jake, 2026-11-17: "it looks good").
--
-- It held the old values from the one-time cleanups
-- (2026-11-13_cleanup_unknown_and_nilf.sql, cleanups 672/671/673, and
-- 2026-11-17_nilf_by_employment_status.sql, cleanup 681) so they could be
-- undone. Jake checked the results on prod and approved, so the undo copy is no
-- longer needed. IRREVERSIBLE: after this, those cleanups cannot be rolled back.
--
-- Named "purge" so it sorts AFTER 2026-11-17_nilf_by_employment_status.sql
-- (which writes into this table) on a fresh database.
-- ============================================================================
BEGIN;

DROP TABLE IF EXISTS data_cleanup_backup;

COMMIT;

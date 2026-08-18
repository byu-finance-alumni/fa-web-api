-- Version history, part 1: group an audit trail into versions and label where
-- each write came from (#45).
--
-- `audit_logs` already has field_name / old_value / new_value, but until now
-- only the alumni CORE row populated them; every nested section (contact,
-- current employment, education, engagement) recorded nothing but "a change
-- happened", and the per-row employment/education endpoints recorded not even
-- the field. The application change that starts capturing those old values ships
-- alongside this migration; two columns are added here to make the captured rows
-- usable.
--
-- 1. change_set_id -- ONE SAVE = ONE VERSION.
--    A save that changes five fields writes five rows. The only grouping key
--    available today is created_at, and it does not work: Postgres now() is
--    TRANSACTION-START time, and `import_csv.commit_update` applies an entire
--    bulk CSV update in a SINGLE transaction, so thousands of records' rows all
--    carry the same instant. A per-save uuid is generated in the application
--    (app/core/audit_context.new_change_set_id) and written on every row of that
--    save, so version grouping is exact regardless of transaction boundaries.
--
-- 2. source -- 'manual' | 'import'.
--    Hand edits and bulk CSV updates both flow through
--    `alumni_service.update_alumni`, so an audit row cannot currently say which
--    it was. A later restore feature needs that distinction before it reverts a
--    value: restoring a field that an import legitimately corrected would
--    silently undo good data. Recording it now costs one varchar per row and
--    cannot be reconstructed later, which is the whole reason it lands with the
--    capture work rather than with the restore work.
--
-- BOTH COLUMNS ARE NULLABLE WITH NO DEFAULT, and nothing already in the table is
-- rewritten. Existing rows keep change_set_id IS NULL (= "not grouped"; they
-- predate the concept) and source IS NULL (= "provenance unknown"), which is
-- honest -- backfilling either would be inventing history. Audit rows written by
-- paths that carry no provenance (logins, exports, disclosure reads) also leave
-- source NULL by design; only the alumni record-change paths set it.
--
-- `engineer_action_log` mirrors `audit_logs` column-for-column because the
-- before_flush guard in app/models/audit.py REROUTES a suppressed engineer's
-- audit row into it. Without the same two columns there, an engineer's save
-- would lose its grouping and provenance on the way across, so they are added to
-- both tables in one migration.
--
-- Additive and non-blocking: ADD COLUMN with no default and no NOT NULL does not
-- rewrite the table, so this is a catalog-only change even on a large audit_logs.

BEGIN;

ALTER TABLE audit_logs
    ADD COLUMN IF NOT EXISTS change_set_id varchar(36);
ALTER TABLE audit_logs
    ADD COLUMN IF NOT EXISTS source varchar(20);

COMMENT ON COLUMN audit_logs.change_set_id IS
    'Groups the rows written by one save into one version (#45). NULL on rows predating the column.';
COMMENT ON COLUMN audit_logs.source IS
    'Write provenance: manual | import (#45). NULL where the path carries none.';

-- The read shape version history will use: "every row of this change set".
-- Partial (WHERE NOT NULL) so the ~1.5k existing NULL rows -- and every future
-- login/export/disclosure row, which never carries a change set -- stay out of
-- the index entirely.
CREATE INDEX IF NOT EXISTS idx_audit_logs_change_set_id
    ON audit_logs (change_set_id)
    WHERE change_set_id IS NOT NULL;

-- Mirror onto the engineer oversight trail (see header).
ALTER TABLE engineer_action_log
    ADD COLUMN IF NOT EXISTS change_set_id varchar(36);
ALTER TABLE engineer_action_log
    ADD COLUMN IF NOT EXISTS source varchar(20);

COMMIT;

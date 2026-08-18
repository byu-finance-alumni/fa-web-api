-- Version history, part 1b: 'survey' becomes a third audit provenance (#45).
--
-- COMMENT-ONLY. No DDL, no data change, no rewrite.
--
-- `audit_logs.source` was added earlier today (2026-08-17_audit_change_set_and_source.sql)
-- as a plain varchar(20) with NO check constraint, so the application can start
-- writing 'survey' without any schema change at all. What DOES need changing is
-- the column comment: it enumerates the legal values, and a comment that says
-- "manual | import" while the table holds 'survey' rows is the kind of quiet
-- documentation drift that sends the next person reading this schema looking for
-- a bug that isn't there.
--
-- Why a third value rather than folding it into 'manual': `survey_responses.apply_response`
-- writes an alum's OWN answers about themselves, which a staff reviewer approved.
-- That is not a staff hand edit and it is not a spreadsheet correction. Restore
-- has to be able to tell them apart -- reverting an alum's correction of their
-- own employer is a very different act from reverting an import that clobbered
-- it -- and provenance cannot be reconstructed after the fact.
--
-- The comment on `engineer_action_log` is not touched here because that table's
-- source column never carried one; the before_flush reroute in app/models/audit.py
-- copies whatever `audit_logs.source` would have held, 'survey' included.

BEGIN;

COMMENT ON COLUMN audit_logs.source IS
    'Write provenance: manual | import | survey (#45). NULL where the path carries none.';

COMMIT;

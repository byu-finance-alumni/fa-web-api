-- Add alumni.languages — free-text list of languages the alum speaks
-- (e.g. "English; Spanish").
--
-- Store-only: it is populated/read via CSV import and export and is NOT rendered
-- on the profile or exposed through any response schema. Mirrors the varchar(255)
-- column added to database/schema.sql and the SQLAlchemy Alumni model. Idempotent
-- (ADD COLUMN IF NOT EXISTS) so a re-run is a no-op.
BEGIN;

ALTER TABLE alumni ADD COLUMN IF NOT EXISTS languages varchar(255);

COMMIT;

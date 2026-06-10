-- =============================================================================
-- Migration: add full birthday + spouse fields to alumni
-- Date: 2026-06-09
-- -----------------------------------------------------------------------------
-- Adds a full birth date (we previously only stored birth_year) and spouse
-- attributes (name + birthday). When an alumnus's spouse is ALSO an alumnus,
-- spouse_alumni_id links the two records so the profile can deep-link to the
-- spouse's page. The link is a self-referential FK on the alumni table:
--   * ON DELETE SET NULL  — archiving/deleting the spouse's record just clears
--     the pointer, it never cascades a delete back to this alumnus.
--
-- All columns live on the existing `alumni` table, which already has RLS
-- enabled (see rls_lockdown.sql), so no new RLS step is needed here — adding
-- columns inherits the table's deny-all policy for the Supabase API roles.
--
-- SAFE TO RE-RUN: ADD COLUMN IF NOT EXISTS throughout; the FK + index are
-- guarded so a second run is a no-op.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. New columns on alumni
-- -----------------------------------------------------------------------------

ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS birth_date         date,
    ADD COLUMN IF NOT EXISTS spouse_first_name  varchar(100),
    ADD COLUMN IF NOT EXISTS spouse_last_name   varchar(100),
    ADD COLUMN IF NOT EXISTS spouse_birth_date  date,
    ADD COLUMN IF NOT EXISTS spouse_alumni_id   bigint;

-- -----------------------------------------------------------------------------
-- 2. Self-referential FK: spouse_alumni_id -> alumni.alumni_id
--    Guarded so the migration is idempotent (ADD CONSTRAINT has no IF NOT
--    EXISTS in Postgres).
-- -----------------------------------------------------------------------------

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'fk_alumni_spouse_alumni_id'
    ) THEN
        ALTER TABLE alumni
            ADD CONSTRAINT fk_alumni_spouse_alumni_id
            FOREIGN KEY (spouse_alumni_id) REFERENCES alumni (alumni_id)
            ON DELETE SET NULL;
    END IF;
END$$;

-- A record may not be its own spouse.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_alumni_spouse_not_self'
    ) THEN
        ALTER TABLE alumni
            ADD CONSTRAINT ck_alumni_spouse_not_self
            CHECK (spouse_alumni_id IS NULL OR spouse_alumni_id <> alumni_id);
    END IF;
END$$;

-- -----------------------------------------------------------------------------
-- 3. Index the FK for reverse lookups ("who is linked to this alumnus?").
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_alumni_spouse_alumni_id
    ON alumni (spouse_alumni_id);

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT column_name, data_type
-- FROM information_schema.columns
-- WHERE table_name = 'alumni'
--   AND column_name IN ('birth_date','spouse_first_name','spouse_last_name',
--       'spouse_birth_date','spouse_alumni_id')
-- ORDER BY column_name;

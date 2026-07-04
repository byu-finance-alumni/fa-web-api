-- =============================================================================
-- Migration: fleet-audit constraints + indexes (issues #171, #172, #175, #176)
-- Date: 2026-07-03
-- -----------------------------------------------------------------------------
-- Batches the schema-integrity and query-performance fixes surfaced by the
-- fleet audit. Two classes of change:
--
--   * INTEGRITY GUARDS (unique / check constraints) — enforce at the DB level
--     invariants the application layer already assumes:
--       #171  one contact-info row and one current-employment row per alum
--       #175  net_id unique case-insensitively over active rows
--       #176  login_attempts.email_lc must already be lowercase
--       #175  duplicate_candidates pairs are ordered + unique (table has no
--             writer yet, so it is empty; the guard is preventative)
--
--   * LOOKUP / HOT-PATH INDEXES (additive, no data change) — back the search,
--     dashboard and de-dup code paths:
--       #172  case-insensitive mst_id lookup
--       #175  graduation_year, (archived,is_alumni) predicate, contact country,
--             employment current_state
--
-- Applied to the SHARED dev/prod Supabase database on 2026-07-03. Every
-- pre-flight below returned 0 conflicting rows, so all statements were applied.
--
-- SAFE TO RE-RUN: all indexes use IF NOT EXISTS; the constraint ADDs are guarded
-- by DO blocks that no-op when the constraint already exists.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- #171 -- one contact-info row per alum.
--   Pre-flight (must be 0):
--     SELECT alumni_id, count(*) FROM alumni_contact_info
--       GROUP BY alumni_id HAVING count(*) > 1;
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_alumni_contact_info_alumni_id
    ON alumni_contact_info (alumni_id);

-- -----------------------------------------------------------------------------
-- #171 -- one current-employment row per alum.
--   Pre-flight (must be 0):
--     SELECT alumni_id, count(*) FROM current_employment
--       GROUP BY alumni_id HAVING count(*) > 1;
-- -----------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_current_employment_alumni_id
    ON current_employment (alumni_id);

-- -----------------------------------------------------------------------------
-- #172 -- case-insensitive mst_id lookup (additive, no pre-flight).
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_alumni_mst_id_lower
    ON alumni (lower(trim(mst_id))) WHERE mst_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- #175 -- hot-path lookup indexes (additive, no pre-flight).
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_alumni_graduation_year
    ON alumni (graduation_year);

CREATE INDEX IF NOT EXISTS idx_alumni_archived_is_alumni
    ON alumni (archived, is_alumni);

CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_country
    ON alumni_contact_info (country);

CREATE INDEX IF NOT EXISTS idx_current_employment_state
    ON current_employment (current_state);

-- -----------------------------------------------------------------------------
-- #175 -- net_id unique case-insensitively over ACTIVE rows.
--   Replaces the case-sensitive uq_alumni_net_id_active from
--   migrations/2026-06-12_alumni_unique_byu_net.sql so "Abc123" and "abc123"
--   collide. The plain non-unique idx_alumni_net_id is left untouched.
--   Pre-flight (must be 0):
--     SELECT lower(trim(net_id)), count(*) FROM alumni
--       WHERE archived = false AND net_id IS NOT NULL
--       GROUP BY 1 HAVING count(*) > 1;
-- -----------------------------------------------------------------------------
DROP INDEX IF EXISTS uq_alumni_net_id_active;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alumni_net_id_lower_active
    ON alumni (lower(trim(net_id))) WHERE archived = false AND net_id IS NOT NULL;

-- -----------------------------------------------------------------------------
-- #176 -- login_attempts.email_lc must already be lowercase.
--   Pre-flight (must be 0):
--     SELECT email_lc FROM login_attempts WHERE email_lc <> lower(email_lc);
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_login_attempts_email_lc_lower'
    ) THEN
        ALTER TABLE login_attempts
            ADD CONSTRAINT ck_login_attempts_email_lc_lower
            CHECK (email_lc = lower(email_lc));
    END IF;
END $$;

-- -----------------------------------------------------------------------------
-- #175 -- duplicate_candidates: ordered + unique pair guard.
--   The table currently has no writer, so it is empty; this is preventative so
--   a future writer cannot store (a,b) and (b,a) as two distinct "duplicates".
--   Pre-flight (both must be 0):
--     SELECT count(*) FROM duplicate_candidates;
--     SELECT count(*) FROM duplicate_candidates WHERE alumni_id_1 >= alumni_id_2;
-- -----------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'ck_duplicate_candidates_ordered'
    ) THEN
        ALTER TABLE duplicate_candidates
            ADD CONSTRAINT ck_duplicate_candidates_ordered
            CHECK (alumni_id_1 < alumni_id_2);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_duplicate_candidates_pair'
    ) THEN
        ALTER TABLE duplicate_candidates
            ADD CONSTRAINT uq_duplicate_candidates_pair
            UNIQUE (alumni_id_1, alumni_id_2);
    END IF;
END $$;

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT indexname FROM pg_indexes
--   WHERE tablename IN ('alumni','alumni_contact_info','current_employment',
--                       'login_attempts','duplicate_candidates');
-- SELECT conname FROM pg_constraint
--   WHERE conname IN ('ck_login_attempts_email_lc_lower',
--                     'ck_duplicate_candidates_ordered',
--                     'uq_duplicate_candidates_pair');
-- =============================================================================

-- =============================================================================
-- Migration: add program-engagement fields
-- Date: 2026-06-02
-- -----------------------------------------------------------------------------
-- Adds the alumni program-engagement attributes that were missing from the
-- schema (NetTrek hosting, finance conferences, mentorship, PIFF donations,
-- Finance Society leadership, BBQ attendance, hiring, designations) plus the
-- missing contact / employment-location columns.
--
-- SECURITY: every NEW table below gets `ENABLE ROW LEVEL SECURITY` with NO
-- policies, matching database/rls_lockdown.sql. That is deny-all for the
-- Supabase API roles (anon, authenticated); the FastAPI backend is unaffected
-- because it connects with a privileged Postgres role that bypasses RLS.
-- Nothing here ships reachable through the public Data API.
--
-- SAFE TO RE-RUN: uses IF NOT EXISTS throughout and RLS enable is idempotent.
--
-- Dropdown option lists for the free-text fields (industries, conferences,
-- leadership roles, graduate degrees, etc.) live in database/dropdowns.md.
-- They are intentionally NOT enforced as DB enums/constraints.
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- 1. New columns on existing tables
-- -----------------------------------------------------------------------------

ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS net_id               varchar(50),
    ADD COLUMN IF NOT EXISTS finance_program_year int,
    ADD COLUMN IF NOT EXISTS graduate_degree      varchar(100);

ALTER TABLE current_employment
    ADD COLUMN IF NOT EXISTS current_zip varchar(20);

ALTER TABLE employment_history
    ADD COLUMN IF NOT EXISTS city  varchar(100),
    ADD COLUMN IF NOT EXISTS state varchar(100);

-- -----------------------------------------------------------------------------
-- 2. 1:1 program-engagement profile (Yes/No willingness flags + scalars)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alumni_program_engagement (
    engagement_profile_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id                       bigint NOT NULL,
    source_id                       bigint,
    nettrek_host_willing            boolean NOT NULL DEFAULT false,
    finance_conference_willing      boolean NOT NULL DEFAULT false,
    mentor_willing                  boolean NOT NULL DEFAULT false,
    company_event_sponsor_willing   boolean NOT NULL DEFAULT false,
    guest_speaker_willing           boolean NOT NULL DEFAULT false,
    help_at_event_willing           boolean NOT NULL DEFAULT false,
    case_competition_host_willing   boolean NOT NULL DEFAULT false,
    women_in_finance_mentor_willing boolean NOT NULL DEFAULT false,
    hired_finance_intern            boolean NOT NULL DEFAULT false,
    hired_finance_full_time         boolean NOT NULL DEFAULT false,
    piff_donor                      boolean NOT NULL DEFAULT false,
    piff_donor_amount               numeric(12,2),
    cfp_designation                 boolean NOT NULL DEFAULT false,
    cfa_designation                 boolean NOT NULL DEFAULT false,
    engagement_notes                text,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_program_engagement_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_alumni_program_engagement_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL,
    CONSTRAINT uq_alumni_program_engagement UNIQUE (alumni_id)
);

-- -----------------------------------------------------------------------------
-- 3. Mentor industries (multi-select) — see dropdowns.md "Mentor Industries"
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alumni_mentor_industries (
    mentor_industry_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    industry           varchar(100) NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_mentor_industries_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_alumni_mentor_industries UNIQUE (alumni_id, industry)
);

-- -----------------------------------------------------------------------------
-- 4. NetTrek hosting (one row per year hosted)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS nettrek_hosting (
    nettrek_hosting_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    source_id          bigint,
    host_year          int,
    host_company       varchar(255),
    notes              text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_nettrek_hosting_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_nettrek_hosting_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- 5. Finance conference participation (one row per conference per year)
--    `conference` values — see dropdowns.md "Finance Conferences"
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS conference_participation (
    conference_participation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    conference         varchar(100) NOT NULL,
    participation_year int,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_conference_participation_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_conference_participation UNIQUE (alumni_id, conference, participation_year)
);

-- -----------------------------------------------------------------------------
-- 6. Finance Society leadership (one row per role per year)
--    `leadership_role` values — see dropdowns.md "Finance Society Leadership Roles"
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS finance_society_leadership (
    finance_society_leadership_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id       bigint NOT NULL,
    leadership_role varchar(100) NOT NULL,
    role_year       int,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_finance_society_leadership_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE
);

-- -----------------------------------------------------------------------------
-- 7. Alumni BBQ attendance (one row per year attended)
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS bbq_attendance (
    bbq_attendance_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id         bigint NOT NULL,
    attended_year     int NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_bbq_attendance_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_bbq_attendance UNIQUE (alumni_id, attended_year)
);

-- -----------------------------------------------------------------------------
-- 8. Indexes on foreign keys / common lookups
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_alumni_net_id                        ON alumni (net_id);
CREATE INDEX IF NOT EXISTS idx_alumni_program_engagement_alumni_id  ON alumni_program_engagement (alumni_id);
CREATE INDEX IF NOT EXISTS idx_alumni_mentor_industries_alumni_id   ON alumni_mentor_industries (alumni_id);
CREATE INDEX IF NOT EXISTS idx_nettrek_hosting_alumni_id            ON nettrek_hosting (alumni_id);
CREATE INDEX IF NOT EXISTS idx_conference_participation_alumni_id   ON conference_participation (alumni_id);
CREATE INDEX IF NOT EXISTS idx_finance_society_leadership_alumni_id ON finance_society_leadership (alumni_id);
CREATE INDEX IF NOT EXISTS idx_bbq_attendance_alumni_id             ON bbq_attendance (alumni_id);

-- -----------------------------------------------------------------------------
-- 9. SECURITY — deny-all RLS on every new table (matches rls_lockdown.sql)
-- -----------------------------------------------------------------------------

ALTER TABLE alumni_program_engagement  ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni_mentor_industries   ENABLE ROW LEVEL SECURITY;
ALTER TABLE nettrek_hosting            ENABLE ROW LEVEL SECURITY;
ALTER TABLE conference_participation   ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_society_leadership ENABLE ROW LEVEL SECURITY;
ALTER TABLE bbq_attendance             ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- VERIFY (run after committing) — all six should show rowsecurity = true.
-- =============================================================================
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
--   AND tablename IN ('alumni_program_engagement','alumni_mentor_industries',
--       'nettrek_hosting','conference_participation','finance_society_leadership',
--       'bbq_attendance')
-- ORDER BY tablename;

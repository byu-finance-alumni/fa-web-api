-- =============================================================================
-- Finance Alumni Database — PostgreSQL Schema
-- Source of truth for the schema. Maintained by hand (the original dbdiagram.io
-- ERD PDF was removed once it fell out of date).
--
-- Conventions:
--   * bigint identity surrogate primary keys
--   * created_at / updated_at default to now()
--   * Foreign keys named fk_<table>_<column>
--   * source_id references are provenance pointers and are nullable
--   * alumni_id / user_id ownership references are NOT NULL where the row
--     cannot exist without its parent
--
-- ROW-LEVEL SECURITY IS NOT DECLARED HERE. Every table in `public` must run with
-- deny-all RLS; that is applied by ./rls_lockdown.sql, which sweeps whatever is
-- actually in the database rather than reading this file. Keeping the two apart
-- means a table missing from this snapshot still gets locked down (#424).
-- =============================================================================

BEGIN;

-- -----------------------------------------------------------------------------
-- Migration bookkeeping
-- -----------------------------------------------------------------------------

-- Created by ./migrate.sh's own bootstrap statement before it
-- applies anything, NOT by this file or by any migration — it has to exist
-- before the first migration can be recorded. Documented here anyway (#424):
-- being invisible to every schema file is exactly how it ended up as the one
-- table in the database running without RLS. One row per applied migration
-- filename; no data, no FKs, never read by the application.
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Identity & access control
-- -----------------------------------------------------------------------------

CREATE TABLE users (
    user_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_user_id    uuid UNIQUE NOT NULL,
    first_name      varchar(100),
    last_name       varchar(100),
    email           varchar(255) NOT NULL UNIQUE,
    active          boolean NOT NULL DEFAULT true,
    auth_provider   varchar(50),
    last_login_at   timestamptz,
    -- Force a password change on next login. Set true on account creation (temp
    -- password) or a super_admin password reset; cleared by the user themselves
    -- via POST /auth/password/complete. See app/api/routes/auth.py.
    must_change_password boolean NOT NULL DEFAULT false,
    -- Hard account lock after too many failed logins (see login_attempts and
    -- app/services/login_lockout.py). Cleared by a super_admin password reset.
    locked_at       timestamptz,
    locked_reason   text,
    -- Single active session per account (#147): the Supabase session_id of the
    -- MOST RECENT sign-in. A newer login overwrites it, so any earlier device's
    -- session no longer matches and is rejected (forced logout) on the backend.
    -- NULL until the user's first sign-in after this feature shipped.
    active_session_id  text,
    active_session_at  timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Rolling per-email failed-login counter driving the pre-login cooldown and
-- (for registered emails) the hard lock above. Keyed by lowercased email so it
-- is case-insensitive; intentionally NOT a FK to users so the cooldown path
-- works for non-existent emails too and cannot be used to enumerate accounts.
CREATE TABLE login_attempts (
    email_lc        text PRIMARY KEY,
    failed_count    int NOT NULL DEFAULT 0,
    first_failed_at timestamptz,
    last_failed_at  timestamptz,
    cooldown_until  timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- email_lc must already be lowercased by the writer (#176). See
    -- migrations/2026-07-03_fleet_audit_constraints_indexes.sql.
    CONSTRAINT ck_login_attempts_email_lc_lower CHECK (email_lc = lower(email_lc))
);

-- Login history (security log). One row per successful sign-in, written by
-- POST /auth/login (which also stamps users.last_login_at). Kept separate from
-- audit_logs: sign-in events are a security log, not the record-change audit
-- trail. email is snapshotted and user_id is ON DELETE SET NULL so the history
-- survives a later user deletion with attribution intact.
CREATE TABLE login_events (
    login_event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        bigint,
    email          varchar(255) NOT NULL,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    -- Client IP + approximate (IP-based) location captured by the Next.js login
    -- action from the incoming request (x-forwarded-for + Vercel geo headers).
    -- Nullable: absent in local dev / on logins recorded before this was added.
    ip_address     varchar(64),
    city           varchar(128),
    region         varchar(128),
    country        varchar(64),
    CONSTRAINT fk_login_events_user_id FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL
);
CREATE INDEX idx_login_events_occurred_at ON login_events (occurred_at DESC);
CREATE INDEX idx_login_events_user_id ON login_events (user_id);
CREATE INDEX idx_login_events_email ON login_events (email);
-- Retention: a pg_cron job ('purge-login-events-90d') deletes rows older than
-- 90 days daily — IP + location are personal data and shouldn't be kept forever.
-- See migration 2026-06-18_login_events_retention.sql.

-- Per-attempt FAILED-login security log (the counterpart to login_events). One
-- row per failed sign-in, written by POST /auth/login/record on a failure, so an
-- engineer can see who failed, when, and from what IP. Separate from
-- login_attempts (the rolling counter that drives cooldown/lock). email is the
-- attempted address, snapshotted; intentionally NOT a FK to users because a
-- failure may be for an email with no account (a probe). See migration
-- 2026-07-16_add_login_failures.sql.
CREATE TABLE login_failures (
    login_failure_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email            varchar(255) NOT NULL,
    occurred_at      timestamptz NOT NULL DEFAULT now(),
    ip_address       varchar(64),
    city             varchar(128),
    region           varchar(128),
    country          varchar(64),
    reason           varchar(64)
);
CREATE INDEX idx_login_failures_occurred_at ON login_failures (occurred_at DESC);

CREATE TABLE roles (
    role_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role_name        varchar(100) NOT NULL UNIQUE,
    role_description text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE user_roles (
    user_role_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      bigint NOT NULL,
    role_id      bigint NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_user_roles_user_id FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE,
    CONSTRAINT fk_user_roles_role_id FOREIGN KEY (role_id) REFERENCES roles (role_id) ON DELETE CASCADE,
    CONSTRAINT uq_user_roles UNIQUE (user_id, role_id)
);

-- Editable permission config (#164): which capabilities each role holds. A row's
-- presence grants the capability; capability codes are defined in code
-- (app/core/capabilities.py). Seeded from the historical guard mapping; the
-- engineer edits it via the permission editor. See migration
-- 2026-06-26_role_capabilities.
CREATE TABLE role_capabilities (
    role_capability_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role_id            bigint NOT NULL,
    capability_code    varchar(100) NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_role_capabilities_role_id FOREIGN KEY (role_id) REFERENCES roles (role_id) ON DELETE CASCADE,
    CONSTRAINT uq_role_capabilities UNIQUE (role_id, capability_code)
);

-- -----------------------------------------------------------------------------
-- Data provenance / imports
-- -----------------------------------------------------------------------------

CREATE TABLE data_sources (
    source_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_name        varchar(255) NOT NULL,
    source_type        varchar(100),
    source_description text,
    imported_at        timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE import_batches (
    import_batch_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    imported_by_user_id     bigint,
    source_id               bigint,
    import_file_name        varchar(255),
    imported_at             timestamptz NOT NULL DEFAULT now(),
    total_rows              int,
    created_count           int,
    updated_count           int,
    skipped_count           int,
    duplicate_warning_count int,
    import_notes            text,
    CONSTRAINT fk_import_batches_user_id FOREIGN KEY (imported_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_import_batches_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- Alumni core
-- -----------------------------------------------------------------------------

CREATE TABLE alumni (
    alumni_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id            bigint,
    byu_id               varchar(50),
    mst_id               varchar(50),
    net_id               varchar(50),
    first_name           varchar(100),
    middle_name          varchar(100),
    last_name            varchar(100),
    preferred_first_name varchar(100),
    birth_name           varchar(100),
    gender               varchar(30),
    birth_year           int,
    birth_date           date,
    graduation_year      int,
    -- graduation_month retained physically but no longer exposed in the API read
    -- schema (superseded by graduation_semester + graduation_class below).
    graduation_month     int,
    -- Semester + graduating class replace the raw month in the API surface.
    -- graduation_semester is one of Fall / Winter / Spring / Summer; the
    -- graduation_class is the graduating cohort/class, DISTINCT from
    -- graduation_year (they usually match but need not).
    graduation_semester  varchar(20),
    graduation_class     int,
    finance_program_year int,
    graduate_degree      varchar(100),
    -- Graduation year of a GRADUATE program (distinct from graduation_year).
    graduate_graduation_year int,
    -- Survey / demographics captured on the alumni survey. Optional/nullable
    -- additive fields. home_country is the country of ORIGIN (distinct from the
    -- current-address country on the contact record); employment_status is
    -- person-level (Employed / Unemployed / Retired / Student / Seeking, ...);
    -- other_designations is free-text (e.g. "Series 7, Series 63").
    citizenship          varchar(100),
    marital_status       varchar(50),
    -- Home town of ORIGIN (paired with home_country); distinct from the
    -- current-address city. Backs the profile "Hometown" line (#366).
    hometown             varchar(100),
    home_country         varchar(100),
    employment_status    varchar(50),
    other_designations   text,
    -- Free-text list of languages the alum speaks (e.g. "English; Spanish").
    -- Stored + import/export only; not shown on the profile.
    languages            varchar(255),
    survey_completed_date date,
    -- Manual-edit provenance for the profile ("Profile updated by Amy"): the
    -- date of the last manual profile update and the user who made it. FK ->
    -- users(user_id) ON DELETE SET NULL, mirroring spouse_alumni_id.
    profile_updated_date date,
    profile_updated_by_user_id bigint,
    -- Free-text "updated by" NAME from the intake sheet (as typed). DISTINCT from
    -- profile_updated_by_user_id (the resolved app-user FK); backs the "Profile
    -- updated by ..." hover fallback when no user FK is linked. See
    -- migrations/2026-07-08_add_profile_updated_by.sql.
    profile_updated_by   varchar(200),
    -- Secondary affiliation / education (#47, PRD section 6). Optional/nullable
    -- additive fields extending the record beyond the core program/employment
    -- fields. Short single-value fields are varchar; narrative fields are text.
    mba_program          varchar(255),
    law_school           varchar(255),
    medical_school       varchar(255),
    graduate_school      varchar(255),
    startup_involvement  text,
    advisory_roles       text,
    secondary_employment text,
    spouse_first_name    varchar(100),
    spouse_last_name     varchar(100),
    spouse_birth_date    date,
    spouse_alumni_id     bigint,
    deceased             boolean NOT NULL DEFAULT false,
    linkedin_url         varchar(500),
    notes                text,
    archived             boolean NOT NULL DEFAULT false,
    manually_edited_at   timestamptz,
    last_imported_at     timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL,
    CONSTRAINT fk_alumni_spouse_alumni_id FOREIGN KEY (spouse_alumni_id) REFERENCES alumni (alumni_id) ON DELETE SET NULL,
    CONSTRAINT fk_alumni_profile_updated_by_user_id FOREIGN KEY (profile_updated_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_alumni_spouse_not_self CHECK (spouse_alumni_id IS NULL OR spouse_alumni_id <> alumni_id)
);

CREATE INDEX IF NOT EXISTS idx_alumni_spouse_alumni_id ON alumni (spouse_alumni_id);
CREATE INDEX IF NOT EXISTS idx_alumni_profile_updated_by_user_id ON alumni (profile_updated_by_user_id);

-- Partial unique indexes: an active (non-archived) alum's byu_id / net_id must
-- be unique. These are the authoritative guard behind the application-layer
-- duplicate detection (closes a TOCTOU race between concurrent writes). NULL ids
-- and archived rows are excluded. byu_id is stored digits-only by the cleaner.
-- net_id is matched case-insensitively (lower(trim(...))) per #175.
-- See migrations/2026-06-12_alumni_unique_byu_net.sql and
-- migrations/2026-07-03_fleet_audit_constraints_indexes.sql.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alumni_byu_id_active
    ON alumni (byu_id) WHERE archived = false AND byu_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_alumni_net_id_lower_active
    ON alumni (lower(trim(net_id))) WHERE archived = false AND net_id IS NOT NULL;

CREATE TABLE alumni_contact_info (
    contact_info_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id       bigint NOT NULL,
    source_id       bigint,
    personal_email  varchar(255),
    work_email      varchar(255),
    phone           varchar(50),
    address_line_1  varchar(255),
    address_line_2  varchar(255),
    city            varchar(100),
    state           varchar(100),
    zip             varchar(20),
    country         varchar(100),
    region          varchar(100),
    -- Which contact method is flagged "preferred"; allowed values validated in
    -- the app layer (personal_email/work_email/phone/linkedin, or NULL = none).
    preferred_contact_method varchar(30),
    -- The literal "best contact" value from the intake sheet (a phone or email
    -- the alum flagged as best). Free text; distinct from preferred_contact_method
    -- (which names a method, not a value). See migrations/2026-07-08_add_best_contact.sql.
    best_contact    varchar(255),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_contact_info_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_alumni_contact_info_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

-- One contact-info row per alum (#171). See
-- migrations/2026-07-03_fleet_audit_constraints_indexes.sql.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alumni_contact_info_alumni_id
    ON alumni_contact_info (alumni_id);

CREATE TABLE current_employment (
    current_employment_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id                 bigint NOT NULL,
    source_id                 bigint,
    current_employer          varchar(255),
    current_title             varchar(255),
    current_industry          varchar(255),
    current_industry_secondary varchar(255),
    -- Company street address line ("Company Address" on the profile, #366);
    -- the city/state/country/zip below are the finer-grained location fields.
    company_address           varchar(255),
    current_city              varchar(100),
    current_state             varchar(100),
    current_country           varchar(100),
    current_zip               varchar(20),
    seniority_level           varchar(100),
    last_verified_at          timestamptz,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_current_employment_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_current_employment_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

-- One current-employment row per alum (#171). See
-- migrations/2026-07-03_fleet_audit_constraints_indexes.sql.
CREATE UNIQUE INDEX IF NOT EXISTS uq_current_employment_alumni_id
    ON current_employment (alumni_id);

CREATE TABLE education_history (
    education_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id     bigint NOT NULL,
    source_id     bigint,
    university    varchar(255),
    college       varchar(255),
    department    varchar(255),
    degree        varchar(255),
    major         varchar(255),
    degree_status varchar(100),
    degree_year   int,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_education_history_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_education_history_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

CREATE TABLE employment_history (
    employment_history_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id             bigint NOT NULL,
    source_id             bigint,
    employer_name         varchar(255),
    employment_title      varchar(255),
    employment_industry   varchar(255),
    city                  varchar(100),
    state                 varchar(100),
    start_year            int,
    end_year              int,
    is_current            boolean NOT NULL DEFAULT false,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_employment_history_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_employment_history_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- Verification & research
-- -----------------------------------------------------------------------------

CREATE TABLE verification_log (
    verification_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id           bigint NOT NULL,
    user_id             bigint,
    source_id           bigint,
    verified_field_name varchar(255),
    old_value           text,
    new_value           text,
    verification_notes  text,
    verified_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_verification_log_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_verification_log_user_id FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_verification_log_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

CREATE TABLE alumni_engagement (
    engagement_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id               bigint NOT NULL,
    source_id               bigint,
    engagement_interest_type varchar(255),
    engagement_notes        text,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_engagement_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_alumni_engagement_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

CREATE TABLE research_tracking (
    research_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id      bigint NOT NULL,
    user_id        bigint,
    checked_out    boolean NOT NULL DEFAULT false,
    research_flag  varchar(100),
    research_notes text,
    started_at     timestamptz,
    completed_at   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_research_tracking_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_research_tracking_user_id FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- Tags & status labels (many-to-many)
-- -----------------------------------------------------------------------------

CREATE TABLE tags (
    tag_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tag_name        varchar(100) NOT NULL UNIQUE,
    tag_description text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alumni_tags (
    alumni_tag_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id     bigint NOT NULL,
    tag_id        bigint NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_tags_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_alumni_tags_tag_id FOREIGN KEY (tag_id) REFERENCES tags (tag_id) ON DELETE CASCADE,
    CONSTRAINT uq_alumni_tags UNIQUE (alumni_id, tag_id)
);

CREATE TABLE status_labels (
    status_label_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    status_label_name        varchar(100) NOT NULL UNIQUE,
    status_label_description text,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alumni_status_labels (
    alumni_status_label_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id              bigint NOT NULL,
    status_label_id        bigint NOT NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_status_labels_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_alumni_status_labels_status_label_id FOREIGN KEY (status_label_id) REFERENCES status_labels (status_label_id) ON DELETE CASCADE,
    CONSTRAINT uq_alumni_status_labels UNIQUE (alumni_id, status_label_id)
);

-- Editable controlled vocabulary (#82): one row per dropdown option in a
-- category (industry, event_type, attendance_status, interaction_type).
-- Engineer/super_admin manage these at runtime; active=false soft-hides a term.
CREATE TABLE vocabulary_terms (
    term_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    category    varchar(50) NOT NULL,
    value       varchar(100) NOT NULL,
    sort_order  integer NOT NULL DEFAULT 0,
    active      boolean NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_vocabulary_terms_category_value UNIQUE (category, value)
);
CREATE INDEX ix_vocabulary_terms_category_active ON vocabulary_terms (category, active);

-- Engineer-curated "who to contact" entries shown to logged-in users on the
-- in-app error screen. See migration 2026-06-17_support_contacts.sql.
CREATE TABLE support_contacts (
    support_contact_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    role_label         varchar(100) NOT NULL,
    name               varchar(255) NOT NULL,
    email              varchar(255) NOT NULL,
    sort_order         integer NOT NULL DEFAULT 0,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- CRM activity: interactions, tasks, events, surveys, attachments
-- -----------------------------------------------------------------------------

CREATE TABLE interactions (
    interaction_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id             bigint NOT NULL,
    user_id               bigint,
    interaction_type      varchar(100),
    interaction_date_time timestamptz,
    interaction_notes     text,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_interactions_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_interactions_user_id FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE follow_up_tasks (
    follow_up_task_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    assigned_to_user_id bigint,
    task_title         varchar(255),
    due_date           date,
    completed          boolean NOT NULL DEFAULT false,
    completed_at       timestamptz,
    task_notes         text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_follow_up_tasks_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_follow_up_tasks_user_id FOREIGN KEY (assigned_to_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE events (
    event_id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    logged_by_user_id bigint,
    event_name        varchar(255) NOT NULL,
    event_type        varchar(100),
    event_date        date,
    event_location    varchar(255),
    event_notes       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_events_user_id FOREIGN KEY (logged_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE event_attendance (
    event_attendance_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            bigint NOT NULL,
    alumni_id           bigint NOT NULL,
    attendance_status   varchar(100),
    attendance_notes    text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_event_attendance_event_id FOREIGN KEY (event_id) REFERENCES events (event_id) ON DELETE CASCADE,
    CONSTRAINT fk_event_attendance_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_event_attendance UNIQUE (event_id, alumni_id)
);

-- Pay It Forward Fund donations (#161): a per-alumnus ledger of gifts, each an
-- amount tied to a month + year. Dollar amounts are gated to full_access+ in the
-- API (field-level); donor identity is view-access. See migration
-- 2026-06-27_donations.sql.
CREATE TABLE donations (
    donation_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id         bigint NOT NULL,
    amount            numeric(12, 2) NOT NULL,
    donation_month    smallint,
    donation_year     smallint NOT NULL,
    notes             text,
    logged_by_user_id bigint,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_donations_amount_positive CHECK (amount > 0),
    CONSTRAINT ck_donations_month_range CHECK (donation_month IS NULL OR donation_month BETWEEN 1 AND 12),
    CONSTRAINT ck_donations_year_range CHECK (donation_year BETWEEN 1900 AND 2200),
    CONSTRAINT ck_donations_notes_length CHECK (char_length(notes) <= 10000),
    CONSTRAINT fk_donations_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_donations_user_id FOREIGN KEY (logged_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- Unified notes (#39): free-text notes attached to exactly one of an alumni,
-- an interaction, or an event. The CHECK enforces single-target; each FK
-- cascades so a note never outlives its parent. See migration
-- 2026-06-22_unified_notes.sql.
CREATE TABLE notes (
    note_id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint,
    interaction_id     bigint,
    event_id           bigint,
    body               text NOT NULL,
    created_by_user_id bigint,
    updated_by_user_id bigint,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_notes_single_target CHECK (num_nonnulls(alumni_id, interaction_id, event_id) = 1),
    CONSTRAINT ck_notes_body_length CHECK (char_length(body) <= 10000),
    CONSTRAINT fk_notes_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_notes_interaction_id FOREIGN KEY (interaction_id) REFERENCES interactions (interaction_id) ON DELETE CASCADE,
    CONSTRAINT fk_notes_event_id FOREIGN KEY (event_id) REFERENCES events (event_id) ON DELETE CASCADE,
    CONSTRAINT fk_notes_created_by FOREIGN KEY (created_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_notes_updated_by FOREIGN KEY (updated_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE surveys (
    survey_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id       bigint NOT NULL,
    survey_year     int,
    survey_due_date date,
    completed       boolean NOT NULL DEFAULT false,
    completed_at    timestamptz,
    survey_status   varchar(100),
    survey_notes    text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_surveys_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE
);

-- Alum "confirm your info" submissions from the public survey link, STAGED for
-- admin review (per the email's "reviewed before applied" promise). `payload` is
-- the submitted values keyed by survey field keys (table.column). See
-- migrations/2026-07-27_survey_responses.sql.
-- `cycle_seq` / `stage` record WHICH campaign email this answers (#497), copied
-- at submit time from the `survey_send_log` row for the email the alum was
-- actually sent. Both are NULLABLE and NOT backfilled: a response that predates
-- the stamp has no knowable cycle, and a guessed number is indistinguishable
-- from a real one in a report. NEVER derive either from `submitted_at` — a
-- campaign starting in late December sends its reminders in January, so a
-- date-derived cycle splits one campaign in two (see the `survey_schedule` note
-- below). See migrations/2026-08-17_survey_response_cycle_stamp.sql.
CREATE TABLE survey_responses (
    survey_response_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id           bigint NOT NULL,
    graduation_year     int,
    payload             jsonb NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'pending',
    -- Staging key of a NEW profile photo the alum uploaded with this response
    -- (headshots bucket, `survey-pending/<id>`), pending admin review. See
    -- migrations/2026-07-28_survey_response_photo.sql.
    staged_photo_path   varchar(255),
    -- Which campaign asked, and which email in it the alum had most recently
    -- been sent (0 = initial, 1 = 1-week, 2 = 2-week). NULL = unknown.
    cycle_seq           int,
    stage               smallint,
    submitted_at        timestamptz NOT NULL DEFAULT now(),
    reviewed_by_user_id bigint,
    reviewed_at         timestamptz,
    CONSTRAINT fk_survey_responses_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_survey_responses_reviewer FOREIGN KEY (reviewed_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_responses_status CHECK (status IN ('pending', 'applied', 'rejected')),
    CONSTRAINT ck_survey_responses_cycle_seq CHECK (cycle_seq IS NULL OR cycle_seq >= 1),
    CONSTRAINT ck_survey_responses_stage CHECK (stage IS NULL OR stage BETWEEN 0 AND 2)
);
CREATE INDEX IF NOT EXISTS idx_survey_responses_status_year ON survey_responses (status, graduation_year);
CREATE INDEX IF NOT EXISTS idx_survey_responses_alumni_id ON survey_responses (alumni_id);
CREATE INDEX IF NOT EXISTS ix_survey_responses_year_cycle ON survey_responses (graduation_year, cycle_seq);

-- Survey send scheduler (#542). `survey_schedule` holds one row per graduation
-- year (initial send date + campaign state); a daily Vercel cron sends the due
-- stage. `survey_send_log` is the append-only record of every delivered email —
-- its UNIQUE (graduation_year, alumni_id, stage, cycle_seq) prevents
-- double-emailing across cron runs. See migrations/2026-07-29_survey_scheduler.sql.
-- `cycle_seq` is the CAMPAIGN identity (#357): a year's first campaign is cycle
-- 1, and starting the next annual one increments it. Without it the send log was
-- an ALL-TIME record, so a re-surveyed year selected zero targets at every stage
-- and "completed" having emailed nobody. It is deliberately an opaque counter,
-- not a date — a campaign starting in late December sends its reminders in
-- January, so a year-derived cycle would flip mid-campaign and re-send the
-- initial to the whole cohort. See migrations/2026-08-03_survey_campaign_cycle.sql.
-- `paused` is the REVERSIBLE stop (`cancelled` is terminal). `paused_at` is load-
-- bearing, not just an audit stamp: the send stage is derived from
-- `today - start_date`, so resume shifts `start_date` forward by the paused
-- duration to keep the cadence. `paused_from_status` is what resume restores.
-- Both are NULL unless the campaign is paused. See
-- migrations/2026-08-03_survey_schedule_pause.sql.
CREATE TABLE survey_schedule (
    survey_schedule_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    graduation_year     int NOT NULL UNIQUE,
    start_date          date NOT NULL,
    status              varchar(20) NOT NULL DEFAULT 'scheduled',
    created_by_user_id  bigint,
    last_run_at         timestamptz,
    paused_at           timestamptz,
    paused_from_status  varchar(20),
    cycle_seq           int NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_survey_schedule_created_by FOREIGN KEY (created_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_schedule_status CHECK (status IN ('scheduled', 'active', 'paused', 'completed', 'cancelled')),
    CONSTRAINT ck_survey_schedule_paused_from_status CHECK (paused_from_status IS NULL OR paused_from_status IN ('scheduled', 'active')),
    CONSTRAINT ck_survey_schedule_cycle_seq CHECK (cycle_seq >= 1)
);

CREATE TABLE survey_send_log (
    survey_send_log_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    graduation_year     int NOT NULL,
    alumni_id           bigint NOT NULL,
    stage               smallint NOT NULL,
    cycle_seq           int NOT NULL DEFAULT 1,
    -- Which engineer-reset generation this email belongs to (#395). 0 = the
    -- alumnus had never been reset when it went out. It is in the unique key
    -- because a reset must let the same (year, alumni, stage, cycle) be emailed
    -- again WITHOUT deleting the row recording the first one — ignoring the old
    -- row in the reads is not enough, the constraint itself refuses the insert
    -- and the claim's ON CONFLICT DO NOTHING would silently skip the recipient.
    -- See migrations/2026-08-05_survey_reset_log.sql.
    reset_seq           int NOT NULL DEFAULT 0,
    sent_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_survey_send_log_alumni FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT ck_survey_send_log_cycle_seq CHECK (cycle_seq >= 1),
    CONSTRAINT ck_survey_send_log_reset_seq CHECK (reset_seq >= 0),
    CONSTRAINT uq_survey_send_log_year_alumni_stage UNIQUE (graduation_year, alumni_id, stage, cycle_seq, reset_seq)
);
CREATE INDEX IF NOT EXISTS idx_survey_send_log_year_stage ON survey_send_log (graduation_year, stage);
CREATE INDEX IF NOT EXISTS ix_survey_send_log_year_cycle_stage ON survey_send_log (graduation_year, cycle_seq, stage);

-- Per-alumnus survey campaign resets (#395). A reset makes ONE person surveyable
-- again and DELETES NOTHING: their responses and send-log rows stay exactly as
-- they are, and every eligibility query ignores what predates the latest reset
-- here. `reset_seq` is a per-alumnus counter starting at 1; it is also the value
-- their next survey email's `survey_send_log.reset_seq` carries, which is what
-- keeps the unique key above from refusing the re-send. See
-- migrations/2026-08-05_survey_reset_log.sql.
CREATE TABLE survey_reset_log (
    survey_reset_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id            bigint NOT NULL,
    reset_seq            int NOT NULL,
    reset_at             timestamptz NOT NULL DEFAULT now(),
    reset_by_user_id     bigint,
    sends_superseded     int NOT NULL DEFAULT 0,
    responses_superseded int NOT NULL DEFAULT 0,
    CONSTRAINT fk_survey_reset_log_alumni FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_survey_reset_log_user FOREIGN KEY (reset_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_reset_log_seq CHECK (reset_seq >= 1),
    CONSTRAINT uq_survey_reset_log_alumni_seq UNIQUE (alumni_id, reset_seq)
);
CREATE INDEX IF NOT EXISTS ix_survey_reset_log_alumni_at ON survey_reset_log (alumni_id, reset_at DESC);

-- Deleted survey campaigns (#398). Any campaign can be deleted, whatever its
-- status, and none of its emails or answers are removed with it. `survey_schedule`
-- is the sole holder of a year's `cycle_seq`, so this row is where that number
-- survives the delete: `survey_email.current_cycle_seq` resolves a year with no
-- schedule to max(cycle_seq) + 1 here, which puts the next campaign for the year
-- ABOVE the retired send-log rows -- so the cycle-scoped double-send guard no
-- longer sees them and the send log's unique key cannot refuse the new claims.
-- Resolving to 1 instead is #357: everyone reads as already emailed and the
-- campaign completes having sent nothing. Same event-that-supersedes shape as
-- survey_reset_log, one level up (campaign rather than alumnus). See
-- migrations/2026-08-05_survey_campaign_retirement.sql.
CREATE TABLE survey_campaign_retirement (
    survey_campaign_retirement_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    graduation_year     int NOT NULL,
    cycle_seq           int NOT NULL,
    retired_at          timestamptz NOT NULL DEFAULT now(),
    retired_by_user_id  bigint,
    previous_status     varchar(20),
    start_date          date,
    sends_retired       int NOT NULL DEFAULT 0,
    responses_kept      int NOT NULL DEFAULT 0,
    CONSTRAINT fk_survey_campaign_retirement_user FOREIGN KEY (retired_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT ck_survey_campaign_retirement_cycle CHECK (cycle_seq >= 1),
    CONSTRAINT uq_survey_campaign_retirement_year_cycle UNIQUE (graduation_year, cycle_seq)
);
CREATE INDEX IF NOT EXISTS ix_survey_campaign_retirement_year_cycle ON survey_campaign_retirement (graduation_year, cycle_seq DESC);

-- Survey send cap (#542 follow-up). Single-row config (id pinned to 1) the
-- scheduler paces against: when `enabled`, sends at most `daily_limit`/day and
-- `monthly_limit`/month across every graduation year. See
-- migrations/2026-07-29_survey_send_config.sql.
CREATE TABLE survey_send_config (
    id                  int PRIMARY KEY DEFAULT 1,
    enabled             boolean NOT NULL DEFAULT true,
    daily_limit         int NOT NULL DEFAULT 100,
    monthly_limit       int NOT NULL DEFAULT 3000,
    updated_by_user_id  bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_survey_send_config_singleton CHECK (id = 1),
    CONSTRAINT ck_survey_send_config_daily   CHECK (daily_limit   >= 0),
    CONSTRAINT ck_survey_send_config_monthly CHECK (monthly_limit >= 0),
    CONSTRAINT fk_survey_send_config_updated_by FOREIGN KEY (updated_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- Site-wide maintenance mode. Single-row config (id pinned to 1) holding the
-- engineer's pause switch: while `enabled`, non-engineers cannot sign in or call
-- the API (503 / maintenance_mode) and the frontend shows a maintenance page.
-- `message` is PUBLIC copy (NULL = application default); `enabled_at` /
-- `enabled_by_user_id` are engineer-console-only and are never returned by the
-- public status endpoint. Engineers are exempt from the pause, which is what
-- makes the switch reversible. Force-logout reuses users.active_session_id
-- (#147) and needs no column here. See
-- migrations/2026-08-03_maintenance_mode.sql.
CREATE TABLE maintenance_mode (
    id                  int PRIMARY KEY DEFAULT 1,
    enabled             boolean NOT NULL DEFAULT false,
    message             text,
    enabled_at          timestamptz,
    enabled_by_user_id  bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_maintenance_mode_singleton CHECK (id = 1),
    CONSTRAINT fk_maintenance_mode_enabled_by FOREIGN KEY (enabled_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE attachments (
    attachment_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    uploaded_by_user_id bigint,
    file_name          varchar(255) NOT NULL,
    storage_key        varchar(500) NOT NULL,
    file_type          varchar(100),
    attachment_notes   text,
    uploaded_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_attachments_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_attachments_user_id FOREIGN KEY (uploaded_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- -----------------------------------------------------------------------------
-- Auditing & deduplication
-- -----------------------------------------------------------------------------

CREATE TABLE audit_logs (
    audit_log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      bigint,
    action_type  varchar(100) NOT NULL,
    entity_type  varchar(100) NOT NULL,
    entity_id    bigint,
    field_name   varchar(255),
    old_value    text,
    new_value    text,
    -- Actor identity snapshotted at INSERT time (trigger below) so it survives
    -- the actor's later deletion (user_id -> NULL). See migration
    -- 2026-06-17_audit_actor_snapshot.sql.
    actor_email  varchar(255),
    actor_name   varchar(255),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_audit_logs_user_id FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

-- Snapshot the acting user's email/name onto each audit row at write time, so a
-- later user deletion (user_id -> NULL) never erases who performed the action.
CREATE OR REPLACE FUNCTION audit_logs_snapshot_actor()
RETURNS trigger AS $$
BEGIN
    IF NEW.actor_email IS NULL AND NEW.user_id IS NOT NULL THEN
        SELECT u.email,
               NULLIF(TRIM(CONCAT_WS(' ', u.first_name, u.last_name)), '')
          INTO NEW.actor_email, NEW.actor_name
          FROM users u
         WHERE u.user_id = NEW.user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_logs_snapshot_actor ON audit_logs;
CREATE TRIGGER trg_audit_logs_snapshot_actor
    BEFORE INSERT ON audit_logs
    FOR EACH ROW
    EXECUTE FUNCTION audit_logs_snapshot_actor();

-- engineer_action_log: append-only, tamper-resistant record of engineer-actor
-- actions (#199 / #200 forensic blind spot). Engineer audit_logs writes are
-- suppressed so they don't clutter the record-change trail; the before_flush guard
-- reroutes each into a row here instead of dropping it (see app/models/audit.py).
-- No delete/purge route exists and only super_admin can read it (GET
-- /admin/engineer-actions) -- the engineer cannot read, delete, or disable it.
-- actor_email is snapshotted at INSERT (trigger below) so a row survives the
-- actor's later deletion (actor_user_id -> NULL). See migration
-- 2026-07-07_engineer_action_log.sql.
CREATE TABLE engineer_action_log (
    engineer_action_log_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_user_id bigint,
    actor_email   varchar(255),
    action_type   varchar(100) NOT NULL,
    entity_type   varchar(100) NOT NULL,
    entity_id     bigint,
    field_name    varchar(255),
    old_value     text,
    new_value     text,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_engineer_action_log_actor_user_id FOREIGN KEY (actor_user_id) REFERENCES users (user_id) ON DELETE SET NULL
);
CREATE INDEX idx_engineer_action_log_occurred_at ON engineer_action_log (occurred_at DESC);
CREATE INDEX idx_engineer_action_log_actor_user_id ON engineer_action_log (actor_user_id);

-- Snapshot the acting user's email onto each engineer_action_log row at write
-- time (mirrors audit_logs_snapshot_actor), so a later user deletion
-- (actor_user_id -> NULL) never erases who performed the action.
CREATE OR REPLACE FUNCTION engineer_action_log_snapshot_actor()
RETURNS trigger AS $$
BEGIN
    IF NEW.actor_email IS NULL AND NEW.actor_user_id IS NOT NULL THEN
        SELECT u.email
          INTO NEW.actor_email
          FROM users u
         WHERE u.user_id = NEW.actor_user_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_engineer_action_log_snapshot_actor ON engineer_action_log;
CREATE TRIGGER trg_engineer_action_log_snapshot_actor
    BEFORE INSERT ON engineer_action_log
    FOR EACH ROW
    EXECUTE FUNCTION engineer_action_log_snapshot_actor();

CREATE TABLE duplicate_candidates (
    duplicate_candidate_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id_1            bigint NOT NULL,
    alumni_id_2            bigint NOT NULL,
    match_reason           text,
    confidence_score       double precision,
    duplicate_status       varchar(100),
    reviewed_by_user_id    bigint,
    reviewed_at            timestamptz,
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_duplicate_candidates_alumni_id_1 FOREIGN KEY (alumni_id_1) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_duplicate_candidates_alumni_id_2 FOREIGN KEY (alumni_id_2) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_duplicate_candidates_user_id FOREIGN KEY (reviewed_by_user_id) REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT chk_duplicate_candidates_distinct CHECK (alumni_id_1 <> alumni_id_2),
    -- Ordered + unique pair guard (#175): a pair is stored once, low id first, so
    -- (a,b) and (b,a) cannot both exist. See
    -- migrations/2026-07-03_fleet_audit_constraints_indexes.sql.
    CONSTRAINT ck_duplicate_candidates_ordered CHECK (alumni_id_1 < alumni_id_2),
    CONSTRAINT uq_duplicate_candidates_pair UNIQUE (alumni_id_1, alumni_id_2)
);

-- -----------------------------------------------------------------------------
-- Program engagement (NetTrek, conferences, mentorship, donations, leadership)
-- Dropdown option lists for the free-text fields below live in dropdowns.md;
-- they are deliberately NOT enforced as DB enums/constraints.
-- -----------------------------------------------------------------------------

CREATE TABLE alumni_program_engagement (
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
    cfp_designation                 varchar(100),
    cfa_designation                 varchar(100),
    cpa_designation                 varchar(100),
    engagement_notes                text,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_program_engagement_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_alumni_program_engagement_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL,
    CONSTRAINT uq_alumni_program_engagement UNIQUE (alumni_id)
);

CREATE TABLE alumni_mentor_industries (
    mentor_industry_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    industry           varchar(100) NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_mentor_industries_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_alumni_mentor_industries UNIQUE (alumni_id, industry)
);

CREATE TABLE nettrek_hosting (
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

CREATE TABLE conference_participation (
    conference_participation_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id          bigint NOT NULL,
    conference         varchar(100) NOT NULL,
    participation_year int,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_conference_participation_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_conference_participation UNIQUE (alumni_id, conference, participation_year)
);

CREATE TABLE finance_society_leadership (
    finance_society_leadership_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id       bigint NOT NULL,
    leadership_role varchar(100) NOT NULL,
    role_year       int,
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_finance_society_leadership_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE
);

CREATE TABLE bbq_attendance (
    bbq_attendance_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    alumni_id         bigint NOT NULL,
    attended_year     int NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_bbq_attendance_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT uq_bbq_attendance UNIQUE (alumni_id, attended_year)
);

-- -----------------------------------------------------------------------------
-- Reference data & application config
-- -----------------------------------------------------------------------------

-- City -> lat/lng crosswalk backing the map radius/proximity search and the
-- county rollups (#151). Non-sensitive public US Census reference data, seeded
-- from the frontend crosswalk. Keys are normalized: city_norm = lower(trim(city)),
-- state = upper 2-letter. See migrations/2026-06-25_city_geo_crosswalk.sql and
-- 2026-06-26_city_geo_county_fips.sql.
CREATE TABLE city_geo (
    city_norm   text NOT NULL,
    state       char(2) NOT NULL,
    lat         double precision NOT NULL,
    lng         double precision NOT NULL,
    -- 5-digit county FIPS, so /geography/counties can aggregate nationwide.
    county_fips char(5),
    PRIMARY KEY (city_norm, state)
);

-- Engineer / super-admin-curated dashboard quick-filter presets: a label plus a
-- relative in-app deep link into a pre-filtered list. No active flag — admins
-- add, reorder and remove rows directly. See
-- migrations/2026-06-26_dashboard_presets.sql.
CREATE TABLE dashboard_presets (
    dashboard_preset_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    label               varchar(200) NOT NULL,
    href                varchar(500) NOT NULL,
    sort_order          integer NOT NULL DEFAULT 0,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- -----------------------------------------------------------------------------
-- Indexes on foreign keys / common lookups
-- -----------------------------------------------------------------------------

CREATE INDEX idx_user_roles_user_id              ON user_roles (user_id);
-- Retention purge on the rolling failed-login counter filters on this (#423);
-- see migrations/2026-08-07_schema_migrations_rls_and_login_retention.sql.
CREATE INDEX idx_login_attempts_last_failed_at   ON login_attempts (last_failed_at);
CREATE INDEX idx_user_roles_role_id              ON user_roles (role_id);
CREATE INDEX ix_role_capabilities_role_id        ON role_capabilities (role_id);
CREATE INDEX idx_import_batches_user_id          ON import_batches (imported_by_user_id);
CREATE INDEX idx_import_batches_source_id        ON import_batches (source_id);
CREATE INDEX idx_alumni_source_id                ON alumni (source_id);
CREATE INDEX idx_alumni_last_name                ON alumni (last_name);
CREATE INDEX idx_alumni_byu_id                   ON alumni (byu_id);
CREATE INDEX idx_alumni_net_id                   ON alumni (net_id);
-- Case-insensitive mst_id lookup (#172).
CREATE INDEX IF NOT EXISTS idx_alumni_mst_id_lower
    ON alumni (lower(trim(mst_id))) WHERE mst_id IS NOT NULL;
-- Graduation-year filter + (archived,is_alumni) hot-path predicate (#175).
CREATE INDEX IF NOT EXISTS idx_alumni_graduation_year   ON alumni (graduation_year);
CREATE INDEX IF NOT EXISTS idx_alumni_archived_is_alumni ON alumni (archived, is_alumni);
CREATE INDEX idx_alumni_contact_info_alumni_id   ON alumni_contact_info (alumni_id);
CREATE INDEX idx_alumni_contact_info_state        ON alumni_contact_info (state);
CREATE INDEX idx_alumni_contact_info_city_state   ON alumni_contact_info (city, state);
CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_country ON alumni_contact_info (country);
-- Expression indexes matching the normalized geography GROUP BYs (#186).
CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_state_norm      ON alumni_contact_info (upper(trim(state)));
CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_city_state_norm ON alumni_contact_info (lower(trim(city)), upper(trim(state)));
CREATE INDEX idx_current_employment_alumni_id    ON current_employment (alumni_id);
CREATE INDEX idx_current_employment_employer      ON current_employment (current_employer);
CREATE INDEX idx_current_employment_industry      ON current_employment (current_industry);
CREATE INDEX IF NOT EXISTS idx_current_employment_state ON current_employment (current_state);
-- Work-location indexes (#287): current_employment is the location record the
-- geography map / geocoded search / dashboard by-state read, so it carries the
-- same query load alumni_contact_info's geography indexes above used to.
CREATE INDEX IF NOT EXISTS idx_current_employment_city_state   ON current_employment (current_city, current_state);
CREATE INDEX IF NOT EXISTS idx_current_employment_country      ON current_employment (current_country);
CREATE INDEX IF NOT EXISTS idx_current_employment_state_norm      ON current_employment (upper(trim(current_state)));
CREATE INDEX IF NOT EXISTS idx_current_employment_city_state_norm ON current_employment (lower(trim(current_city)), upper(trim(current_state)));
CREATE INDEX idx_education_history_alumni_id     ON education_history (alumni_id);
CREATE INDEX idx_employment_history_alumni_id    ON employment_history (alumni_id);
CREATE INDEX idx_verification_log_alumni_id      ON verification_log (alumni_id);
CREATE INDEX idx_alumni_engagement_alumni_id     ON alumni_engagement (alumni_id);
CREATE INDEX idx_research_tracking_alumni_id     ON research_tracking (alumni_id);
CREATE INDEX idx_alumni_tags_alumni_id           ON alumni_tags (alumni_id);
CREATE INDEX idx_alumni_tags_tag_id              ON alumni_tags (tag_id);
CREATE INDEX idx_alumni_status_labels_alumni_id  ON alumni_status_labels (alumni_id);
CREATE INDEX idx_interactions_alumni_id          ON interactions (alumni_id);
-- Dashboard last-contacted anti-joins filter on alumni_id + date (#186).
CREATE INDEX idx_interactions_alumni_id_date     ON interactions (alumni_id, interaction_date_time);
CREATE INDEX idx_follow_up_tasks_alumni_id       ON follow_up_tasks (alumni_id);
CREATE INDEX idx_event_attendance_event_id       ON event_attendance (event_id);
CREATE INDEX idx_event_attendance_alumni_id      ON event_attendance (alumni_id);
CREATE INDEX idx_surveys_alumni_id               ON surveys (alumni_id);
CREATE INDEX idx_attachments_alumni_id           ON attachments (alumni_id);
CREATE INDEX idx_audit_logs_entity              ON audit_logs (entity_type, entity_id);
CREATE INDEX idx_audit_logs_created_at          ON audit_logs (created_at DESC);
CREATE INDEX idx_duplicate_candidates_alumni_1   ON duplicate_candidates (alumni_id_1);
CREATE INDEX idx_duplicate_candidates_alumni_2   ON duplicate_candidates (alumni_id_2);
CREATE INDEX idx_alumni_program_engagement_alumni_id  ON alumni_program_engagement (alumni_id);
CREATE INDEX idx_alumni_mentor_industries_alumni_id   ON alumni_mentor_industries (alumni_id);
CREATE INDEX idx_nettrek_hosting_alumni_id            ON nettrek_hosting (alumni_id);
CREATE INDEX idx_conference_participation_alumni_id   ON conference_participation (alumni_id);
CREATE INDEX idx_finance_society_leadership_alumni_id ON finance_society_leadership (alumni_id);
CREATE INDEX idx_bbq_attendance_alumni_id             ON bbq_attendance (alumni_id);
CREATE INDEX idx_notes_alumni_id                      ON notes (alumni_id);
CREATE INDEX idx_notes_interaction_id                 ON notes (interaction_id);
CREATE INDEX idx_notes_event_id                       ON notes (event_id);
CREATE INDEX idx_donations_alumni_id                  ON donations (alumni_id);
CREATE INDEX idx_donations_year                       ON donations (donation_year);

-- city_geo lookups: by state for the map's per-state work, by county FIPS for
-- the county rollups.
CREATE INDEX idx_city_geo_state                       ON city_geo (state);
CREATE INDEX idx_city_geo_county                      ON city_geo (county_fips);

-- Conference-attendee matching (#612). Expression indexes matching the
-- normalized exact-equality legs the matcher emits; see
-- migrations/2026-08-04_attendee_match_indexes.sql for why they must match the
-- emitted SQL verbatim.
CREATE INDEX idx_alumni_last_name_norm                ON alumni (lower(trim(last_name)));
CREATE INDEX idx_alumni_birth_name_norm               ON alumni (lower(trim(birth_name)));
CREATE INDEX idx_alumni_first_name_norm               ON alumni (lower(trim(first_name)));
CREATE INDEX idx_alumni_preferred_first_name_norm     ON alumni (lower(trim(preferred_first_name)));
CREATE INDEX idx_alumni_contact_info_personal_email_norm ON alumni_contact_info (lower(trim(personal_email)));
CREATE INDEX idx_alumni_contact_info_work_email_norm     ON alumni_contact_info (lower(trim(work_email)));

-- Free-text alumni search (#620). ``alumni_search_norm`` is the canonical
-- normal form both sides of every comparison collapse to (accents folded, case
-- folded, every non-alphanumeric character deleted) and the GIN trigram indexes
-- are what make the exact / prefix / contains / similar legs index scans rather
-- than a sequential scan. See migrations/2026-08-05_fuzzy_alumni_search.sql --
-- the index expressions must match the emitted SQL verbatim.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA extensions;

CREATE OR REPLACE FUNCTION public.immutable_unaccent(text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS $$
    SELECT extensions.unaccent('extensions.unaccent'::regdictionary, $1)
$$;

CREATE OR REPLACE FUNCTION public.alumni_search_norm(text)
RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT regexp_replace(
        lower(public.immutable_unaccent(coalesce($1, ''))),
        '[^a-z0-9]+', '', 'g'
    )
$$;

CREATE INDEX idx_alumni_first_name_trgm             ON alumni USING gin (public.alumni_search_norm(first_name) extensions.gin_trgm_ops);
CREATE INDEX idx_alumni_middle_name_trgm            ON alumni USING gin (public.alumni_search_norm(middle_name) extensions.gin_trgm_ops);
CREATE INDEX idx_alumni_last_name_trgm              ON alumni USING gin (public.alumni_search_norm(last_name) extensions.gin_trgm_ops);
CREATE INDEX idx_alumni_preferred_first_name_trgm   ON alumni USING gin (public.alumni_search_norm(preferred_first_name) extensions.gin_trgm_ops);
CREATE INDEX idx_alumni_birth_name_trgm             ON alumni USING gin (public.alumni_search_norm(birth_name) extensions.gin_trgm_ops);
CREATE INDEX idx_alumni_other_designations_trgm     ON alumni USING gin (public.alumni_search_norm(other_designations) extensions.gin_trgm_ops);
CREATE INDEX idx_alumni_byu_id_trgm                 ON alumni USING gin (public.alumni_search_norm(byu_id) extensions.gin_trgm_ops);
CREATE INDEX idx_alumni_net_id_trgm                 ON alumni USING gin (public.alumni_search_norm(net_id) extensions.gin_trgm_ops);
CREATE INDEX idx_current_employment_employer_trgm            ON current_employment USING gin (public.alumni_search_norm(current_employer) extensions.gin_trgm_ops);
CREATE INDEX idx_current_employment_title_trgm               ON current_employment USING gin (public.alumni_search_norm(current_title) extensions.gin_trgm_ops);
CREATE INDEX idx_current_employment_city_trgm                ON current_employment USING gin (public.alumni_search_norm(current_city) extensions.gin_trgm_ops);
CREATE INDEX idx_current_employment_state_trgm               ON current_employment USING gin (public.alumni_search_norm(current_state) extensions.gin_trgm_ops);
CREATE INDEX idx_current_employment_country_trgm             ON current_employment USING gin (public.alumni_search_norm(current_country) extensions.gin_trgm_ops);
CREATE INDEX idx_current_employment_industry_trgm            ON current_employment USING gin (public.alumni_search_norm(current_industry) extensions.gin_trgm_ops);
CREATE INDEX idx_current_employment_industry_secondary_trgm  ON current_employment USING gin (public.alumni_search_norm(current_industry_secondary) extensions.gin_trgm_ops);
CREATE INDEX idx_employment_history_employer_trgm            ON employment_history USING gin (public.alumni_search_norm(employer_name) extensions.gin_trgm_ops);

COMMIT;

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
-- =============================================================================

BEGIN;

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
    finance_program_year int,
    graduate_degree      varchar(100),
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
    CONSTRAINT ck_alumni_spouse_not_self CHECK (spouse_alumni_id IS NULL OR spouse_alumni_id <> alumni_id)
);

CREATE INDEX IF NOT EXISTS idx_alumni_spouse_alumni_id ON alumni (spouse_alumni_id);

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
    cfp_designation                 boolean NOT NULL DEFAULT false,
    cfa_designation                 boolean NOT NULL DEFAULT false,
    cpa_designation                 boolean NOT NULL DEFAULT false,
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
-- Indexes on foreign keys / common lookups
-- -----------------------------------------------------------------------------

CREATE INDEX idx_user_roles_user_id              ON user_roles (user_id);
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
CREATE INDEX idx_current_employment_alumni_id    ON current_employment (alumni_id);
CREATE INDEX idx_current_employment_employer      ON current_employment (current_employer);
CREATE INDEX idx_current_employment_industry      ON current_employment (current_industry);
CREATE INDEX IF NOT EXISTS idx_current_employment_state ON current_employment (current_state);
CREATE INDEX idx_education_history_alumni_id     ON education_history (alumni_id);
CREATE INDEX idx_employment_history_alumni_id    ON employment_history (alumni_id);
CREATE INDEX idx_verification_log_alumni_id      ON verification_log (alumni_id);
CREATE INDEX idx_alumni_engagement_alumni_id     ON alumni_engagement (alumni_id);
CREATE INDEX idx_research_tracking_alumni_id     ON research_tracking (alumni_id);
CREATE INDEX idx_alumni_tags_alumni_id           ON alumni_tags (alumni_id);
CREATE INDEX idx_alumni_tags_tag_id              ON alumni_tags (tag_id);
CREATE INDEX idx_alumni_status_labels_alumni_id  ON alumni_status_labels (alumni_id);
CREATE INDEX idx_interactions_alumni_id          ON interactions (alumni_id);
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

COMMIT;

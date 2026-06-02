-- =============================================================================
-- Finance Alumni Database — PostgreSQL Schema
-- Generated from "Finance Alumni Db.pdf" (dbdiagram.io ERD)
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
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

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
    first_name           varchar(100),
    middle_name          varchar(100),
    last_name            varchar(100),
    preferred_first_name varchar(100),
    birth_name           varchar(100),
    gender               varchar(30),
    birth_year           int,
    graduation_year      int,
    deceased             boolean NOT NULL DEFAULT false,
    linkedin_url         varchar(500),
    notes                text,
    archived             boolean NOT NULL DEFAULT false,
    manually_edited_at   timestamptz,
    last_imported_at     timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_alumni_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

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
    seniority_level           varchar(100),
    last_verified_at          timestamptz,
    created_at                timestamptz NOT NULL DEFAULT now(),
    updated_at                timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_current_employment_alumni_id FOREIGN KEY (alumni_id) REFERENCES alumni (alumni_id) ON DELETE CASCADE,
    CONSTRAINT fk_current_employment_source_id FOREIGN KEY (source_id) REFERENCES data_sources (source_id) ON DELETE SET NULL
);

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
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_audit_logs_user_id FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE SET NULL
);

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
    CONSTRAINT chk_duplicate_candidates_distinct CHECK (alumni_id_1 <> alumni_id_2)
);

-- -----------------------------------------------------------------------------
-- Indexes on foreign keys / common lookups
-- -----------------------------------------------------------------------------

CREATE INDEX idx_user_roles_user_id              ON user_roles (user_id);
CREATE INDEX idx_user_roles_role_id              ON user_roles (role_id);
CREATE INDEX idx_import_batches_user_id          ON import_batches (imported_by_user_id);
CREATE INDEX idx_import_batches_source_id        ON import_batches (source_id);
CREATE INDEX idx_alumni_source_id                ON alumni (source_id);
CREATE INDEX idx_alumni_last_name                ON alumni (last_name);
CREATE INDEX idx_alumni_byu_id                   ON alumni (byu_id);
CREATE INDEX idx_alumni_contact_info_alumni_id   ON alumni_contact_info (alumni_id);
CREATE INDEX idx_current_employment_alumni_id    ON current_employment (alumni_id);
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
CREATE INDEX idx_duplicate_candidates_alumni_1   ON duplicate_candidates (alumni_id_1);
CREATE INDEX idx_duplicate_candidates_alumni_2   ON duplicate_candidates (alumni_id_2);

COMMIT;

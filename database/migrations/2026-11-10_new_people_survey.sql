-- =============================================================================
-- Migration: new-people survey (api#661)
-- Date: 2026-11-10
-- -----------------------------------------------------------------------------
-- WHY. The career directors want ONE link they can email to alumni and
-- professionals who are NOT in the database yet. Filling it out must not add
-- anyone straight away: every answer is HELD for staff review, and a reviewer
-- either creates a new record (alum or friend) or applies it to an existing one.
--
-- The normal survey cannot carry this: its link is signed for an existing
-- alumni_id and `survey_responses.alumni_id` is NOT NULL. These people have no
-- id, so they get their own link and their own holding table.
--
-- TWO TABLES.
--
-- new_people_survey_link  (single row, id pinned to 1)
--   * `link_key`  -- the shared secret in the public URL. Random, URL-safe,
--     32+ characters, minted by the API (never by this file, so no key is ever
--     committed to the repo). Reissuing replaces it, which kills the old URL
--     immediately. Stored in the clear because staff must be able to read the
--     link back to email it; RLS (below) keeps it off the Data API.
--   * `enabled`   -- the on/off switch on the survey screen. A disabled link
--     answers exactly like a wrong key (404).
--   NOT SEEDED: the API creates the row, disabled, the first time a staff
--   member opens the screen. Until then the public routes simply 404.
--
-- new_people_submissions  (one row per held submission)
--   * `kind` -- what the respondent picked: `alum` (BYU Alumni Professional) or
--     `friend` (Non-BYU Alumni Professional). The reviewer may override it.
--   * `payload` -- the CLEANED survey answers, keyed by the existing survey field
--     keys plus `new.graduation_year` / `new.byu_degree`. PII. NULL once the
--     submission has been decided (approve or reject): the answers then live on
--     the record and in the field-level audit trail, or were refused, and a
--     second copy here would only be more PII to protect.
--   * `comments` -- the optional free-text box, same rules as the survey's
--     (#890). Also purged on decision.
--   * `fill_seconds` -- the survey's attacker-controllable timer, clamped by the
--     API before it lands here.
--   * `status` -- pending | approved | rejected. Only `pending` rows are listed.
--   * `decided_by_user_id` / `decided_at` -- who approved or rejected it.
--   * `alumni_id` -- the record an APPROVED submission created or merged into.
--
-- NOT COUNTED ANYWHERE. These rows are deliberately NOT in `alumni` or
-- `survey_responses`, so held people never appear in alumni counts, KPIs,
-- campaign / progress tallies or the normal survey review queue. Nothing reads
-- this table except the new-people routes.
--
-- SECURITY: both tables get deny-all RLS (no policies), registered in
-- database/rls_lockdown.sql like every other table. The app connects as the
-- table owner and bypasses RLS; anon/authenticated are denied.
--
-- PURELY ADDITIVE: two new tables, no existing table touched, nothing
-- backfilled. Old code ignores them, and new code on the old schema is never
-- reached because the routes that read them ship in the same release AFTER this
-- migration. Ship it as a migration-only commit first anyway, per the README.
--
-- SAFE TO RE-RUN: CREATE ... IF NOT EXISTS; the RLS enable is idempotent.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS new_people_survey_link (
    id                  int PRIMARY KEY DEFAULT 1,
    link_key            varchar(128) NOT NULL,
    enabled             boolean NOT NULL DEFAULT false,
    -- Last staff member to switch or reissue it. Console detail only; the audit
    -- trail is the durable record.
    updated_by_user_id  bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_new_people_survey_link_singleton CHECK (id = 1),
    CONSTRAINT ck_new_people_survey_link_key_length
        CHECK (char_length(link_key) BETWEEN 32 AND 128),
    CONSTRAINT fk_new_people_survey_link_updated_by
        FOREIGN KEY (updated_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS new_people_submissions (
    submission_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind                varchar(10) NOT NULL,
    payload             jsonb,
    comments            text,
    fill_seconds        int,
    submitted_at        timestamptz NOT NULL DEFAULT now(),
    status              varchar(20) NOT NULL DEFAULT 'pending',
    decided_by_user_id  bigint,
    decided_at          timestamptz,
    alumni_id           bigint,
    CONSTRAINT ck_new_people_submissions_kind CHECK (kind IN ('alum', 'friend')),
    CONSTRAINT ck_new_people_submissions_status
        CHECK (status IN ('pending', 'approved', 'rejected')),
    CONSTRAINT ck_new_people_submissions_fill_seconds
        CHECK (fill_seconds IS NULL OR fill_seconds >= 0),
    -- A pending row always holds its answers; a decided one never does.
    CONSTRAINT ck_new_people_submissions_payload_state
        CHECK ((status = 'pending') = (payload IS NOT NULL)),
    CONSTRAINT ck_new_people_submissions_comments_purged
        CHECK (status = 'pending' OR comments IS NULL),
    CONSTRAINT fk_new_people_submissions_decided_by
        FOREIGN KEY (decided_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL,
    CONSTRAINT fk_new_people_submissions_alumni
        FOREIGN KEY (alumni_id)
        REFERENCES alumni (alumni_id) ON DELETE SET NULL
);

-- The review queue (pending, oldest first) and the global pending cap's count.
CREATE INDEX IF NOT EXISTS idx_new_people_submissions_status_submitted
    ON new_people_submissions (status, submitted_at);

ALTER TABLE new_people_survey_link ENABLE ROW LEVEL SECURITY;
ALTER TABLE new_people_submissions ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS new_people_submissions;
--   DROP TABLE IF EXISTS new_people_survey_link;
-- Dropping the submissions table discards any held answers that were never
-- approved, which is the intended outcome of a rollback.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public'
--    AND tablename IN ('new_people_survey_link', 'new_people_submissions');
-- SELECT id, enabled, char_length(link_key) AS key_len, updated_at
--   FROM new_people_survey_link;
-- SELECT status, count(*) FROM new_people_submissions GROUP BY 1;

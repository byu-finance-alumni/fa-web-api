-- =============================================================================
-- Migration: survey_email_message (staff-editable copy of the survey email)
-- Date: 2026-09-09  (issue #524)
-- -----------------------------------------------------------------------------
-- WHY THIS TABLE EXISTS. The "Edit email message" box on the Needs Surveying
-- page saved its intro, its closing and its on-file field selection to the
-- BROWSER'S localStorage and nowhere else. The real send built subject and body
-- from string constants in app/services/survey_email.py, so a Career Director's
-- edit was per-browser, per-machine, invisible to the other director, and
-- reached not one alum. The box was a preview of something nobody was sending.
-- This table makes that copy data, and the send path reads it.
--
-- SINGLE-ROW CONFIG, `id` pinned to 1 by a CHECK constraint — the same shape as
-- `survey_send_config` and `maintenance_mode`. There is one survey email.
--
-- -----------------------------------------------------------------------------
-- A ROW HERE IS AN OVERRIDE, NOT THE SOURCE OF TRUTH
-- -----------------------------------------------------------------------------
-- The Career Directors' authored copy is compiled into
-- app/services/survey_message.py (DEFAULT_SUBJECT / DEFAULT_INTRO /
-- DEFAULT_CLOSING / ON_FILE_FIELDS). No row, a blank column, an unreadable
-- table, or a database that has never had this migration applied ALL mean "use
-- the default", resolved field by field. `survey_message.get_for_send` cannot
-- raise: every failure resolves to the built-in wording, which is byte for byte
-- what the email said before this feature existed.
--
-- That direction is the whole safety property, and it is the same one
-- alert_message_templates has: a feature that lets someone change what the email
-- SAYS must never be able to make it empty, and must never be able to stop a
-- send.
--
-- ⚠️ DELIBERATELY NOT SEEDED. alert_message_templates seeds its defaults so the
-- table is self-describing, and pays for it with a test that parses the .sql file
-- to keep the two copies identical. This email body is far longer and contains
-- the directors' prose; a second copy of it in SQL is a drift hazard with no
-- upside, because an absent row already resolves to the default. An empty table
-- here means "nobody has edited the email", which is exactly true on day one.
-- "Is this customised?" is decided by COMPARING the resolved copy against the
-- built-in defaults, never by whether a row exists — so seeding would not change
-- the answer either.
--
-- -----------------------------------------------------------------------------
-- `on_file_fields` CAN ONLY HIDE, NEVER ADD
-- -----------------------------------------------------------------------------
-- The labels of the "here's what we have on file" rows the email shows. Always a
-- SUBSET of survey_message.ON_FILE_FIELDS, stored in that canonical order,
-- validated on write and intersected with the canonical list AGAIN at render
-- time. So a value here — even one inserted by hand in psql — cannot introduce a
-- field the email does not know how to fill, cannot reorder the box, and cannot
-- put the email's field list out of step with the survey form or the sample
-- survey. Three lists must agree (form / email picker / sample values) and a
-- parity test enforces it; this column is structurally incapable of breaking
-- that agreement.
--
-- An empty array is legal and means the on-file box is omitted from the email.
--
-- -----------------------------------------------------------------------------
-- THE CHECK CONSTRAINTS, AND WHY THEY ARE IN POSTGRES AND NOT ONLY IN PYTHON
-- -----------------------------------------------------------------------------
--   ..._subject_len   1-200 chars. A subject longer than this is truncated by
--                     every mail client anyway, and an empty one is a mail that
--                     looks like spam.
--   ..._intro_len /
--   ..._closing_len   1-5000 chars each. Generous — this is a whole email body —
--                     but Resend answers an oversized payload with a 400, and an
--                     email lost to a 400 is worse than a wordy one.
--
-- The API validates all three before writing (and additionally rejects control
-- and invisible characters, and a line break in the SUBJECT, which is header
-- injection rather than formatting). These constraints are the layer that holds
-- when the write did not come through the API at all.
--
-- SECURITY: new table -> `ENABLE ROW LEVEL SECURITY` with NO policies, the
-- deny-all lockdown every table in this schema gets (mirrors #51). The app
-- connects as the table owner and bypasses RLS; anon/authenticated are denied.
-- Nothing here is PII, but this text is rendered into an email to alumni and the
-- Data API must not be allowed to become a second write path to it.
--
-- NO INDEXES: the table holds exactly one row, always fetched by primary key.
--
-- NOTHING IS BACKFILLED and no existing table is touched — this is purely
-- additive, so applying it changes nothing about what any send does until
-- somebody saves an edit.
--
-- SAFE TO RE-RUN: CREATE TABLE IF NOT EXISTS, and no seed to re-apply.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS survey_email_message (
    id                  int PRIMARY KEY DEFAULT 1,
    -- The email's subject line. Single line by API validation.
    subject             text NOT NULL,
    -- The paragraph(s) above the on-file box. Blank lines separate paragraphs;
    -- the HTML builder escapes this and then turns "\n\n" into paragraph breaks,
    -- so nothing typed here can introduce markup.
    intro               text NOT NULL,
    -- The paragraph(s) below the button, sign-off included. Escaped the same way,
    -- with single newlines becoming <br>.
    closing             text NOT NULL,
    -- Which on-file rows the email shows, by LABEL. See the header: subset only,
    -- canonical order, re-filtered at render time. Empty array = no on-file box.
    on_file_fields      text[] NOT NULL DEFAULT '{}',
    -- Who last edited it. Nullable / ON DELETE SET NULL: the copy must survive
    -- the account of whoever typed it being removed. The durable record of the
    -- edit is the audit trail (`update_survey_message` / `reset_survey_message`,
    -- rerouted into engineer_action_log for an engineer actor by the
    -- before_flush guard, #199), not this column.
    updated_by_user_id  bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_survey_email_message_singleton CHECK (id = 1),
    CONSTRAINT ck_survey_email_message_subject_len
        CHECK (char_length(subject) BETWEEN 1 AND 200),
    CONSTRAINT ck_survey_email_message_intro_len
        CHECK (char_length(intro) BETWEEN 1 AND 5000),
    CONSTRAINT ck_survey_email_message_closing_len
        CHECK (char_length(closing) BETWEEN 1 AND 5000),
    CONSTRAINT fk_survey_email_message_updated_by FOREIGN KEY (updated_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL
);

ALTER TABLE survey_email_message ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS survey_email_message;
-- Dropping the table restores the built-in wording exactly: the send-path read is
-- wrapped so an unreadable store means "no override", and the defaults in
-- app/services/survey_message.py are what the email said before this migration.
-- Any staff edit is lost, which is the intended meaning of the rollback.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public' AND tablename = 'survey_email_message';
-- SELECT id, char_length(subject) AS subj, char_length(intro) AS intro,
--        char_length(closing) AS closing, on_file_fields, updated_at
--   FROM survey_email_message;
-- -- The singleton and the length rules, proved (all of these must ERROR):
-- -- INSERT INTO survey_email_message (id, subject, intro, closing)
-- --      VALUES (2, 'x', 'y', 'z');
-- -- INSERT INTO survey_email_message (id, subject, intro, closing)
-- --      VALUES (1, '', 'y', 'z');
-- -- INSERT INTO survey_email_message (id, subject, intro, closing)
-- --      VALUES (1, repeat('x', 201), 'y', 'z');

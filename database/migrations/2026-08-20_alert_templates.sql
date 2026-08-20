-- =============================================================================
-- Migration: alert_message_templates (owner-editable Slack alert wording)
-- Date: 2026-08-20  (follows #444 / #456 / #457)
-- -----------------------------------------------------------------------------
-- WHY THIS TABLE EXISTS. The Slack lines this app sends are the first thing the
-- owner reads when something is wrong, and until now every word of them was a
-- string literal in app/services/failure_alert.py and app/services/login_abuse.py.
-- Changing "You are being attacked by ..." to say something he would rather read
-- at 2am meant a code change, a review, a deploy and a Vercel build. This table
-- makes the WORDING data, editable from the engineer Maintenance page, while
-- leaving the FACTS -- who, from where, how many, what we did -- entirely in the
-- renderers' hands.
--
-- ONE ROW PER MESSAGE KIND, and a row is an OVERRIDE, not the source of truth.
-- Every kind has a built-in default compiled into app/services/alert_templates.py
-- (KINDS[...].default). A row here replaces that default; deleting the row
-- restores it. That direction matters: if this table is dropped, unreadable, or
-- has never been migrated, alerting keeps working and says exactly what it says
-- today. A feature that makes the wording editable must not be able to make the
-- message disappear.
--
-- -----------------------------------------------------------------------------
-- WHAT A TEMPLATE MAY AND MAY NOT CONTAIN
-- -----------------------------------------------------------------------------
-- A template is literal text plus named placeholders in braces -- {ip},
-- {location}, {attempts}, {addresses}, {duration}, {action}. Substitution is a
-- single explicit scan in Python (NOT str.format, which would expose attribute
-- access via {x.__class__}, and NOT an f-string or eval, which would be
-- arbitrary code from a database row). Only the names a kind declares can
-- resolve, and the renderer hands substitution a dict containing nothing else.
--
-- ⚠️ THERE IS NO PLACEHOLDER FOR THE ATTEMPTED EMAIL ADDRESSES, AND THERE MUST
-- NEVER BE ONE. Same rule as login_abuse_incidents and login_ip_blocks, for the
-- same reason: those addresses are unverified strings a stranger typed, some of
-- them belong to real people, and a list of them in a Slack channel is both the
-- attacker's own scraped material republished and an enumeration oracle for
-- anyone who can see the channel. `{addresses}` is the COUNT
-- (`distinct_email_count`), never the addresses. A template cannot introduce
-- data the renderer does not already expose, because a template cannot name
-- anything the renderer did not put in the dict -- it is not a query language.
-- Tests assert the declared placeholder set contains no name that could reach an
-- address.
--
-- -----------------------------------------------------------------------------
-- THE TWO CONSTRAINTS, AND WHY THEY ARE IN POSTGRES AND NOT ONLY IN PYTHON
-- -----------------------------------------------------------------------------
--   ck_alert_templates_length   caps a body at 500 characters. Slack rejects an
--                               oversized payload with a 400, and an alert lost
--                               to a 400 is worse than a plain one.
--   ck_alert_templates_visible  rejects ASCII control characters. A newline, a
--                               NUL or an ESC in a body is either a mistake or
--                               an attempt to forge structure in a channel, and
--                               none of these messages is more than a sentence.
--
-- The API validates both before writing (and re-validates at RENDER time, so a
-- body inserted by hand in psql still cannot produce a broken message). These
-- constraints are the layer that holds when the write did not come through the
-- API at all.
--
-- SECURITY: new table -> `ENABLE ROW LEVEL SECURITY` with NO policies, the
-- deny-all lockdown every table in this schema gets (mirrors #51). The app
-- connects as the table owner and bypasses RLS; anon/authenticated are denied.
-- Nothing here is PII, but the write path is engineer-only and the Data API must
-- not be allowed to become a second one.
--
-- SAFE TO RE-RUN: IF NOT EXISTS on the table and the index; the RLS enable is
-- idempotent; the seed below is `ON CONFLICT DO NOTHING`, so re-running can
-- never overwrite wording the owner has since edited.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS alert_message_templates (
    template_id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- WHICH MESSAGE. One of the keys declared in app/services/alert_templates.py
    -- ('security_attack_opening', 'security_attack_resolved', 'outage_opening',
    -- 'outage_recovered'). Deliberately NOT a foreign key to an enum table: the
    -- set of kinds is a property of the CODE that renders them, so the code is
    -- the authority and a row whose key no code recognises is simply ignored
    -- (and reported as unknown by the console) rather than being a broken FK.
    template_key       varchar(64)  NOT NULL,
    -- THE WORDING. Literal text plus {placeholders}; see the header for what may
    -- appear and, more importantly, what may not.
    body               text         NOT NULL,
    -- Who last edited it. Nullable and ON DELETE SET NULL: the wording must
    -- survive the account of whoever typed it being removed. The durable record
    -- of the edit is the audit trail -- an engineer's AuditLog is rerouted to
    -- engineer_action_log by the before_flush guard (#199) -- not this column.
    updated_by_user_id bigint REFERENCES users(user_id) ON DELETE SET NULL,
    created_at         timestamptz  NOT NULL DEFAULT now(),
    updated_at         timestamptz  NOT NULL DEFAULT now(),
    -- Slack rejects an oversized payload outright, so this is the difference
    -- between a wordy alert and no alert.
    CONSTRAINT ck_alert_templates_length
        CHECK (char_length(body) BETWEEN 1 AND 500),
    -- No ASCII control characters. These messages are one sentence; a newline or
    -- an escape sequence in one is either a slip or an attempt to fake structure
    -- in the channel.
    CONSTRAINT ck_alert_templates_visible
        CHECK (body ~ '^[^[:cntrl:]]+$')
);

-- ONE ROW PER KIND. This is what makes the write an idempotent
-- `INSERT ... ON CONFLICT (template_key) DO UPDATE` rather than a read-then-write,
-- and it is the index that ON CONFLICT clause infers.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alert_templates_key
    ON alert_message_templates (template_key);

ALTER TABLE alert_message_templates ENABLE ROW LEVEL SECURITY;

-- -----------------------------------------------------------------------------
-- SEED: today's wording, byte for byte.
-- -----------------------------------------------------------------------------
-- These four strings are the SAME text as the built-in defaults in
-- app/services/alert_templates.py, and a test in tests/test_alert_templates.py
-- parses this file and asserts they still match -- so an edited default in
-- Python that was never mirrored here goes red in CI rather than shipping as a
-- silent wording difference on a freshly migrated database.
--
-- Seeding is not required for the feature to work (an absent row means "use the
-- built-in default"), and it deliberately does not change what any alert says.
-- It exists so the table is self-describing: an engineer reading
-- alert_message_templates sees the live wording rather than an empty table.
--
-- `ON CONFLICT DO NOTHING`, so re-running this migration cannot clobber an edit.
-- "Is this customised?" is decided by comparing the body against the built-in
-- default, never by whether a row exists -- otherwise these seeds would make
-- every kind look edited on day one.
INSERT INTO alert_message_templates (template_key, body) VALUES
    ('security_attack_opening',
     'You are being attacked by {ip}{location_phrase}. {action}'),
    ('security_attack_resolved',
     'The attack from {ip}{location_parenthetical} has stopped. {attempts} attempts across {addresses} addresses over {duration}. Nothing got in.'),
    ('outage_opening',
     'The API has been failing for long enough to be an incident. You will get one more email when it clears.'),
    ('outage_recovered',
     'The API is serving requests again. This incident is closed.')
ON CONFLICT (template_key) DO NOTHING;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS alert_message_templates;
-- Dropping the table restores the hard-coded wording exactly: every read in
-- app/services/alert_templates.py is wrapped so an unreadable store means "no
-- overrides", and the built-in defaults are what the alerts said before this
-- migration.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public' AND tablename = 'alert_message_templates';
-- SELECT template_key, char_length(body) AS len, body
--   FROM alert_message_templates ORDER BY template_key;
-- -- The two constraints, proved (both of these must ERROR):
-- -- INSERT INTO alert_message_templates (template_key, body)
-- --      VALUES ('probe', repeat('x', 501));
-- -- INSERT INTO alert_message_templates (template_key, body)
-- --      VALUES ('probe', E'two\nlines');

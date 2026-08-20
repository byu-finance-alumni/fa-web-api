-- =============================================================================
-- Migration: alert_delivery_config (Slack only, or Slack AND e-mail)
-- Date: 2026-08-19  (#458, follows #444 / #456)
-- -----------------------------------------------------------------------------
-- WHY THIS TABLE EXISTS. On 2026-08-19 alert delivery was changed so that Slack
-- is the channel and e-mail is the backstop: the mail goes out only when the
-- Slack post does not land. Before that, both went every time, which is why one
-- attack arrived twice. The owner now wants to CHOOSE between the two, and the
-- requirement he stated is the reason this is a table and not an environment
-- variable: he wants to change it WITHOUT A REDEPLOY, and every env var on this
-- stack needs one.
--
-- WHY IT LIVES IN THE DATABASE AND NOT IN MEMORY. The same argument as
-- 2026-08-18_service_incidents.sql, 2026-08-19_login_abuse_incidents.sql and
-- 2026-08-19_login_ip_blocks.sql: "which channels does this service alert on?"
-- is a fact about the SERVICE. This API runs on Vercel serverless, so a
-- module-level variable dies with the invocation and the twenty instances
-- handling an outage share no memory with each other -- exactly why the
-- in-memory rate limiter never fired on 2026-08-19. Postgres is the only
-- durable, already-connected shared store this stack has.
--
-- -----------------------------------------------------------------------------
-- THE SAFETY PROPERTY, AND WHERE IT LIVES
-- -----------------------------------------------------------------------------
-- ⚠️ NEITHER MODE CAN PRODUCE SILENCE. In `slack_only` the e-mail is not turned
-- off, it is turned into a BACKSTOP: a Slack post that fails, is rejected, or
-- has nowhere to go still falls through to the mail. The switch chooses whether
-- e-mail is a COPY or a BACKSTOP; there is deliberately no third value meaning
-- "Slack, and nothing if Slack breaks", because a single channel that breaks IS
-- silence and silence is the failure the alerting feature exists to prevent.
--
-- That property is enforced in app/services/failure_alert.deliver_alert -- the
-- only branch that skips the e-mail is reached when the Slack post SUCCEEDED --
-- and tests/test_alert_delivery.py asserts it for EVERY value in
-- alert_delivery.MODES rather than for the one that looks risky, so a mode added
-- later cannot quietly opt out. The CHECK constraint below is the database's
-- half: an unknown value cannot be stored at all.
--
-- FAIL-SAFE READS. app/services/alert_delivery.read_mode never raises. If this
-- table is missing (migration not yet applied), unreadable, or slow, the read
-- resolves to 'slack_only' -- the mode whose backstop sends MORE when Slack is
-- unhealthy. That matters here more than usual: the read happens on a request
-- that is ALREADY FAILING, quite possibly because the database is the outage.
--
-- -----------------------------------------------------------------------------
-- SHAPE NOTES
-- -----------------------------------------------------------------------------
-- Single-row config table, the same shape as `maintenance_mode` and
-- `survey_send_config`: `id` pinned to 1 by a CHECK constraint, seeded here so
-- the application always has a row to read and never needs a "no config yet"
-- branch on the alerting path.
--
--   * `mode` DEFAULTS TO 'slack_only' and the seeded row takes the default.
--     Applying this migration must not change how anything is delivered: the
--     seeded value is exactly what the code does today.
--   * `updated_by_user_id` is engineer-console detail. ON DELETE SET NULL,
--     matching `maintenance_mode` and `survey_send_config`: deleting a user must
--     not be blocked by, or cascade into, this row. The durable record of who
--     changed the setting is the audit trail (`set_alert_delivery_mode`), which
--     an engineer's write is rerouted into `engineer_action_log` for (#199) and
--     which snapshots the actor's e-mail, so it survives the deletion.
--   * NO PII AND NO SECRETS. The row holds one short enum value and a user id.
--     The webhook URLs and the alert recipients stay in environment variables --
--     a webhook URL is a credential -- and are never copied here.
--
-- NO INDEXES: the table holds exactly one row, always fetched by primary key.
--
-- SECURITY: new table -> `ENABLE ROW LEVEL SECURITY` with NO policies, the
-- deny-all lockdown every table in this schema gets (mirrors #51). The app
-- connects as the table owner and bypasses RLS; anon/authenticated are denied.
--
-- NOTHING IS BACKFILLED and no existing table is touched -- this is purely
-- additive, so applying it is a no-op for every live session, every login and
-- every alert already in flight.
--
-- SAFE TO RE-RUN: CREATE TABLE IF NOT EXISTS + INSERT ... ON CONFLICT DO
-- NOTHING; the RLS enable is idempotent in Postgres. Re-running will NOT reset a
-- mode the engineer has chosen.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS alert_delivery_config (
    id                  int PRIMARY KEY DEFAULT 1,
    -- 'slack_only'      = Slack is the channel, e-mail is the backstop (today).
    -- 'slack_and_email' = both channels on every alert (pre-2026-08-19).
    -- There is deliberately no 'slack_and_nothing_else' -- see the header.
    mode                varchar(20) NOT NULL DEFAULT 'slack_only',
    -- Last engineer to change it. Console detail only; the audit trail is the
    -- durable record. See the note above on ON DELETE SET NULL.
    updated_by_user_id  bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_alert_delivery_config_singleton CHECK (id = 1),
    -- The database's half of "an unknown mode is not representable". The API
    -- schema rejects one earlier (422) and the service normalises anything it
    -- does not recognise back to 'slack_only'; this makes a bad value
    -- unstorable even by a hand-written UPDATE in the SQL editor.
    CONSTRAINT ck_alert_delivery_config_mode
        CHECK (mode IN ('slack_only', 'slack_and_email')),
    CONSTRAINT fk_alert_delivery_config_updated_by FOREIGN KEY (updated_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL
);

-- Seed the single row so the app always has a config to read. Explicitly the
-- CURRENT behaviour, so applying this changes nothing about delivery.
INSERT INTO alert_delivery_config (id, mode) VALUES (1, 'slack_only')
ON CONFLICT (id) DO NOTHING;

ALTER TABLE alert_delivery_config ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone). Non-destructive: the table
-- holds a single operational preference and no user data.
--   DROP TABLE IF EXISTS alert_delivery_config;
-- Dropping it makes the setting unreadable, which app/services/alert_delivery.py
-- treats as 'slack_only' (fail-safe), so alerting continues exactly as it did
-- before this migration -- Slack first, e-mail whenever Slack does not land.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public' AND tablename = 'alert_delivery_config';
-- -- Exactly one row, and what it currently says:
-- SELECT id, mode, updated_by_user_id, updated_at FROM alert_delivery_config;
-- -- The CHECK really refuses an unknown mode (should ERROR, then ROLLBACK):
-- -- BEGIN; UPDATE alert_delivery_config SET mode = 'email_only'; ROLLBACK;

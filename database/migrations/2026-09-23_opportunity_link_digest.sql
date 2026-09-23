-- =============================================================================
-- Migration: opportunity_link_digest (the 6pm job-posting digest to staff)
-- Date: 2026-09-23  (issue #567, follows #771)
-- -----------------------------------------------------------------------------
-- WHY. When alumni submit job or internship links through the survey, the
-- Career Directors get ONE e-mail at about 6pm Mountain, and only on days a link
-- arrived. #771 built the digest and left it switched off; this makes it the
-- behaviour whenever an engineer has set recipients in the console.
--
-- TWO TABLES.
--
-- opportunity_link_digest_config  (single row, id pinned to 1)
--   * `recipients`  -- the staff addresses, set from the engineer console. A
--     TABLE AND NOT AN ENV VAR because the owner asked to manage them from the
--     console, and every env var on this stack needs a redeploy. EMPTY MEANS NO
--     DIGEST, and then the per-posting alert of #771 fires instead, so emptying
--     the list can never turn the notification off. Capped at 10 by a CHECK;
--     the API validates and lowercases each address before it gets here.
--   * `reported_through` -- the digest's WATERMARK: every survey posting
--     submitted at or before this instant has been reported. Each run reports
--     (reported_through, now - settle] and advances it only when an e-mail
--     actually landed. That is what makes the window gap-free AND repeat-free
--     however far inside its hour Vercel Hobby fires the cron, and what makes a
--     failed run carry its postings to the next one instead of dropping them.
--     NULL = never sent; the first run looks back a fixed window.
--   * `last_digest_on` -- the America/Denver DATE the digest last ran. Two cron
--     entries fire every evening (one per UTC offset, see the cron route) and
--     Vercel can deliver a cron twice; this makes it at most one digest per
--     local day however many calls arrive.
--
-- opportunity_link_digest_send_log  (append-only, one row per e-mail)
--   The digest spends from the SAME Resend account and the SAME UTC-day quota as
--   the survey. A 6pm Mountain send is already the NEXT UTC day, so without this
--   ledger the noon survey run would plan its full daily budget and meet Resend's
--   429 on its last emails. survey_email.get_send_usage counts these rows beside
--   survey_send_log, so the survey's daily and monthly allowance, the send gate
--   inside send_survey_stage, and the console's usage meter all shrink by exactly
--   the digest e-mails that went out. A row is written BEFORE the e-mail is sent
--   and removed only when Resend explicitly refuses it -- the same "claim, then
--   send" direction as survey_send_log, failing toward "counted but maybe not
--   sent" rather than "sent but not counted".
--   NO RECIPIENT ADDRESS IS STORED. The count is the whole requirement.
--
-- SECURITY: new tables -> `ENABLE ROW LEVEL SECURITY` with NO policies, the
-- deny-all lockdown every table in this schema gets (mirrors #51), and both are
-- registered in database/rls_lockdown.sql. The app connects as the table owner
-- and bypasses RLS; anon/authenticated are denied. The recipient list is staff
-- e-mail addresses -- not alumni data, but still nobody's business via the Data
-- API.
--
-- PURELY ADDITIVE: two new tables and one seeded row. No existing table is
-- touched and nothing is backfilled. Old code ignores both tables; new code on
-- the OLD schema reads the config as "no recipients" (per-posting, i.e. today's
-- behaviour) and the ledger as zero digest sends, so the deploy gap is safe in
-- either order. Ship it as a migration-only commit first anyway, per the README.
--
-- SAFE TO RE-RUN: CREATE ... IF NOT EXISTS + INSERT ... ON CONFLICT DO NOTHING;
-- the RLS enable is idempotent. Re-running will NOT reset recipients an engineer
-- has set.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS opportunity_link_digest_config (
    id                  int PRIMARY KEY DEFAULT 1,
    -- Lowercased, deduped staff addresses. Empty = no digest (per-posting fires).
    recipients          text[] NOT NULL DEFAULT '{}',
    -- Watermark: every survey posting submitted at or before this was reported.
    reported_through    timestamptz,
    -- The America/Denver date the digest last ran to completion. At most one
    -- digest per local day: a duplicated or retried cron call cannot send twice
    -- or spend the survey's quota twice.
    last_digest_on      date,
    -- Last engineer to change the recipients. Console detail only; the audit
    -- trail (rerouted to engineer_action_log, #199) is the durable record.
    updated_by_user_id  bigint,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_opportunity_link_digest_config_singleton CHECK (id = 1),
    CONSTRAINT ck_opportunity_link_digest_config_recipients_max
        CHECK (cardinality(recipients) <= 10),
    CONSTRAINT fk_opportunity_link_digest_config_updated_by
        FOREIGN KEY (updated_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL
);

-- Seed the single row: no recipients, so applying this changes nothing about
-- what is sent until an engineer adds an address.
INSERT INTO opportunity_link_digest_config (id) VALUES (1)
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS opportunity_link_digest_send_log (
    digest_send_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sent_at         timestamptz NOT NULL DEFAULT now()
);

-- get_send_usage counts this month's rows on every budget read.
CREATE INDEX IF NOT EXISTS idx_opportunity_link_digest_send_log_sent_at
    ON opportunity_link_digest_send_log (sent_at);

ALTER TABLE opportunity_link_digest_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE opportunity_link_digest_send_log ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS opportunity_link_digest_send_log;
--   DROP TABLE IF EXISTS opportunity_link_digest_config;
-- With the config gone the app reads "no recipients" and falls back to the #771
-- per-posting alert; with the ledger gone the survey budget counts survey sends
-- only, exactly as before this migration.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public'
--    AND tablename IN ('opportunity_link_digest_config',
--                      'opportunity_link_digest_send_log');
-- SELECT id, cardinality(recipients) AS recipients, reported_through,
--        last_digest_on, updated_at
--   FROM opportunity_link_digest_config;
-- SELECT date_trunc('day', sent_at) AS day, count(*)
--   FROM opportunity_link_digest_send_log GROUP BY 1 ORDER BY 1 DESC LIMIT 7;

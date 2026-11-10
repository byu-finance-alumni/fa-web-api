-- =============================================================================
-- Migration: new-people survey invites ledger (api#661 add-on)
-- Date: 2026-11-10
-- -----------------------------------------------------------------------------
-- WHY. Staff can now email the shared new-people survey link straight from the
-- New people tab (POST /survey/new-people/invite). This table is the LEDGER of
-- those emails, for two things:
--
--   1. The SHARED RESEND BUDGET. The invites spend from the same Resend account
--      as the survey and the job-posting digest, so `survey_email.get_send_usage`
--      counts one row here per invite email, exactly as it counts the digest's
--      `opportunity_link_digest_send_log`. The console meter, the cron's pacing
--      and the survey's send gate all see it.
--   2. The 30-DAY "recently invited" skip: the same address is not invited
--      twice within 30 days.
--
-- NO ADDRESSES. `email_hash` is the sha256 (hex) of the normalised address
-- (trimmed, lower-cased). The address itself is never stored: the hash is all
-- the 30-day check needs, and a ledger of who staff emailed would otherwise be a
-- second copy of PII for people who are not in the database.
--
-- Rows are written only AFTER Resend accepts the batch, one per email.
--
-- SECURITY: deny-all RLS (no policies), registered in database/rls_lockdown.sql
-- like every other table. The app connects as the table owner and bypasses RLS.
--
-- PURELY ADDITIVE: one new table, nothing existing touched, nothing backfilled.
-- The usage read is wrapped (SAVEPOINT + count-zero on failure), so code that
-- reaches an un-migrated schema still sends surveys exactly as before.
--
-- SAFE TO RE-RUN: CREATE ... IF NOT EXISTS; the RLS enable is idempotent.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS new_people_invites (
    invite_id        bigserial PRIMARY KEY,
    email_hash       text NOT NULL,
    sent_by_user_id  bigint,
    sent_at          timestamptz NOT NULL DEFAULT now(),
    resend_email_id  text,
    CONSTRAINT ck_new_people_invites_email_hash
        CHECK (email_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT fk_new_people_invites_sent_by
        FOREIGN KEY (sent_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL
);

-- The 30-day "recently invited" lookup.
CREATE INDEX IF NOT EXISTS idx_new_people_invites_hash_sent
    ON new_people_invites (email_hash, sent_at DESC);
-- The shared usage count (today / this month).
CREATE INDEX IF NOT EXISTS idx_new_people_invites_sent_at
    ON new_people_invites (sent_at);

ALTER TABLE new_people_invites ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS new_people_invites;
-- Invites already sent then stop counting toward the Resend budget for the rest
-- of their day/month, and the 30-day skip forgets them.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public' AND tablename = 'new_people_invites';
-- SELECT count(*) AS invites, max(sent_at) AS last_sent FROM new_people_invites;

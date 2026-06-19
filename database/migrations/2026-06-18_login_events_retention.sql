-- Login-events retention + forensic index (security/privacy review follow-ups,
-- issue #105).
--
-- 1. Retention: IP + approximate location are personal data; keeping them
--    forever is disproportionate for a security access log. Purge rows older
--    than 90 days via a daily pg_cron job. The named job upserts on re-run
--    (pg_cron >= 1.4), so this migration is idempotent.
-- 2. Index login_events(email): email is snapshotted so a deleted user's logins
--    stay attributable and searchable; index it for forensic lookups by email.
BEGIN;

-- pg_cron lives in the `postgres` database on Supabase; safe to enable here.
CREATE EXTENSION IF NOT EXISTS pg_cron;

CREATE INDEX IF NOT EXISTS idx_login_events_email ON login_events (email);

-- Daily at 09:00 UTC: drop login history older than 90 days. Re-running this
-- migration re-registers the SAME named job (no duplicates).
SELECT cron.schedule(
    'purge-login-events-90d',
    '0 9 * * *',
    $$DELETE FROM login_events WHERE occurred_at < now() - INTERVAL '90 days'$$
);

COMMIT;

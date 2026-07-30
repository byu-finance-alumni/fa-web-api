-- =============================================================================
-- Migration: survey send-cap config (throttle scheduled sends to a daily/monthly
--            budget so a cohort goes out ~100/day instead of all at once)
-- Date: 2026-07-29  (issue #542 follow-up)
-- -----------------------------------------------------------------------------
-- survey_send_config — a SINGLE-ROW table (id is pinned to 1) holding the
-- account-wide send cap the scheduler paces against:
--
--   enabled        — when true, the cron spends at most `daily_limit` emails per
--                    UTC day and `monthly_limit` per calendar month across ALL
--                    graduation years, spreading a large cohort over several days.
--                    Set false (e.g. after upgrading the Resend plan) to remove
--                    the internal cap entirely — sends are then limited only by
--                    Resend itself.
--   daily_limit    — emails/day budget   (default 100 = Resend Free).
--   monthly_limit  — emails/month budget (default 3000 = Resend Free).
--
-- The row is seeded here so the app can always read a config (GET falls back to
-- defaults if it is ever missing).
--
-- SECURITY: RLS enabled with NO policies (deny-all lockdown, like the other
-- survey tables). The app connects as the table owner and bypasses RLS.
--
-- SAFE TO RE-RUN: IF NOT EXISTS + ON CONFLICT DO NOTHING.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS survey_send_config (
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
    CONSTRAINT fk_survey_send_config_updated_by FOREIGN KEY (updated_by_user_id)
        REFERENCES users (user_id) ON DELETE SET NULL
);

-- Seed the single row so the app always has a config to read.
INSERT INTO survey_send_config (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

ALTER TABLE survey_send_config ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if the table must be dropped):
--   DROP TABLE IF EXISTS survey_send_config;
-- =============================================================================

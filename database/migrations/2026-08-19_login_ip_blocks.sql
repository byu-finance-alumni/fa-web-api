-- =============================================================================
-- Migration: login_ip_blocks (automatic, self-expiring block on the login path)
-- Date: 2026-08-19  (#457, follows #456)
-- -----------------------------------------------------------------------------
-- WHY THIS TABLE EXISTS. #456 shipped DETECTION for the 2026-08-19 campaigns
-- (190/68, 338/78, 222/202 -- see 2026-08-19_login_abuse_incidents.sql) and
-- deliberately blocked nothing: it told a human, who could then block the source
-- at the edge. The edge turned out not to be available -- Vercel's rate limiting
-- is a Pro feature and this account is on Hobby -- so the throttle has to live in
-- the application, and "a human blocks it at the WAF" is not a control this
-- project actually has. This table is that throttle's durable state.
--
-- WHY IT LIVES IN THE DATABASE AND NOT IN MEMORY. Identical to the argument in
-- 2026-08-18_service_incidents.sql and 2026-08-19_login_abuse_incidents.sql, and
-- this is the case that PROVES it: `app/core/rate_limit.py` is an in-memory
-- fixed-window counter, so on Vercel each warm instance keeps its own and shares
-- it with nobody. That is exactly why it never fired on 2026-08-19. "Is this
-- source blocked?" has to be a fact about the SERVICE, and the database is the
-- only durable, already-connected shared store this stack has.
--
-- -----------------------------------------------------------------------------
-- THE SAFETY PROPERTY THIS TABLE'S SHAPE ENFORCES
-- -----------------------------------------------------------------------------
-- A block is far more consequential than an alert. An alert that misfires costs
-- one Slack message; a block that misfires locks the department out of its own
-- system. Three of the safety properties are therefore structural here rather
-- than conventions in Python:
--
--   1. IT EXPIRES BY BEING READ, NOT BY BEING CLEANED UP. `blocked_until` is a
--      timestamp and every read carries `AND blocked_until > now()`. Nothing has
--      to run for a block to lapse -- no cron, no sweep, no engineer. If every
--      other moving part in this feature stops working, every block still ends.
--      There is NO representation for a permanent block: the column is NOT NULL
--      and a CHECK constraint caps any single block at 24 hours.
--
--   2. A LIFT IS PERMANENT-ISH, NOT A SUGGESTION. `lifted_at` takes the row out
--      of the partial unique index below, and the service refuses to open a new
--      block for a source lifted in the last 24 hours. An engineer who decides a
--      block was wrong must not watch it snap back on the attacker's -- or the
--      victim's -- next failed login.
--
--   3. THE EXEMPTIONS ARE EVALUATED BY POSTGRES, INSIDE THE INSERT. The service
--      creates a block with `INSERT ... SELECT ... WHERE NOT EXISTS (<recent
--      successful login from this IP>) AND NOT EXISTS (<any engineer sign-in
--      from this IP, ever>)`. There is no code path that writes this table
--      without those two clauses, because they are part of the only statement
--      that writes it. See app/services/login_block.py.
--
-- ⚠️ WHY (3) IS THE MOST IMPORTANT LINE IN THIS FILE. `ip_address` is copied
-- from `login_failures`, which the frontend populates from the incoming
-- request's `x-forwarded-for`. ANYONE CALLING THIS API DIRECTLY CAN PUT ANYTHING
-- THERE. Without the successful-login exemption, an attacker could put the
-- owner's address in that header, fail eight sign-ins, and lock the staff out of
-- their own system -- turning a failed attack into a successful denial of
-- service, which is strictly worse than the attack this feature exists to stop.
-- The exemption reads `login_events`, a table only a genuinely AUTHENTICATED
-- caller can write (POST /auth/login requires a valid Supabase token), so the
-- shield is not forgeable by the same party that can forge the block.
--
-- -----------------------------------------------------------------------------
-- NO PII, EVER
-- -----------------------------------------------------------------------------
-- Same rule as login_abuse_incidents, for the same reason: this row's contents
-- reach a Slack channel. It stores a source IP, COUNTS, and a fixed pattern
-- string. It does NOT store the attempted email addresses -- those are
-- unverified strings a stranger typed, some belong to real people, and a list of
-- them is both the attacker's scraped material and an enumeration oracle. They
-- stay in `login_failures`, behind the engineer console.
--
-- SECURITY: new table -> `ENABLE ROW LEVEL SECURITY` with NO policies, the
-- deny-all lockdown every table in this schema gets (mirrors #51). The app
-- connects as the table owner and bypasses RLS; anon/authenticated are denied.
--
-- SAFE TO RE-RUN: IF NOT EXISTS on the table and all indexes; the RLS enable is
-- idempotent in Postgres.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS login_ip_blocks (
    block_id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Which deployment blocked it ('production' / 'development'). Dev and prod
    -- have separate databases, but preview deployments share the dev one, so the
    -- active-block uniqueness below is scoped per environment as well as per
    -- source, exactly like service_incidents and login_abuse_incidents.
    environment          varchar(40)  NOT NULL,
    -- The blocked source. varchar(64) matches login_failures.ip_address (wide
    -- enough for IPv6 with a zone id). ⚠️ CLIENT-SUPPLIED -- see the header.
    ip_address           varchar(64)  NOT NULL,
    -- When the CURRENT block period began. Re-armed in place if the source is
    -- still abusive when the block is re-evaluated, so this is "most recently
    -- blocked at", not "first ever seen" — which is also what keeps
    -- ck_login_ip_blocks_bounded below satisfiable for a very long campaign.
    blocked_at           timestamptz  NOT NULL DEFAULT now(),
    -- THE EXPIRY. NOT NULL on purpose: there is no way to spell "forever" in
    -- this table. Every read of a block carries `AND blocked_until > now()`, so
    -- a block lapses because time passed, not because anything ran.
    blocked_until        timestamptz  NOT NULL,
    -- Why, snapshotted at the moment of the decision. These are the SAME numbers
    -- the incident row and the Slack alert carry, taken from the SAME aggregate,
    -- so the console, the message and the block can never describe one source
    -- three different ways.
    attempt_count        integer      NOT NULL DEFAULT 0,
    distinct_email_count integer      NOT NULL DEFAULT 0,
    -- One of login_abuse.classify's fixed strings ('enumeration: ...',
    -- 'spraying: ...', 'guessing: ...'). Never anything derived from input.
    pattern              varchar(64),
    -- The login_abuse_incidents row this block was opened alongside, when there
    -- was one. Nullable rather than a FK: blocking does not depend on alerting
    -- being configured (a forgotten webhook must never silently disable a
    -- security control), so a block can legitimately exist with no incident row,
    -- and a FK would make the protection depend on the observability.
    abuse_incident_id    bigint,
    -- Set when an engineer lifts the block by hand. Takes the row OUT of the
    -- partial unique index below, and the service will not open a new block for
    -- the same source for 24 hours afterwards -- a human override outranks the
    -- heuristic that produced the block.
    lifted_at            timestamptz,
    lifted_by_user_id    bigint REFERENCES users(user_id) ON DELETE SET NULL,
    created_at           timestamptz  NOT NULL DEFAULT now(),
    updated_at           timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ck_login_ip_blocks_counts
        CHECK (attempt_count >= 0 AND distinct_email_count >= 0),
    -- A block must end after it began ...
    CONSTRAINT ck_login_ip_blocks_expiry_after_start
        CHECK (blocked_until > blocked_at),
    -- ... and it must not last longer than a day, whatever a future caller
    -- passes. The service asks for one hour; this is the database refusing to
    -- store the mistake that would turn a false positive into a real outage.
    CONSTRAINT ck_login_ip_blocks_bounded
        CHECK (blocked_until <= blocked_at + interval '24 hours')
);

-- THE dedup constraint: at most one ACTIVE (un-lifted) block row per source per
-- environment. This is what makes the service's
-- `INSERT ... ON CONFLICT (environment, ip_address) WHERE lifted_at IS NULL DO
-- UPDATE` collapse twenty concurrent serverless instances into one row instead
-- of twenty, and it is the index that ON CONFLICT clause infers.
--
-- Note it does NOT mention `blocked_until`: an index predicate must be
-- immutable, so "active" cannot mean "not yet expired" here. An EXPIRED row
-- stays in the index and is re-armed in place by the DO UPDATE, which is the
-- behaviour we want -- one row per source, carrying its history, rather than a
-- new row per campaign.
CREATE UNIQUE INDEX IF NOT EXISTS uq_login_ip_blocks_active
    ON login_ip_blocks (environment, ip_address)
    WHERE lifted_at IS NULL;

-- The engineer console lists blocks newest-first.
CREATE INDEX IF NOT EXISTS idx_login_ip_blocks_blocked_at
    ON login_ip_blocks (blocked_at DESC);

-- The lift-grace check reads lifted rows for one source; they are excluded from
-- the unique index above, so they need their own.
CREATE INDEX IF NOT EXISTS idx_login_ip_blocks_lifted
    ON login_ip_blocks (environment, ip_address, lifted_at DESC)
    WHERE lifted_at IS NOT NULL;

ALTER TABLE login_ip_blocks ENABLE ROW LEVEL SECURITY;

-- THE SHIELD'S INDEX. The successful-login exemption is a `NOT EXISTS` over
-- `login_events` keyed on ip_address inside a time window, evaluated inside the
-- INSERT that creates a block -- i.e. on an unauthenticated public route, under
-- exactly the flood it is there to police. Without this it is a sequential scan
-- of the whole sign-in history every time a source crosses the threshold.
CREATE INDEX IF NOT EXISTS idx_login_events_ip_occurred
    ON login_events (ip_address, occurred_at DESC);

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS login_ip_blocks;
--   DROP INDEX IF EXISTS idx_login_events_ip_occurred;
-- Dropping the table disables blocking entirely and FAILS OPEN: every read in
-- app/services/login_block.py is wrapped so an unreadable store means "not
-- blocked", so logins continue exactly as they did before #457.
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public' AND tablename = 'login_ip_blocks';
-- SELECT indexname FROM pg_indexes
--  WHERE schemaname = 'public'
--    AND indexname IN ('uq_login_ip_blocks_active', 'idx_login_events_ip_occurred');
-- -- Active blocks right now (this is also what GET /admin/login-ip-blocks shows):
-- SELECT ip_address, blocked_at, blocked_until, distinct_email_count, pattern
--   FROM login_ip_blocks
--  WHERE lifted_at IS NULL AND blocked_until > now()
--  ORDER BY blocked_at DESC;

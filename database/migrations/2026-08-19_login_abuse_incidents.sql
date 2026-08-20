-- =============================================================================
-- Migration: login_abuse_incidents (brute-force detection — one alert per source)
-- Date: 2026-08-19  (#456)
-- -----------------------------------------------------------------------------
-- WHY THIS TABLE EXISTS. On 2026-08-19 three sources hammered the production
-- login endpoint: 190 attempts across 68 addresses over ten minutes, 338 across
-- 78 over six minutes, and 222 across 202 in SIXTEEN SECONDS. Nothing succeeded,
-- and nothing told anybody -- the owner found out because he happened to look at
-- `login_failures`. The fix is a message when a single source starts guessing.
--
-- The hard part is not noticing (the rows are already there, see
-- `2026-07-16_add_login_failures.sql`); it is sending exactly ONE message for a
-- campaign of 750 attempts rather than 750 of them.
--
-- WHY IT LIVES IN THE DATABASE AND NOT IN MEMORY. Identical to the argument in
-- `2026-08-18_service_incidents.sql`: the API runs on Vercel serverless, there is
-- no long-lived process, and a module-level flag dies with the invocation and is
-- shared with none of the other instances handling the same flood. "Have we
-- already reported this source?" has to be a fact about the SERVICE, and the
-- database is the only durable, already-connected shared store this stack has.
--
-- WHAT AN "INCIDENT" IS -- this table's design is the definition. An incident is
-- ONE CAMPAIGN FROM ONE SOURCE, represented by exactly one row with
-- `resolved_at IS NULL` for that (environment, ip_address). It is created only
-- once a source crosses a threshold (see app/services/login_abuse.py: eight
-- distinct addresses, or thirty attempts, inside a fifteen-minute window), it
-- reports ONCE (`alert_sent_at`), and it closes after an hour of silence so the
-- same address returning next week is reported again. Attempts do not create
-- rows; they update the counters on the open one. A row in this table therefore
-- always means something happened -- honest typos never appear here.
--
-- THE PARTIAL UNIQUE INDEX IS THE LOAD-BEARING PART. `uq_login_abuse_open`
-- allows at most ONE open row per (environment, ip_address), which is what makes
-- the service's `INSERT ... ON CONFLICT ... DO UPDATE` collapse twenty concurrent
-- instances into one row instead of twenty. The `alert_sent_at` claim then works
-- the same way as the incident alerter's: `UPDATE ... WHERE alert_sent_at IS NULL
-- RETURNING` can be won by exactly one transaction under READ COMMITTED. Drop
-- this index and the feature silently degrades to one message per instance --
-- which is the flood the table exists to prevent.
--
-- NO PII, EVER. This table's contents are emailed off-platform and posted into a
-- Slack channel. It stores a source IP, a coarse IP-geolocation, COUNTS, and a
-- time window. It deliberately does NOT store the attempted email addresses:
-- those are unverified strings typed by a stranger, some of them belong to real
-- people, and a list of them is the scraped-and-guessed material the attacker was
-- probing with. They stay in `login_failures`, behind the engineer console.
--
-- ⚠️ `ip_address` IS CLIENT-SUPPLIED. It is copied from `login_failures`, which
-- the frontend populates from the incoming request's `x-forwarded-for`. Anyone
-- calling the API directly can put anything there. It is still the only
-- per-attacker identifier this data has (the server-derived key is the frontend
-- function's egress address, shared by every real login in the organisation), so
-- it is used -- but the value is a LEAD, not a verdict. Verify against the edge's
-- own logs before blocking on it.
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

CREATE TABLE IF NOT EXISTS login_abuse_incidents (
    abuse_incident_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Which deployment saw it ('production' / 'development'). Dev and prod have
    -- separate databases, but preview deployments share the dev one, so the
    -- open-incident uniqueness below is scoped per environment as well as per
    -- source, exactly like service_incidents.
    environment          varchar(40)  NOT NULL,
    -- The source. varchar(64) matches login_failures.ip_address (wide enough for
    -- IPv6 with a zone id).
    ip_address           varchar(64)  NOT NULL,
    -- Earliest and latest failure attributed to this campaign. `last_seen_at` is
    -- what "has the source gone quiet?" is measured from.
    started_at           timestamptz  NOT NULL DEFAULT now(),
    last_seen_at         timestamptz  NOT NULL DEFAULT now(),
    -- High-water marks, not running totals: the service measures over a ROLLING
    -- window and folds the result in with GREATEST, so a campaign longer than the
    -- window cannot make the recorded numbers go backwards.
    attempt_count        integer      NOT NULL DEFAULT 0,
    distinct_email_count integer      NOT NULL DEFAULT 0,
    -- The measurement window those two counts were taken over, in seconds, so a
    -- message can say "N attempts in the last 15 minutes" and stay true if the
    -- constant is ever retuned.
    window_seconds       integer      NOT NULL,
    -- Coarse IP geolocation forwarded alongside the attempt. Nullable: absent in
    -- local dev and whenever the client forwards no context. Sized to match
    -- login_failures.
    city                 varchar(128),
    region               varchar(128),
    country              varchar(64),
    -- The SHAPE of the campaign ('enumeration: ...', 'spraying: ...',
    -- 'guessing: ...'), so the reader knows what they are looking at. One of a
    -- fixed set of strings written by the service; never anything derived from
    -- input.
    pattern              varchar(64),
    -- Set when the message is claimed, before it is sent. Claim-then-send mirrors
    -- the survey sender and the incident alerter: a claim is repeatable and a
    -- Slack post is not, so both fail toward "possibly not sent" rather than
    -- "sent 750 times".
    alert_sent_at        timestamptz,
    -- NULL = open. Exactly one row per (environment, ip_address) may be NULL here.
    resolved_at          timestamptz,
    created_at           timestamptz  NOT NULL DEFAULT now(),
    updated_at           timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ck_login_abuse_counts
        CHECK (attempt_count >= 0 AND distinct_email_count >= 0),
    CONSTRAINT ck_login_abuse_resolved_after_start
        CHECK (resolved_at IS NULL OR resolved_at >= started_at)
);

-- THE dedup constraint: at most one OPEN incident per source per environment.
-- This is what makes "one alert per campaign" true across concurrent serverless
-- instances, and what the service's ON CONFLICT clause infers.
CREATE UNIQUE INDEX IF NOT EXISTS uq_login_abuse_open
    ON login_abuse_incidents (environment, ip_address)
    WHERE resolved_at IS NULL;

-- History browsing ("who has been probing us this month").
CREATE INDEX IF NOT EXISTS idx_login_abuse_started_at
    ON login_abuse_incidents (started_at DESC);

ALTER TABLE login_abuse_incidents ENABLE ROW LEVEL SECURITY;

-- The detector's only read is a single aggregate over ONE ip_address inside a
-- fifteen-minute window. `idx_login_failures_occurred_at` (time only) would make
-- that a scan of every failure in the window across every source; this composite
-- makes it an index-only range read on the one source being evaluated. It runs on
-- an unauthenticated public route, so it has to stay cheap under exactly the
-- flood it is there to detect.
CREATE INDEX IF NOT EXISTS idx_login_failures_ip_occurred
    ON login_failures (ip_address, occurred_at DESC);

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if this must be undone):
--   DROP TABLE IF EXISTS login_abuse_incidents;
--   DROP INDEX IF EXISTS idx_login_failures_ip_occurred;
-- =============================================================================

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT tablename, rowsecurity FROM pg_tables
--  WHERE schemaname = 'public' AND tablename = 'login_abuse_incidents';
-- SELECT indexname FROM pg_indexes
--  WHERE schemaname = 'public'
--    AND indexname IN ('uq_login_abuse_open', 'idx_login_failures_ip_occurred');

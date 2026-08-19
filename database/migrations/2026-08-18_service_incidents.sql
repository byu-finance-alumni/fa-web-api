-- =============================================================================
-- Migration: service_incidents (API failure alerting — one email per incident)
-- Date: 2026-08-18  (#444)
-- -----------------------------------------------------------------------------
-- WHY THIS TABLE EXISTS. On 2026-08-18 the API returned 500s for every request
-- for several minutes and nothing told anyone; a human opened the site and
-- noticed. The fix is an email to the engineer when the API is failing — but an
-- outage produces thousands of errors a minute, so the hard part is not
-- detecting failure, it is sending exactly ONE email for it.
--
-- WHY IT LIVES IN THE DATABASE AND NOT IN MEMORY. The API runs on Vercel
-- serverless. There is no long-lived process: a module-level counter dies with
-- the invocation and is not shared with the other instances handling the same
-- flood (this is the same caveat `app/core/rate_limit.py` carries, and there it
-- only costs accuracy — here it would cost one email per instance per burst).
-- "Have we already alerted for this incident?" therefore has to be a fact about
-- the SERVICE, not about a process, and the database is the only durable,
-- already-connected shared store this stack has.
--
-- WHAT AN "INCIDENT" IS — this table's whole design is the definition. An
-- incident is ONE CONTIGUOUS PERIOD OF FAILURE, represented by exactly one row
-- with `resolved_at IS NULL`. It opens on the first server error seen after a
-- healthy period, it does NOT page anyone until the failure is SUSTAINED
-- (`alert_sent_at`), and it closes when the API has gone quiet of failures again
-- (`resolved_at`, plus one recovery email — `recovery_sent_at`). Errors do not
-- create incidents; they increment `failure_count` on the open one.
--
-- THE PARTIAL UNIQUE INDEX IS THE LOAD-BEARING PART. `uq_service_incidents_open`
-- allows at most ONE open row per environment, so when twenty concurrent
-- serverless instances all observe the outage and all try to open an incident,
-- Postgres lets exactly one INSERT land and the other nineteen hit
-- ON CONFLICT DO NOTHING. The two `*_sent_at` claims work the same way: the
-- sender sets the column with `... WHERE alert_sent_at IS NULL RETURNING`, and
-- under READ COMMITTED exactly one concurrent UPDATE can match the row. Delete
-- the index and this whole feature silently becomes "one email per instance".
--
-- NO PII, EVER. This table's contents are emailed off-platform. It stores route
-- TEMPLATES (`/alumni/{alumni_id}`), never the request path with its ids, never
-- query strings, never request bodies, and never an alumni field. The columns
-- are sized so nothing large can be persisted here by an error message.
--
-- SECURITY: new table -> `ENABLE ROW LEVEL SECURITY` with NO policies, the
-- deny-all lockdown every table in this schema gets (mirrors #51). The app
-- connects as the table owner and bypasses RLS; anon/authenticated are denied.
--
-- SAFE TO RE-RUN: IF NOT EXISTS on the table and both indexes; the RLS enable is
-- idempotent in Postgres.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS service_incidents (
    incident_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Which deployment is failing ('production' / 'development'). Dev and prod
    -- have separate databases, but preview deployments share the dev one, so the
    -- open-incident uniqueness below is scoped per environment rather than
    -- global.
    environment      varchar(40)  NOT NULL,
    -- First failure of this incident, and the most recent one. `last_failure_at`
    -- is what "has it gone quiet?" is measured from, for both the recovery email
    -- and for reaping an incident nobody ever closed.
    started_at       timestamptz  NOT NULL DEFAULT now(),
    last_failure_at  timestamptz  NOT NULL DEFAULT now(),
    -- Server errors OBSERVED AND REPORTED for this incident. Not the true error
    -- count: each instance throttles itself to roughly one report every few
    -- seconds so a flood cannot become a write storm against the database that
    -- may itself be the thing failing. It is a floor, and the email says so.
    failure_count    integer      NOT NULL DEFAULT 1,
    -- Route TEMPLATES only (`/alumni/{alumni_id}`), never a real path.
    first_path       varchar(200),
    last_path        varchar(200),
    -- Representative status code (500/502/503/504) and exception class name
    -- (e.g. 'ProgrammingError') — the two most useful diagnostic facts that
    -- carry no data. Never the exception message: that can quote a row.
    status_code      integer,
    error_kind       varchar(100),
    -- Set when the OPENING email is claimed, before it is sent. Claim-then-send
    -- mirrors the survey sender: a claim is reversible and an email is not, so
    -- both fail toward "possibly not sent" rather than "sent twice". For an
    -- alert that trade is deliberate — a missed alert is a bug in one incident,
    -- a duplicated alert is the flood this whole table exists to prevent.
    alert_sent_at    timestamptz,
    -- Set when the RECOVERY email is claimed. NULL on a resolved incident means
    -- it never paged anyone (a blip), so there was nothing to clear.
    recovery_sent_at timestamptz,
    -- NULL = open. Exactly one row per environment may be NULL here.
    resolved_at      timestamptz,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ck_service_incidents_failure_count CHECK (failure_count >= 0),
    -- A resolved incident cannot end before it began.
    CONSTRAINT ck_service_incidents_resolved_after_start
        CHECK (resolved_at IS NULL OR resolved_at >= started_at)
);

-- THE dedup constraint: at most one OPEN incident per environment. This is what
-- makes "one alert per incident" true across concurrent serverless instances.
CREATE UNIQUE INDEX IF NOT EXISTS uq_service_incidents_open
    ON service_incidents (environment)
    WHERE resolved_at IS NULL;

-- History browsing / cleanup ("what broke last month").
CREATE INDEX IF NOT EXISTS idx_service_incidents_started_at
    ON service_incidents (started_at DESC);

ALTER TABLE service_incidents ENABLE ROW LEVEL SECURITY;

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand if the table must be dropped):
--   DROP TABLE IF EXISTS service_incidents;
-- =============================================================================

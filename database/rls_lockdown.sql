-- =============================================================================
-- rls_lockdown.sql — deny-all Row-Level Security for every public table
-- =============================================================================
-- WHY: Supabase auto-exposes every table in the `public` schema through its
-- REST Data API, reachable with the *publishable/anon key that ships in the
-- frontend bundle*. RLS is the only thing standing between that public key and
-- this data. This project routes ALL data access through the FastAPI backend,
-- so no table should be reachable via the Data API at all.
--
-- HOW IT WORKS: Enabling RLS with NO policies = deny-all for the API roles
-- (`anon`, `authenticated`). The backend is UNAFFECTED because it connects with
-- a privileged Postgres role (the tables' owner, which additionally carries
-- BYPASSRLS) that is not subject to row security. Note this file deliberately
-- does NOT set FORCE ROW LEVEL SECURITY, which is what would change that.
--
-- The frontend never queries these tables directly (it uses Supabase only for
-- auth, in the separate `auth` schema), so this does not break the app.
--
-- SAFE TO RE-RUN: every statement below is idempotent.
--
-- =============================================================================
-- ⚠️ 2026-08-07 (#424): THIS FILE NOW HAS TWO LAYERS — A LIST AND A SWEEP.
-- =============================================================================
-- It used to enumerate ~45 tables by name, with a note asking whoever adds a
-- table to remember to add it here too. That list had silently fallen three
-- tables behind reality (`city_geo`, `dashboard_presets`, `schema_migrations`),
-- and the one of those that was ALSO missing from every other source —
-- `schema_migrations`, created ad hoc by migrate.sh's own bootstrap statement —
-- was the single table in the database running without RLS.
--
-- A list that must be remembered is the bug — so the SWEEP below is now the
-- operational source of truth, and it cannot miss a table.
--
-- ⚠️ BUT THE EXPLICIT LIST STAYS, AND MUST. Deleting it broke CI (2026-08-07):
-- `scripts/ferpa_check.py` proves the invariant by matching every
-- `CREATE TABLE` in ./schema.sql against an `ENABLE ROW LEVEL SECURITY` target
-- in THIS file. A `DO` block is opaque to that check, so with only the sweep
-- present the guard went blind and reported all 51 tables as unprotected.
--
-- The two do different jobs and neither replaces the other:
--
--   * The LIST is the declarative, statically-checkable claim. It is what
--     catches a table added to schema.sql and never locked down — at review
--     time, before it ships.
--   * The SWEEP catches what the list structurally cannot: a table that exists
--     in the database but in no schema file. That is precisely how
--     `schema_migrations` hid.
--
-- Keep both in sync with reality. If you add a table, add it to the list; the
-- sweep is the backstop for when someone doesn't.
-- =============================================================================



-- =============================================================================
-- THE EXPLICIT LIST — every table declared in ./schema.sql.
-- =============================================================================
-- Statically checked by scripts/ferpa_check.py, which fails CI if a table in
-- schema.sql has no line here. That check is why this list exists: it catches
-- an unprotected new table at review time. Idempotent — ENABLE ROW LEVEL
-- SECURITY is a no-op when it is already on.
-- =============================================================================

ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_failures ENABLE ROW LEVEL SECURITY;
ALTER TABLE roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE role_capabilities ENABLE ROW LEVEL SECURITY;
ALTER TABLE data_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE import_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni_contact_info ENABLE ROW LEVEL SECURITY;
ALTER TABLE current_employment ENABLE ROW LEVEL SECURITY;
ALTER TABLE education_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE employment_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE verification_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni_engagement ENABLE ROW LEVEL SECURITY;
ALTER TABLE research_tracking ENABLE ROW LEVEL SECURITY;
ALTER TABLE tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni_tags ENABLE ROW LEVEL SECURITY;
ALTER TABLE status_labels ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni_status_labels ENABLE ROW LEVEL SECURITY;
ALTER TABLE vocabulary_terms ENABLE ROW LEVEL SECURITY;
ALTER TABLE support_contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE interactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE follow_up_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_attendance ENABLE ROW LEVEL SECURITY;
ALTER TABLE donations ENABLE ROW LEVEL SECURITY;
ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE surveys ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_schedule ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_send_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_reset_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_campaign_retirement ENABLE ROW LEVEL SECURITY;
ALTER TABLE survey_send_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE maintenance_mode ENABLE ROW LEVEL SECURITY;
ALTER TABLE attachments ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_logs ENABLE ROW LEVEL SECURITY;
ALTER TABLE engineer_action_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE duplicate_candidates ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni_program_engagement ENABLE ROW LEVEL SECURITY;
ALTER TABLE alumni_mentor_industries ENABLE ROW LEVEL SECURITY;
ALTER TABLE nettrek_hosting ENABLE ROW LEVEL SECURITY;
ALTER TABLE conference_participation ENABLE ROW LEVEL SECURITY;
ALTER TABLE finance_society_leadership ENABLE ROW LEVEL SECURITY;
ALTER TABLE bbq_attendance ENABLE ROW LEVEL SECURITY;
ALTER TABLE city_geo ENABLE ROW LEVEL SECURITY;
ALTER TABLE dashboard_presets ENABLE ROW LEVEL SECURITY;
ALTER TABLE opportunity_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE service_incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_abuse_incidents ENABLE ROW LEVEL SECURITY;
ALTER TABLE login_ip_blocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE alert_delivery_config ENABLE ROW LEVEL SECURITY;

-- =============================================================================
-- THE SWEEP — enable RLS on every public table that does not already have it.
-- =============================================================================
-- Reads pg_class rather than pg_tables so it can also filter on `relkind` and on
-- extension membership:
--
--   * relkind IN ('r','p') — ordinary and partitioned tables, the two kinds that
--     accept RLS. Foreign tables ('f') do not support it. VIEWS are not covered
--     here and are their own exposure path (a view runs as its owner unless it
--     is declared `security_invoker`, so it can read straight past the RLS on
--     its base tables) — there are currently NONE in `public`, and if one is
--     ever added it needs a deliberate decision, not a sweep.
--   * NOT relrowsecurity — skip tables already locked down, so a re-run does not
--     take an ACCESS EXCLUSIVE lock on all ~50 tables to change nothing.
--   * deptype <> 'e' — skip tables belonging to an extension (none today; all
--     extensions live in `extensions`/`pg_catalog`). We do not own those, so a
--     future `CREATE EXTENSION ... WITH SCHEMA public` would otherwise abort the
--     whole sweep on a permission error.
--
-- `oid::regclass` renders an already-quoted, schema-qualified-if-needed
-- identifier, so interpolating it with %s is safe.
--
-- If the SQL editor errors on the $$ delimiters, run it through psql instead.
-- =============================================================================

DO $rls$
DECLARE
    r record;
    n int := 0;
BEGIN
    FOR r IN
        SELECT c.oid::regclass AS tbl
          FROM pg_class c
          JOIN pg_namespace ns ON ns.oid = c.relnamespace
         WHERE ns.nspname = 'public'
           AND c.relkind IN ('r', 'p')
           AND NOT c.relrowsecurity
           AND NOT EXISTS (
                 SELECT 1
                   FROM pg_depend d
                  WHERE d.classid = 'pg_class'::regclass
                    AND d.objid   = c.oid
                    AND d.deptype = 'e'
               )
         ORDER BY 1
    LOOP
        RAISE NOTICE 'Enabling RLS on %', r.tbl;
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY;', r.tbl);
        n := n + 1;
    END LOOP;
    RAISE NOTICE 'RLS lockdown: % table(s) changed, the rest were already on.', n;
END
$rls$;


-- =============================================================================
-- REVOKE the API roles' remaining table privileges (defense in depth).
-- =============================================================================
-- ⚠️ PARTIALLY IN EFFECT ALREADY — this block used to be presented as an
-- optional extra that had not been done. Checked live on dev 2026-08-07, the
-- data half of it HAS been applied and the rest has not:
--
--   * `anon` and `authenticated` hold NO SELECT / INSERT / UPDATE / DELETE on
--     ANY table in `public`, and the schema's default privileges grant them none
--     on new tables either. This — not RLS — is why the Data API currently
--     returns nothing: there is no privilege for a missing policy to let
--     through. RLS is the layer that still holds if these grants ever come back
--     (a stray GRANT, a Supabase default change, a restore from a dump).
--
--   * They DO still hold TRUNCATE, REFERENCES and TRIGGER (plus MAINTAIN on
--     PG17) on all 50 tables — Supabase's original blanket grant, never revoked.
--     PostgREST only ever issues SELECT/INSERT/UPDATE/DELETE and RPC, so none of
--     these is reachable through the Data API today. TRUNCATE is the one worth
--     removing anyway: **RLS does not restrict TRUNCATE**, so it is precisely
--     the privilege the lockdown above would NOT contain if it ever did become
--     reachable.
--
-- Running this completes the revoke. It touches `anon` and `authenticated` only;
-- `service_role` and the backend's own role keep everything, so nothing in the
-- application is affected. Re-grant explicitly if a direct Data API path is ever
-- wanted.
--
-- CAVEAT — the ALTER DEFAULT PRIVILEGES line only governs objects created by the
-- role that runs it (i.e. `postgres`, which owns every table here). Supabase
-- keeps its OWN default-privilege entry for `public` under `supabase_admin`, and
-- that one still grants anon/authenticated full DML. A table created in `public`
-- by supabase_admin (the SQL editor / dashboard runs as a different role than
-- migrate.sh) would therefore arrive readable. The sweep above is what covers
-- that case, which is the whole reason it is the source of truth and this block
-- is only defense in depth.
-- =============================================================================

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;


-- =============================================================================
-- VERIFY — run these after the sweep.
-- =============================================================================
-- 1. Every public table has row security on. Should return ZERO rows.
--
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public' AND NOT rowsecurity
-- ORDER BY tablename;
--
-- 2. The API roles hold no table privileges at all. Should return ZERO rows.
--
-- SELECT grantee, table_name, privilege_type
-- FROM information_schema.role_table_grants
-- WHERE table_schema = 'public' AND grantee IN ('anon', 'authenticated')
-- ORDER BY grantee, table_name, privilege_type;
--
-- 3. No views have appeared in `public` (they are not covered by the sweep —
--    see the note above). Should return ZERO rows.
--
-- SELECT c.relname, c.relkind
-- FROM pg_class c JOIN pg_namespace ns ON ns.oid = c.relnamespace
-- WHERE ns.nspname = 'public' AND c.relkind IN ('v', 'm')
-- ORDER BY 1;

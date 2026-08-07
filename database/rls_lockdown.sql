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
-- ⚠️ 2026-08-07 (#424): THIS FILE NO LONGER CARRIES A HAND-MAINTAINED TABLE LIST.
-- =============================================================================
-- It used to enumerate ~45 tables by name, with a note asking whoever adds a
-- table to remember to add it here too. That list had silently fallen three
-- tables behind reality (`city_geo`, `dashboard_presets`, `schema_migrations`),
-- and the one of those that was ALSO missing from every other source —
-- `schema_migrations`, created ad hoc by migrate.sh's bootstrap CREATE TABLE —
-- was the single table in the database running without RLS.
--
-- A list that must be remembered is the bug. The sweep below cannot miss a
-- table, so it, not a list, is now the source of truth. The full table inventory
-- lives in ./schema.sql, which is the file that is supposed to describe the
-- schema; this file only enforces a property over whatever is actually there.
--
-- RUN THIS after any schema change. There is nothing to keep in sync.
-- =============================================================================


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

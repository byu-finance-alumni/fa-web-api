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
-- a privileged Postgres role (and the service-role key) that bypasses RLS.
--
-- The frontend never queries these tables directly (it uses Supabase only for
-- auth, in the separate `auth` schema), so this does not break the app.
--
-- SAFE TO RE-RUN: ENABLE ROW LEVEL SECURITY is idempotent.
-- RE-RUN AFTER SCHEMA CHANGES: when new tables are added, add them here (or use
-- the dynamic block at the bottom) so they don't ship unprotected.
-- =============================================================================

-- Identity / access ----------------------------------------------------------
ALTER TABLE public.users                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.roles                 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_roles            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.login_attempts        ENABLE ROW LEVEL SECURITY;

-- Provenance -----------------------------------------------------------------
ALTER TABLE public.data_sources          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.import_batches        ENABLE ROW LEVEL SECURITY;

-- Alumni core ----------------------------------------------------------------
ALTER TABLE public.alumni                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alumni_contact_info   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.current_employment    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.education_history      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.employment_history    ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.verification_log      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alumni_engagement     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.research_tracking     ENABLE ROW LEVEL SECURITY;

-- Tags & status labels -------------------------------------------------------
ALTER TABLE public.tags                  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alumni_tags           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.status_labels         ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alumni_status_labels  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.vocabulary_terms      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.support_contacts      ENABLE ROW LEVEL SECURITY;

-- Engagement -----------------------------------------------------------------
ALTER TABLE public.interactions          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.follow_up_tasks       ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.events                ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_attendance      ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.surveys               ENABLE ROW LEVEL SECURITY;

-- Files, audit, duplicates ---------------------------------------------------
ALTER TABLE public.attachments           ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_logs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.duplicate_candidates  ENABLE ROW LEVEL SECURITY;

-- Program engagement ---------------------------------------------------------
ALTER TABLE public.alumni_program_engagement  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.alumni_mentor_industries   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.nettrek_hosting            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.conference_participation   ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.finance_society_leadership ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.bbq_attendance             ENABLE ROW LEVEL SECURITY;

-- =============================================================================
-- VERIFY — every row should show rowsecurity = true. Anything `false` is
-- exposed through the Data API.
-- =============================================================================
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public'
-- ORDER BY rowsecurity, tablename;

-- =============================================================================
-- OPTIONAL EXTRA HARDENING — revoke table privileges from the API roles too
-- (defense in depth). Only run this if you're certain nothing should ever read
-- these tables via the Supabase API. Re-grant if you later add a direct path.
-- =============================================================================
-- REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon, authenticated;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM anon, authenticated;

-- =============================================================================
-- DYNAMIC ALTERNATIVE — enable RLS on EVERY current public table in one shot.
-- Handy after the schema is revised (catches new tables automatically). If the
-- SQL editor errors on the $$ delimiters, just use the explicit list above.
-- =============================================================================
-- DO $rls$
-- DECLARE r record;
-- BEGIN
--   FOR r IN
--     SELECT tablename FROM pg_tables WHERE schemaname = 'public'
--   LOOP
--     EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY;', r.tablename);
--   END LOOP;
-- END
-- $rls$;

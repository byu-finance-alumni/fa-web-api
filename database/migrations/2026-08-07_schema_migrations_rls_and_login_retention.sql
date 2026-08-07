-- =============================================================================
-- Migration: deny-all RLS on `schema_migrations` (#424) + a retention index on
-- `login_attempts` (#423)
-- Date: 2026-08-07
-- -----------------------------------------------------------------------------
-- PART 1 (#424) -- `schema_migrations` is the ONE public table with RLS off.
--
-- Every other table in the `public` schema has row security enabled (verified
-- live against dev: 49 of 50 tables `rowsecurity = t`, this one `f`). It is the
-- odd one out for a structural reason: it is not declared in `../schema.sql` or
-- `../rls_lockdown.sql` at all -- `../migrate.sh` creates it ad hoc with a
-- bootstrap `CREATE TABLE IF NOT EXISTS` before it applies anything, so it never
-- passed through the place where the RLS rule is applied. This closes that gap
-- so the invariant "every public table has RLS" is true without exception.
--
-- PROPORTION -- this is a BROKEN INVARIANT, NOT A LIVE EXPOSURE. The table is
-- not reachable through the Supabase Data API today: `anon` and `authenticated`
-- hold NO SELECT/INSERT/UPDATE/DELETE on any table in `public` (also verified
-- live), so there is nothing for a missing RLS policy to let through, and the
-- contents are a list of migration filenames, not data. What is fixed here is
-- the defence-in-depth layer that is supposed to hold even if those grants are
-- ever restored -- which is exactly the kind of change a future Supabase default
-- or a stray `GRANT` could make silently.
--
-- The backend is UNAFFECTED, and so is `migrate.sh`. Both connect as the table's
-- owner (`postgres`, which additionally carries BYPASSRLS), and an owner is not
-- subject to its own table's row security unless FORCE ROW LEVEL SECURITY is
-- set -- which this deliberately does NOT set. Enabling RLS with no policies is
-- a deny-all for the API roles only. The same is already true of the other 49
-- tables the API reads and writes every request, so this is proven in production.
--
-- ORDERING NOTE: `migrate.sh` records a file in `schema_migrations` only AFTER
-- the file itself has committed, in a separate statement, so this migration
-- altering that very table cannot deadlock with its own bookkeeping.
--
-- -----------------------------------------------------------------------------
-- PART 2 (#423) -- index the column the new `login_attempts` retention purge
-- filters on.
--
-- `login_attempts` is a rolling per-email failed-login counter whose primary key
-- is the caller's own (unauthenticated, arbitrary) email string, and until now
-- rows were removed ONLY by a successful login -- so an address failed against
-- once and never signed into left a row for good. The application now deletes
-- rows whose last failure predates the rolling window
-- (login_lockout.ATTEMPT_WINDOW_MINUTES); this index keeps that DELETE from
-- scanning the whole table on a database where a flood has grown it.
--
-- Purely additive: an index changes no row and no behaviour. The lockout itself
-- is NOT touched -- a hard lock lives on `users.locked_at`, not here.
--
-- NOTHING IS DELETED, DROPPED OR BACKFILLED BY THIS MIGRATION. It enables a
-- security flag and creates an index; it reads no alumni data and writes none.
--
-- SAFE TO RE-RUN: ENABLE ROW LEVEL SECURITY is idempotent (a no-op when already
-- on) and CREATE INDEX IF NOT EXISTS is idempotent.
--
-- NOT RUN by this agent against any DB (dev or prod). Apply via the normal
-- migration path.
-- =============================================================================

BEGIN;

-- Part 1: deny-all RLS on the migration bookkeeping table. No policies are
-- created, which IS the lockdown -- see ../rls_lockdown.sql.
ALTER TABLE schema_migrations ENABLE ROW LEVEL SECURITY;

-- Part 2: the retention purge filters on last_failed_at.
CREATE INDEX IF NOT EXISTS idx_login_attempts_last_failed_at
    ON login_attempts (last_failed_at);

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand). Both statements are reversible and neither reversal
-- destroys data -- but note that undoing part 1 restores the exact gap this
-- migration exists to close.
-- =============================================================================
-- ALTER TABLE schema_migrations DISABLE ROW LEVEL SECURITY;
-- DROP INDEX IF EXISTS idx_login_attempts_last_failed_at;

-- =============================================================================
-- VERIFY (run after committing). The first query should return NO rows -- every
-- public table has row security on.
-- =============================================================================
-- SELECT tablename, rowsecurity
-- FROM pg_tables
-- WHERE schemaname = 'public' AND NOT rowsecurity
-- ORDER BY tablename;
--
-- SELECT indexname FROM pg_indexes
-- WHERE schemaname = 'public' AND tablename = 'login_attempts';

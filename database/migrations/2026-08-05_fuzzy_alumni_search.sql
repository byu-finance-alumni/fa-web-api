-- =============================================================================
-- Typo- and spacing-tolerant free-text alumni search (#620)
--
-- Jake, 2026-08-04: "im looking for all alumni at goldman schs in new york"
-- returned nothing, and "i am looking for jake in newyork ... it needs to be
-- able to regoinizr mispleling and spacing like newyork".
--
-- The free-text ``q`` search was a plain ``ILIKE '%token%'`` over the NAME
-- columns only. That cannot reach "Sachs" from "schs" (a typo), cannot reach
-- "New York" from "newyork" (a missing space), and never looked at the employer
-- / title / city / state at all.
--
-- This migration supplies the two things the query layer needs to fix that
-- IN THE DATABASE (never by pulling rows into Python to score them):
--
--   1. ``alumni_search_norm(text)`` -- the canonical normalized form both sides
--      of every comparison are reduced to: accents folded, case folded, and
--      EVERY non-alphanumeric character removed. "New York" and "newyork" both
--      become 'newyork'; "J.P. Morgan" and "jp morgan" both become 'jpmorgan';
--      "Sao Paulo" and "Sao Paulo" both become 'saopaulo'. Spacing, punctuation
--      and diacritics stop being a way to miss a match.
--
--   2. pg_trgm GIN indexes over that normalized expression, so the fuzzy leg
--      (``normalized_column % :term``) is an INDEX SCAN rather than a
--      sequential scan + per-row similarity(). 8,000+ alumni today and growing.
--
-- The query layer (app/repositories/alumni_search.py) then matches
-- exact -> prefix -> contains -> trigram-similar, in that rank order, so an
-- approximate hit can never outrank a real one.
--
-- Index-only + function additions: no data change, no new tables, so no RLS
-- step is needed. Every statement is idempotent (IF NOT EXISTS / OR REPLACE),
-- which matters because migrations auto-apply to dev AND prod via CI.
-- =============================================================================

BEGIN;

-- Supabase installs extensions into the dedicated ``extensions`` schema, which
-- is on the search_path for the roles this API connects as. Creating them there
-- keeps this migration consistent with pgcrypto / uuid-ossp / pg_stat_statements
-- which are already installed that way on both projects.
CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA extensions;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA extensions;

-- ``unaccent(text)`` (the one-argument form) is only STABLE -- it resolves the
-- text-search dictionary by name at call time -- so PostgreSQL refuses to build
-- an expression index on it. The TWO-argument form that names the dictionary
-- explicitly IS immutable, so wrap it. This is the standard workaround and the
-- reason the wrapper exists at all; do not "simplify" it back to unaccent($1).
CREATE OR REPLACE FUNCTION public.immutable_unaccent(text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
STRICT
AS $$
    SELECT extensions.unaccent('extensions.unaccent'::regdictionary, $1)
$$;

-- The canonical search normal form. MUST stay byte-for-byte in agreement with
-- ``app.core.search_terms.normalize`` (the Python twin that normalizes the
-- user's typed term) -- if the two ever disagree, a search silently stops
-- matching. Both: fold accents, lower-case, delete everything that is not
-- [a-z0-9].
--
-- NULL-safe (coalesce to '') so an alumnus with no employer simply normalizes
-- to the empty string instead of turning the whole expression NULL, which keeps
-- the indexes total and the CASE-based ranking arithmetic free of NULLs.
CREATE OR REPLACE FUNCTION public.alumni_search_norm(text)
RETURNS text
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT regexp_replace(
        lower(public.immutable_unaccent(coalesce($1, ''))),
        '[^a-z0-9]+', '', 'g'
    )
$$;

-- --- trigram indexes ---------------------------------------------------------
--
-- One GIN index per searchable column, over the NORMALIZED expression -- the
-- expression must match what the query emits VERBATIM or the planner silently
-- falls back to a sequential scan.
--
-- GIN (not GiST): this is a read-mostly dataset with a very low write rate, and
-- GIN gives materially faster ``%`` lookups. These indexes serve BOTH the fuzzy
-- ``%`` leg and, because gin_trgm_ops also supports LIKE/ILIKE, the
-- leading-wildcard ``LIKE '%term%'`` contains leg -- which the old raw-column
-- btree indexes could never help with.

-- Name columns (the columns q already searched).
CREATE INDEX IF NOT EXISTS idx_alumni_first_name_trgm
    ON alumni USING gin (public.alumni_search_norm(first_name) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_alumni_middle_name_trgm
    ON alumni USING gin (public.alumni_search_norm(middle_name) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_alumni_last_name_trgm
    ON alumni USING gin (public.alumni_search_norm(last_name) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_alumni_preferred_first_name_trgm
    ON alumni USING gin (public.alumni_search_norm(preferred_first_name) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_alumni_birth_name_trgm
    ON alumni USING gin (public.alumni_search_norm(birth_name) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_alumni_other_designations_trgm
    ON alumni USING gin (public.alumni_search_norm(other_designations) extensions.gin_trgm_ops);

-- External ids. Only searched for a SINGLE-word query, but they sit in the same
-- OR as the name columns, so leaving them unindexed drags the whole name-leg
-- subquery down to a sequential scan (measured: 2.3s vs 20ms over 50k rows).
CREATE INDEX IF NOT EXISTS idx_alumni_byu_id_trgm
    ON alumni USING gin (public.alumni_search_norm(byu_id) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_alumni_net_id_trgm
    ON alumni USING gin (public.alumni_search_norm(net_id) extensions.gin_trgm_ops);

-- Employment columns -- the actual gap Jake hit. "Goldman Sachs" lives here, and
-- ``?q=Goldman Sachs`` returned 0 while ``?employer=Goldman Sachs`` returned 15.
CREATE INDEX IF NOT EXISTS idx_current_employment_employer_trgm
    ON current_employment USING gin (public.alumni_search_norm(current_employer) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_current_employment_title_trgm
    ON current_employment USING gin (public.alumni_search_norm(current_title) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_current_employment_city_trgm
    ON current_employment USING gin (public.alumni_search_norm(current_city) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_current_employment_state_trgm
    ON current_employment USING gin (public.alumni_search_norm(current_state) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_current_employment_country_trgm
    ON current_employment USING gin (public.alumni_search_norm(current_country) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_current_employment_industry_trgm
    ON current_employment USING gin (public.alumni_search_norm(current_industry) extensions.gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_current_employment_industry_secondary_trgm
    ON current_employment USING gin (public.alumni_search_norm(current_industry_secondary) extensions.gin_trgm_ops);

-- Past employers: "at <company>" also considers where an alumnus USED to work.
CREATE INDEX IF NOT EXISTS idx_employment_history_employer_trgm
    ON employment_history USING gin (public.alumni_search_norm(employer_name) extensions.gin_trgm_ops);

COMMIT;

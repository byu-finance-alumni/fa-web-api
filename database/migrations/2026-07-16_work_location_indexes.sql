-- =============================================================================
-- Migration: work-location indexes on current_employment (#287)
-- Date: 2026-07-16
-- -----------------------------------------------------------------------------
-- WHY: the intake sheet's address block is the EMPLOYER's address, but the
-- importer bound it to the residence record, so alumni_contact_info.city/state
-- held WORK locations under a residence label. The readers (the geography map,
-- geocoded search, the dashboard's by-state distribution, and the alumni list's
-- city/state filters) have been rebound to read current_employment
-- (current_city / current_state / current_country) directly.
--
-- Those readers carried a specific index load on alumni_contact_info:
--
--   idx_alumni_contact_info_state             (state)
--   idx_alumni_contact_info_city_state        (city, state)
--   idx_alumni_contact_info_country           (country)
--   idx_alumni_contact_info_state_norm        (upper(trim(state)))
--   idx_alumni_contact_info_city_state_norm   (lower(trim(city)),
--                                              upper(trim(state)))
--
-- current_employment now carries that load, so it needs equivalent coverage.
-- The plain (current_state) equivalent already exists as
-- idx_current_employment_state (migrations/2026-07-03_fleet_audit_constraints_
-- indexes.sql), so this migration adds the other four.
--
-- The NORMALIZED expression indexes are the load-bearing ones: the geography
-- GROUP BYs and the geo_search key comparison never touch the raw columns —
-- they group/compare on lower(trim(current_city)) and a CASE that falls through
-- to upper(trim(current_state)). A raw-column index cannot serve those. This
-- mirrors exactly why migrations/2026-07-06_perf_indexes.sql added the
-- _norm pair on alumni_contact_info.
--
-- DROPS NOTHING. The five alumni_contact_info indexes and the columns they
-- cover are deliberately left in place — this PR must be cleanly revertable, and
-- the old columns are retired in a later PR once this is proven in prod.
--
-- Index-only additions: no data change, no new tables, so no RLS step needed.
-- Idempotent via IF NOT EXISTS, so safe to re-run.
-- =============================================================================

BEGIN;

-- Mirrors idx_alumni_contact_info_city_state -- raw (city, state) lookups, e.g.
-- the get_city_detail equality match on current_city + state.
CREATE INDEX IF NOT EXISTS idx_current_employment_city_state
    ON current_employment (current_city, current_state);

-- Mirrors idx_alumni_contact_info_country -- the world map groups and filters on
-- country (get_countries / get_country_detail / get_country_alumni).
CREATE INDEX IF NOT EXISTS idx_current_employment_country
    ON current_employment (current_country);

-- Mirrors idx_alumni_contact_info_state_norm -- the geography state aggregation
-- groups on upper(trim(current_state)) (app/services/geography.py::_STATE).
CREATE INDEX IF NOT EXISTS idx_current_employment_state_norm
    ON current_employment (upper(trim(current_state)));

-- Mirrors idx_alumni_contact_info_city_state_norm -- the geography city
-- aggregation groups on (lower(trim(current_city)), upper(trim(current_state)))
-- (app/services/geography.py::_CITY + _STATE), and geo_search's
-- alumni_location_filter compares that same pair against the city_geo key set.
CREATE INDEX IF NOT EXISTS idx_current_employment_city_state_norm
    ON current_employment (lower(trim(current_city)), upper(trim(current_state)));

COMMIT;

-- =============================================================================
-- VERIFY (run after committing):
-- =============================================================================
-- SELECT indexname FROM pg_indexes WHERE tablename = 'current_employment';
--   expect, in addition to the pre-existing
--     idx_current_employment_alumni_id / _employer / _industry / _state
--     and uq_current_employment_alumni_id:
--     idx_current_employment_city_state
--     idx_current_employment_country
--     idx_current_employment_state_norm
--     idx_current_employment_city_state_norm
-- =============================================================================

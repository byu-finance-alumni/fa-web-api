-- =============================================================================
-- Performance indexes for hot aggregates (#186)
--
-- 1. interactions (alumni_id, interaction_date_time): backs the dashboard
--    "last contacted" filters in app/repositories/alumni.py -- the EXISTS /
--    NOT EXISTS anti-joins for contacted-after / not-contacted-since / never
--    contacted. Only a single-column idx_interactions_alumni_id exists today,
--    which can't serve the date predicate; the composite covers both.
-- 2. Expression indexes matching the normalized geography GROUP BYs
--    (app/services/geography.py). The existing idx_alumni_contact_info_state /
--    _city_state are on the raw columns and can't serve the upper(trim(state))
--    and lower(trim(city)) / upper(trim(state)) expressions the queries group
--    on, so add expression indexes that do.
--
-- Index-only additions: no data change, no new tables, so no RLS step needed.
-- Idempotent via IF NOT EXISTS, so safe to re-run.
-- =============================================================================

BEGIN;

-- Dashboard last-contacted anti-joins (contacted_after / not-contacted-since /
-- never-contacted) filter interactions by alumni_id and interaction_date_time.
CREATE INDEX IF NOT EXISTS idx_interactions_alumni_id_date
    ON interactions (alumni_id, interaction_date_time);

-- Geography state aggregation groups on upper(trim(state)).
CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_state_norm
    ON alumni_contact_info (upper(trim(state)));

-- Geography city aggregation groups on (lower(trim(city)), upper(trim(state))).
CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_city_state_norm
    ON alumni_contact_info (lower(trim(city)), upper(trim(state)));

COMMIT;

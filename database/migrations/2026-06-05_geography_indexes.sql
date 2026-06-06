-- =============================================================================
-- Indexes supporting the Alumni Geography dashboard aggregations
-- (GROUP BY state / city,state / employer / industry over located alumni).
--
-- Index-only additions: no data change, no new tables, so no RLS step needed.
-- Idempotent via IF NOT EXISTS, so safe to re-run.
-- =============================================================================

BEGIN;

CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_state
    ON alumni_contact_info (state);
CREATE INDEX IF NOT EXISTS idx_alumni_contact_info_city_state
    ON alumni_contact_info (city, state);
CREATE INDEX IF NOT EXISTS idx_current_employment_employer
    ON current_employment (current_employer);
CREATE INDEX IF NOT EXISTS idx_current_employment_industry
    ON current_employment (current_industry);

COMMIT;

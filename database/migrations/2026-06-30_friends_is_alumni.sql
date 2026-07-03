-- Friends of the finance program (#218 / #155).
-- Additive flag: every existing row is an alumnus (default true). "Friends"
-- are non-alumni contacts stored in the same table with is_alumni = false,
-- so they reuse all alumni detail tables, search, and map shading while being
-- filterable into their own tab. Partial index supports the friends/alumni
-- split filter without bloating the common (is_alumni = true) path.
ALTER TABLE alumni
    ADD COLUMN IF NOT EXISTS is_alumni boolean NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS idx_alumni_is_alumni
    ON alumni (is_alumni)
    WHERE is_alumni = false;

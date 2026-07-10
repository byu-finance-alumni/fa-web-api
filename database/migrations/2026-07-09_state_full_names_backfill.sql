-- =============================================================================
-- Migration: backfill alumni state values from 2-letter codes to full names
-- Date: 2026-07-09
-- -----------------------------------------------------------------------------
-- Alumni state is now STORED as the full display name (e.g. "Utah") for both
-- the CSV import and the manual create/edit path. This backfills existing rows
-- that still hold a 2-letter code so display is consistent everywhere.
--
-- Two physical columns hold a US state:
--   * alumni_contact_info.state          (the mailing-address state)
--   * current_employment.current_state   (the current-job state)
-- Both are rewritten.
--
-- Only EXACT 2-letter code matches (case-insensitive, after trimming) are
-- rewritten to their full name; every other value is left untouched. This is
-- safe because the geography/map query layer folds a stored full name back to a
-- 2-letter code at query time, so it tolerates both formats during/after the
-- backfill.
--
-- SAFE TO RE-RUN: once a value has become a full name it no longer equals any
-- 2-letter code, so a re-run matches nothing.
-- =============================================================================

BEGIN;

-- alumni_contact_info.state (mailing address)
WITH code_map(code, full_name) AS (
    VALUES
        ('AL', 'Alabama'), ('AK', 'Alaska'), ('AZ', 'Arizona'),
        ('AR', 'Arkansas'), ('CA', 'California'), ('CO', 'Colorado'),
        ('CT', 'Connecticut'), ('DE', 'Delaware'),
        ('DC', 'District of Columbia'), ('FL', 'Florida'), ('GA', 'Georgia'),
        ('HI', 'Hawaii'), ('ID', 'Idaho'), ('IL', 'Illinois'),
        ('IN', 'Indiana'), ('IA', 'Iowa'), ('KS', 'Kansas'),
        ('KY', 'Kentucky'), ('LA', 'Louisiana'), ('ME', 'Maine'),
        ('MD', 'Maryland'), ('MA', 'Massachusetts'), ('MI', 'Michigan'),
        ('MN', 'Minnesota'), ('MS', 'Mississippi'), ('MO', 'Missouri'),
        ('MT', 'Montana'), ('NE', 'Nebraska'), ('NV', 'Nevada'),
        ('NH', 'New Hampshire'), ('NJ', 'New Jersey'), ('NM', 'New Mexico'),
        ('NY', 'New York'), ('NC', 'North Carolina'), ('ND', 'North Dakota'),
        ('OH', 'Ohio'), ('OK', 'Oklahoma'), ('OR', 'Oregon'),
        ('PA', 'Pennsylvania'), ('RI', 'Rhode Island'),
        ('SC', 'South Carolina'), ('SD', 'South Dakota'), ('TN', 'Tennessee'),
        ('TX', 'Texas'), ('UT', 'Utah'), ('VT', 'Vermont'), ('VA', 'Virginia'),
        ('WA', 'Washington'), ('WV', 'West Virginia'), ('WI', 'Wisconsin'),
        ('WY', 'Wyoming')
)
UPDATE alumni_contact_info AS c
SET state = m.full_name
FROM code_map AS m
WHERE c.state IS NOT NULL
  AND upper(btrim(c.state)) = m.code;

-- current_employment.current_state (current job)
WITH code_map(code, full_name) AS (
    VALUES
        ('AL', 'Alabama'), ('AK', 'Alaska'), ('AZ', 'Arizona'),
        ('AR', 'Arkansas'), ('CA', 'California'), ('CO', 'Colorado'),
        ('CT', 'Connecticut'), ('DE', 'Delaware'),
        ('DC', 'District of Columbia'), ('FL', 'Florida'), ('GA', 'Georgia'),
        ('HI', 'Hawaii'), ('ID', 'Idaho'), ('IL', 'Illinois'),
        ('IN', 'Indiana'), ('IA', 'Iowa'), ('KS', 'Kansas'),
        ('KY', 'Kentucky'), ('LA', 'Louisiana'), ('ME', 'Maine'),
        ('MD', 'Maryland'), ('MA', 'Massachusetts'), ('MI', 'Michigan'),
        ('MN', 'Minnesota'), ('MS', 'Mississippi'), ('MO', 'Missouri'),
        ('MT', 'Montana'), ('NE', 'Nebraska'), ('NV', 'Nevada'),
        ('NH', 'New Hampshire'), ('NJ', 'New Jersey'), ('NM', 'New Mexico'),
        ('NY', 'New York'), ('NC', 'North Carolina'), ('ND', 'North Dakota'),
        ('OH', 'Ohio'), ('OK', 'Oklahoma'), ('OR', 'Oregon'),
        ('PA', 'Pennsylvania'), ('RI', 'Rhode Island'),
        ('SC', 'South Carolina'), ('SD', 'South Dakota'), ('TN', 'Tennessee'),
        ('TX', 'Texas'), ('UT', 'Utah'), ('VT', 'Vermont'), ('VA', 'Virginia'),
        ('WA', 'Washington'), ('WV', 'West Virginia'), ('WI', 'Wisconsin'),
        ('WY', 'Wyoming')
)
UPDATE current_employment AS e
SET current_state = m.full_name
FROM code_map AS m
WHERE e.current_state IS NOT NULL
  AND upper(btrim(e.current_state)) = m.code;

COMMIT;

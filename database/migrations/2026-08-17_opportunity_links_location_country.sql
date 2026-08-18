-- =============================================================================
-- Migration: opportunity_links.location_country
-- Date: 2026-08-17  (#441 follow-up)
-- -----------------------------------------------------------------------------
-- WHY. The table shipped with `location_city` + `location_state` only, which can
-- only describe a US opening. Both entry forms (the staff "Links" tab and the
-- public survey) are growing an "outside the United States" mode, so a country
-- now has somewhere to go. Without this column the data has nowhere to land and
-- the two forms would have to smuggle it into `location_state` — which would
-- silently corrupt the state filter and the state facet for every non-US row.
--
-- WIDTH. varchar(100), the same as `location_city` and `location_state` here and
-- the same as `current_employment.current_country` elsewhere in this schema. A
-- location field is a location field; a country that would not fit the column
-- the rest of the app stores countries in is not a country. And, as with every
-- other column on this table, the width is a SECURITY control and not just
-- hygiene: this field is reachable from the PUBLIC, token-gated survey submit,
-- so the width is the persistence bound on how much attacker-supplied text one
-- unauthenticated call can store. It is mirrored by `COUNTRY_MAX` in
-- `app/models/opportunity_link.py`, enforced by the Pydantic schemas, and
-- re-enforced in `app/services/opportunity_links._validated_fields` — which is
-- the only check that exists for a non-HTTP caller.
--
-- The value is sanitised on the way in exactly like `location_city`: control and
-- invisible characters refused, the `;=<>|` set refused, and a LEADING `= + - @`
-- refused (the CSV-formula-lead defence — this text ends up in a staff export).
-- See `_validate_short_text`.
--
-- ⚠️ NO BACKFILL, ON PURPOSE. Existing rows are NOT set to 'United States'.
-- Nobody was asked for a country when they were written, so their country is
-- genuinely unknown; writing a plausible value would turn "we never asked" into
-- "we were told", and that is a worse record than a NULL. A NULL here reads as
-- "not stated" and is visibly a gap someone can fill; an invented 'United
-- States' is indistinguishable from a stated one forever after.
--
-- ADDITIVE AND SAFE ON EXISTING ROWS. A nullable ADD COLUMN with no DEFAULT does
-- not rewrite the table and does not need to validate anything, so it takes a
-- brief ACCESS EXCLUSIVE lock and returns. Existing rows keep working unchanged;
-- readers that do not know about the column are unaffected.
--
-- NOT IN THIS MIGRATION: the "application deadline must be in the future" rule
-- shipped alongside it. That is deliberately APPLICATION-level only and there is
-- no CHECK constraint for it, because a CHECK against `current_date` would not
-- be immutable and — more to the point — would make every existing row with a
-- passed deadline un-updatable, which is the exact behaviour the rule is written
-- to avoid. See `app/schemas/opportunity_link.validate_application_deadline` and
-- `services.opportunity_links.update_link`.
--
-- RLS: no change needed. `opportunity_links` is already in the explicit list in
-- `database/rls_lockdown.sql` (verified) and already has RLS enabled from
-- `2026-08-17_opportunity_links.sql`; adding a column does not alter that.
--
-- SAFE TO RE-RUN: IF NOT EXISTS on the column.
--
-- NOT RUN by this agent against any DB. Apply via the normal migration path.
-- =============================================================================

BEGIN;

ALTER TABLE opportunity_links
    ADD COLUMN IF NOT EXISTS location_country varchar(100);

COMMIT;

-- =============================================================================
-- ROLLBACK (run by hand):
--   ALTER TABLE opportunity_links DROP COLUMN IF EXISTS location_country;
-- =============================================================================

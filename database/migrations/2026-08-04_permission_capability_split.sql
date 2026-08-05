-- =============================================================================
-- Migration: split the permission model into per-section capabilities
-- Date: 2026-08-04  (fa-web-api #379)
-- -----------------------------------------------------------------------------
-- Two changes, both purely ADDITIVE grants:
--
--   1. `interactions.create` is seeded to EVERY role. Logging an interaction was
--      previously described in the permission editor as part of "Edit alumni",
--      even though the routes were open to any view-access role (#129). It is
--      now its own capability so anyone who can sign in can record an
--      interaction, and the engineer can see and change that rule in the matrix.
--
--   2. `alumni.full` ("Manage alumni & data") is dissolved into twelve codes.
--      One switch used to gate alumni create/archive, both importers, every
--      export, headshots, event management, notes, the survey console, donation
--      reads, and the advanced read-only reports, so no one of them could be
--      delegated without all of them. The replacements are:
--
--        alumni.create      alumni.archive     alumni.import     alumni.export
--        alumni.photos      events.create      events.import     events.manage
--        notes.manage       surveys.manage     donations.view    reports.advanced
--
--      (`events.create` / `events.import` already exist from #378; they are
--      re-listed here so a database that skipped that migration still lands in
--      the same place. ON CONFLICT makes the overlap a no-op.)
--
-- PRESERVES TODAY'S ACCESS EXACTLY. The replacements are granted by SELECTING
-- the roles that currently hold `alumni.full` IN THIS DATABASE, rather than by
-- hardcoding "full_access + super_admin". So if an engineer has already narrowed
-- or widened `alumni.full` in the permission editor, that decision is carried
-- forward instead of being silently overwritten by a default.
--
-- `alumni.full` ROWS ARE DELIBERATELY LEFT IN PLACE. They are harmless (no route
-- checks the code any more, and it is absent from the registry so the editor no
-- longer renders or accepts it) and they make rolling the API back to the
-- pre-#379 build safe — the old guards would find their grants exactly as they
-- were. A later cleanup migration can drop them once #379 is settled in prod.
--
-- Data-only and idempotent: no DDL, and ON CONFLICT DO NOTHING means re-running
-- is a no-op. Nothing is ever REVOKED, so it cannot lock anyone out.
--
-- ORDERING TRAP, and how it is handled. `load_grants` falls back to the in-code
-- DEFAULT_GRANTS only when `role_capabilities` is EMPTY, and dev and prod both
-- have rows — so on a real database a new code is DENIED to everyone until this
-- file has run. CI does not migrate dev at all, and the prod `migrate` job is
-- manually gated, so "deploy first, migrate later" is a live possibility here,
-- not a theoretical one. Two defences:
--
--   * app/core/capabilities.expand_legacy_grants: a role whose rows still say
--     `alumni.full` and say nothing about the twelve replacements is read as
--     holding all twelve. So the gap between the API deploy and this migration
--     is invisible to users. Once this migration runs the role holds the
--     explicit rows, the expansion stops firing, and revokes in the permission
--     editor take effect normally.
--   * The EXISTS guards below. On a BRAND-NEW database (empty table) every
--     statement here is a no-op, so the table stays empty and `load_grants`
--     keeps using DEFAULT_GRANTS — which already contains the new codes.
--     Without the guards, seeding `interactions.create` alone would make the
--     table non-empty and switch the fallback off, stripping every other
--     capability from every role. That is the exact failure mode this project
--     has hit before.
--
-- `interactions.create` is the one thing the code-level bridge does NOT cover
-- (its "not migrated yet" signal is indistinguishable from an engineer having
-- deliberately revoked it, which would make the toggle un-revokable), so if the
-- API ships ahead of this file a professor briefly cannot log an interaction.
-- Apply this migration with — or before — the API deploy.
-- =============================================================================

BEGIN;

-- 1. Every role that holds `alumni.full` gets each of its twelve replacements.
--    Derived from the live rows, so an edited config is carried forward as-is.
INSERT INTO role_capabilities (role_id, capability_code)
SELECT rc.role_id, replacement.code
FROM role_capabilities rc
CROSS JOIN (
    VALUES
        ('alumni.create'),
        ('alumni.archive'),
        ('alumni.import'),
        ('alumni.export'),
        ('alumni.photos'),
        ('events.create'),
        ('events.import'),
        ('events.manage'),
        ('notes.manage'),
        ('surveys.manage'),
        ('donations.view'),
        ('reports.advanced')
) AS replacement(code)
WHERE rc.capability_code = 'alumni.full'
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 2. `interactions.create` for EVERY provisioned role — the one deliberate
--    widening in #379, and a no-op in practice: the interaction routes were
--    already reachable by any role holding `view` (#129).
--    The EXISTS guard keeps a brand-new, unseeded database on the in-code
--    DEFAULT_GRANTS fallback (see the ordering note above).
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, 'interactions.create'
FROM roles r
WHERE EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

-- 3. Explicit engineer rows for every new code. The engineer already holds
--    everything via the runtime hard-override in `effective_capabilities`, but
--    the rows keep the permission matrix honest and match the earlier capability
--    migrations (2026-06-30_profile_completeness_capability,
--    2026-07-06_donations_manage_capability, 2026-08-04_event_capabilities).
INSERT INTO role_capabilities (role_id, capability_code)
SELECT r.role_id, new_code.code
FROM roles r
CROSS JOIN (
    VALUES
        ('interactions.create'),
        ('alumni.create'),
        ('alumni.archive'),
        ('alumni.import'),
        ('alumni.export'),
        ('alumni.photos'),
        ('events.create'),
        ('events.import'),
        ('events.manage'),
        ('notes.manage'),
        ('surveys.manage'),
        ('donations.view'),
        ('reports.advanced')
) AS new_code(code)
WHERE r.role_name = 'engineer'
  AND EXISTS (SELECT 1 FROM role_capabilities)
ON CONFLICT ON CONSTRAINT uq_role_capabilities DO NOTHING;

COMMIT;

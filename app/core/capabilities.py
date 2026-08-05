"""Capability registry — the editable permission model (#164).

Authorization used to be hardcoded role allow-lists (one "full access" set =
``{engineer, super_admin, full_access}`` …). This module turns those fixed
buckets into a small set of named **capabilities** whose role assignments are
data — stored in ``role_capabilities`` and editable by the engineer in the
Engineer Console permission editor. The ``require_*`` guards in
``app/api/dependencies/auth.py`` now resolve a capability against the live
config instead of a frozen role set.

Two invariants keep this safe:

* The **capability codes themselves stay defined in code** (here). The engineer
  toggles which roles hold each capability — never invents new capabilities or
  new roles. The 5-role hierarchy in ``app/core/roles.py`` is fixed.
* The :data:`Capability.ENGINEER` capability is **not assignable** — it gates the
  engineer console and the permission editor itself, so it must remain
  engineer-exclusive (granting it to another role would hand over the keys to
  the kingdom, including this editor). It is shown locked in the matrix.

``DEFAULT_GRANTS`` is the seed (and the offline/empty-table fallback) and
reproduces the historical hardcoded guards exactly, so converting to the config
model is behaviour-preserving on day one.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.roles import RoleName


@dataclass(frozen=True)
class CapabilitySpec:
    """One capability: a stable ``code`` plus UI-facing label/description.

    ``assignable`` is False for the engineer meta-capability — the matrix shows
    it locked to the engineer and the toggle endpoint refuses to change it.
    """

    code: str
    label: str
    description: str
    assignable: bool = True


# --- Capability codes (stable identifiers stored in role_capabilities) --------


class Capability:
    """Stable capability code constants (the contract stored in the DB)."""

    VIEW = "view"
    ALUMNI_EDIT = "alumni.edit"
    INTERACTIONS_CREATE = "interactions.create"
    ALUMNI_CREATE = "alumni.create"
    ALUMNI_ARCHIVE = "alumni.archive"
    ALUMNI_IMPORT = "alumni.import"
    ALUMNI_EXPORT = "alumni.export"
    ALUMNI_PHOTOS = "alumni.photos"
    EVENTS_CREATE = "events.create"
    EVENTS_IMPORT = "events.import"
    EVENTS_MANAGE = "events.manage"
    NOTES_MANAGE = "notes.manage"
    SURVEYS_MANAGE = "surveys.manage"
    DONATIONS_VIEW = "donations.view"
    USER_ADMIN = "user_admin"
    DONATIONS_MANAGE = "donations.manage"
    REPORTS_ADVANCED = "reports.advanced"
    VOCAB_ADMIN = "vocab_admin"
    PROFILE_COMPLETENESS = "profile.completeness"
    ENGINEER = "engineer"

    # RETIRED (#379). ``alumni.full`` was the blunt "Manage alumni & data"
    # capability that gated alumni create/archive, import, export, headshots,
    # event management, notes, the survey console, donation-ledger reads, and the
    # advanced read-only reports — all behind one switch, so an engineer could
    # not hand out any one of them without handing out every one of them. It has
    # been dissolved into the twelve codes above and NO route checks it any more.
    #
    # The constant survives for exactly two reasons and must not be granted,
    # rendered, or guarded on:
    #   1. :func:`expand_legacy_grants` reads it, so a database whose
    #      ``role_capabilities`` rows have not been migrated yet still resolves
    #      the new codes (see the note on that constant);
    #   2. the migration seeds the new codes FROM the existing ``alumni.full``
    #      rows, which are deliberately left in place so an API rollback is safe.
    # It is absent from :data:`CAPABILITIES`, so the permission editor no longer
    # offers it and ``PATCH /engineer/permissions`` rejects it as unknown.
    LEGACY_ALUMNI_FULL = "alumni.full"


# Ordered registry — drives the permission-editor matrix rows and the
# role-capabilities table. Order is least → most privileged for readability.
CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        code=Capability.VIEW,
        label="View records",
        description=(
            "Read alumni records, the dashboard, the map, events, and reports."
        ),
    ),
    CapabilitySpec(
        code=Capability.ALUMNI_EDIT,
        label="Edit alumni",
        description=(
            "Edit existing alumni and their related records — employment, "
            "education, leadership, tags, status labels, and follow-up tasks. "
            "Does not include creating or archiving alumni, and no longer "
            "includes logging interactions (see \"Log interactions\")."
        ),
    ),
    # Logging an interaction, split out of the timeline writes that "Edit alumni"
    # described (#379). It is the one write the whole directory is trusted with:
    # a professor who spots an alumnus at a conference should be able to record
    # it without being handed the ability to edit the record itself. Backed by
    # long-standing behaviour — view_only could already POST interactions (#129)
    # — so seeding it to every role changes nothing on day one; it just makes an
    # access rule that was hidden in code visible and editable in the matrix.
    CapabilitySpec(
        code=Capability.INTERACTIONS_CREATE,
        label="Log interactions",
        description=(
            "Record a call, meeting, or email on an alumnus's timeline, and "
            "edit or delete an interaction. Every role holds this by default. "
            "Holders who cannot edit alumni may only change interactions they "
            "logged themselves."
        ),
    ),
    # --- the twelve codes that replaced `alumni.full` (#379) ------------------
    #
    # `alumni.full` ("Manage alumni & data") was one switch over alumni
    # create/archive, both importers, every export, headshots, event management,
    # notes, the survey console, donation-ledger reads, and the advanced
    # read-only reports. The lines below are drawn by WHAT AN ADMINISTRATOR HANDS
    # OUT SEPARATELY, not by endpoint:
    #
    #   * create vs archive vs edit are three different levels of trust in the
    #     same record set — a coordinator may add people without being able to
    #     make them disappear;
    #   * import and export are the two bulk doors, in opposite directions: one
    #     is a data-integrity risk, the other is the FERPA egress risk, and they
    #     are almost never wanted together;
    #   * everything that emits a file of people is ONE toggle (`alumni.export`)
    #     regardless of which screen it hangs off, so "can this role take alumni
    #     data out of the system" is a single, auditable yes/no;
    #   * headshots, notes, surveys, and the donation ledger are separate product
    #     areas an admin thinks about by name;
    #   * the advanced read-only surfaces (activity feed, data quality, queues,
    #     task list, map drill-downs) are grouped as one because they are all
    #     reads that go beyond the basic dashboard — splitting reporting per
    #     screen buys nothing and makes the matrix unreadable.
    #
    # Every one of them seeds to EXACTLY the roles that held `alumni.full`, so
    # day-one authorization is unchanged.
    CapabilitySpec(
        code=Capability.ALUMNI_CREATE,
        label="Add alumni",
        description=(
            "Create new alumni and friend-of-the-program records by hand, "
            "including the pre-save duplicate check and the records created "
            "from unmatched conference attendees."
        ),
    ),
    CapabilitySpec(
        code=Capability.ALUMNI_ARCHIVE,
        label="Archive & restore alumni",
        description=(
            "Archive an alumnus (hiding them from the directory) and restore an "
            "archived record. Kept apart from \"Add alumni\": removing people "
            "from view is a different level of trust from adding them."
        ),
    ),
    CapabilitySpec(
        code=Capability.ALUMNI_IMPORT,
        label="Import alumni data",
        description=(
            "Bulk-load the alumni spreadsheet: the new-record import and the "
            "bulk update import, with their templates and dry-run previews. One "
            "file can create or rewrite thousands of records."
        ),
    ),
    CapabilitySpec(
        code=Capability.ALUMNI_EXPORT,
        label="Export alumni data",
        description=(
            "Download alumni data as a file — the filtered list export, the "
            "single-profile export, an event's attendee roster, and the cohort "
            "download used to prepare a bulk update. This is the one switch "
            "over personal data leaving the system, so it covers every export "
            "screen rather than being split per page."
        ),
    ),
    CapabilitySpec(
        code=Capability.ALUMNI_PHOTOS,
        label="Manage headshots",
        description=(
            "Upload, replace, and remove alumni headshots, including the bulk "
            "photo import. Viewing headshots needs only \"View records\"."
        ),
    ),
    # Event authoring, split out of alumni.full (#378) so the engineer can widen
    # (or narrow) who may add events without also handing over alumni
    # create/archive/import. Deliberately TWO codes, not one: creating a single
    # event is a one-row write a coordinator might reasonably be trusted with,
    # while a bulk upload creates an event plus its whole attendee roster from a
    # file in one shot. Both default to exactly the roles that held alumni.full
    # (full_access + super_admin, plus the engineer override), so day-one
    # behaviour is unchanged.
    CapabilitySpec(
        code=Capability.EVENTS_CREATE,
        label="Create events",
        description=(
            "Add a new event by hand from the events page. Editing, deleting, "
            "and managing an existing event's attendee roster are covered by "
            "\"Manage events\"."
        ),
    ),
    CapabilitySpec(
        code=Capability.EVENTS_IMPORT,
        label="Bulk upload events",
        description=(
            "Upload an attendee CSV to create an event and its roster in one "
            "step, and download the import template. Higher risk than creating "
            "a single event — one file can add an event and hundreds of "
            "attendance records at once."
        ),
    ),
    CapabilitySpec(
        code=Capability.EVENTS_MANAGE,
        label="Manage events",
        description=(
            "Edit and delete existing events and manage their attendee rosters, "
            "including matching a conference registration list to alumni. "
            "Downloading a roster needs \"Export alumni data\"; creating records "
            "for unmatched attendees needs \"Add alumni\"."
        ),
    ),
    CapabilitySpec(
        code=Capability.NOTES_MANAGE,
        label="Write notes",
        description=(
            "Add, edit, and delete the notes attached to an alumnus, an "
            "interaction, or an event. Reading notes needs only \"View "
            "records\"."
        ),
    ),
    CapabilitySpec(
        code=Capability.SURVEYS_MANAGE,
        label="Manage surveys",
        description=(
            "The survey console: review, apply, and reject responses, and "
            "schedule, send, pause, and cancel a cohort's campaign. Stopping or "
            "cancelling EVERY cohort at once stays engineer-only."
        ),
    ),
    CapabilitySpec(
        code=Capability.DONATIONS_VIEW,
        label="View donations",
        description=(
            "See the Pay It Forward Fund donor list, totals, and per-alumnus "
            "giving amounts. Separate from \"Manage donations\" so a role can "
            "read the ledger without being able to write to it."
        ),
    ),
    CapabilitySpec(
        code=Capability.USER_ADMIN,
        label="User administration",
        description=(
            "Create users, assign and remove roles, and read the audit log."
        ),
    ),
    CapabilitySpec(
        code=Capability.DONATIONS_MANAGE,
        label="Manage donations",
        description=(
            "Add, edit, and bulk-import Pay It Forward Fund donation-ledger "
            "records. Distinct from user administration so donation-ledger "
            "writes aren't silently granted when user-admin is delegated (#189)."
        ),
    ),
    CapabilitySpec(
        code=Capability.REPORTS_ADVANCED,
        label="Advanced reports & lookups",
        description=(
            "The read-only tooling beyond the basic dashboard: the activity "
            "feed, the data-quality report, the follow-up and "
            "recently-contacted queues, the open-task list, and the map's "
            "per-state / per-country / radius alumni lists. One toggle because "
            "these are all reads — nothing here changes a record."
        ),
    ),
    CapabilitySpec(
        code=Capability.VOCAB_ADMIN,
        label="Vocabulary & dropdowns",
        description=(
            "Manage the controlled-vocabulary options that populate the app's "
            "dropdowns."
        ),
    ),
    # NOTE (#189): profile.completeness is enforced CLIENT-SIDE ONLY — it toggles
    # the visibility of the completeness tab/score in the UI. There is no backend
    # guard (no ``require_capability(PROFILE_COMPLETENESS)`` on any route), so it is
    # NOT an access-control boundary and must not be mistaken for one: the
    # underlying alumni fields it summarizes are already returned by the normal
    # view-access routes. Add a server-side guard here (and gate the relevant
    # route) if it ever needs to actually restrict data.
    CapabilitySpec(
        code=Capability.PROFILE_COMPLETENESS,
        label="Profile completeness",
        description=(
            "See the per-alumnus profile-completeness checklist and score "
            "(which required fields are missing) as its own tab on the alumni "
            "profile."
        ),
    ),
    CapabilitySpec(
        code=Capability.ENGINEER,
        label="Engineer console",
        description=(
            "Access the engineer-only console: the permission editor, "
            "preview-as-role, login history, and support contacts. Engineer "
            "only — cannot be granted to another role."
        ),
        assignable=False,
    ),
)

CAPABILITIES_BY_CODE: dict[str, CapabilitySpec] = {c.code: c for c in CAPABILITIES}

# Every capability code, as a set, for the engineer hard-override (engineer
# always holds every capability regardless of what the config says).
ALL_CAPABILITY_CODES: frozenset[str] = frozenset(CAPABILITIES_BY_CODE)

# Capability codes the engineer may toggle for a non-engineer role.
ASSIGNABLE_CAPABILITY_CODES: frozenset[str] = frozenset(
    c.code for c in CAPABILITIES if c.assignable
)


# The twelve codes that `alumni.full` used to cover (#379). A role that held
# `alumni.full` holds exactly this set afterwards — the definition of "nobody
# gained or lost access". Used by the deploy-order safety net below, mirrored by
# the seed migration, and asserted in tests/test_capability_split.py.
ALUMNI_FULL_REPLACEMENTS: frozenset[str] = frozenset(
    {
        Capability.ALUMNI_CREATE,
        Capability.ALUMNI_ARCHIVE,
        Capability.ALUMNI_IMPORT,
        Capability.ALUMNI_EXPORT,
        Capability.ALUMNI_PHOTOS,
        Capability.EVENTS_CREATE,
        Capability.EVENTS_IMPORT,
        Capability.EVENTS_MANAGE,
        Capability.NOTES_MANAGE,
        Capability.SURVEYS_MANAGE,
        Capability.DONATIONS_VIEW,
        Capability.REPORTS_ADVANCED,
    }
)


# --- Default grants (seed + empty-table fallback) -----------------------------
#
# Reproduces the historical hardcoded guards. Keyed by role NAME (the stable
# RoleName value), valued by the set of capability codes that role holds.
#
# NOTE the two-way contract with `database/migrations/2026-08-04_capability_split.sql`:
# this dict is what a BRAND-NEW database gets (load_grants falls back here while
# `role_capabilities` is empty), the migration is what an EXISTING database gets.
# They must agree, and `tests/test_capability_split.py` pins that they do.
DEFAULT_GRANTS: dict[str, frozenset[str]] = {
    RoleName.ENGINEER.value: ALL_CAPABILITY_CODES,
    RoleName.SUPER_ADMIN.value: frozenset(
        {
            Capability.VIEW,
            Capability.ALUMNI_EDIT,
            # Seeded to EVERY role (#379): logging an interaction is the one
            # write the whole directory is trusted with. This is not a widening
            # in practice — the interaction routes were already open to any
            # view-access role (#129); the capability just makes that rule
            # visible in the permission editor instead of buried in a guard.
            Capability.INTERACTIONS_CREATE,
            # The twelve codes below replaced `alumni.full` (#379) and default to
            # EXACTLY the roles that held it, so authorization is unchanged.
            *ALUMNI_FULL_REPLACEMENTS,
            Capability.USER_ADMIN,
            # donations.manage defaults to EXACTLY the roles that held user_admin
            # before it was split out (super_admin + engineer), so gating the
            # donation writes on it is behaviour-preserving on day one (#189).
            Capability.DONATIONS_MANAGE,
            Capability.PROFILE_COMPLETENESS,
        }
    ),
    RoleName.FULL_ACCESS.value: frozenset(
        {
            Capability.VIEW,
            Capability.ALUMNI_EDIT,
            Capability.INTERACTIONS_CREATE,
            *ALUMNI_FULL_REPLACEMENTS,
        }
    ),
    RoleName.STUDENT.value: frozenset(
        {
            Capability.VIEW,
            Capability.ALUMNI_EDIT,
            Capability.INTERACTIONS_CREATE,
        }
    ),
    RoleName.VIEW_ONLY.value: frozenset(
        {Capability.VIEW, Capability.INTERACTIONS_CREATE}
    ),
}


def expand_legacy_grants(caps: frozenset[str] | set[str]) -> set[str]:
    """Resolve a role's stored grants, expanding pre-#379 `alumni.full` rows.

    **This exists to defuse a deploy-ordering trap, not to add behaviour.**
    ``load_grants`` only falls back to :data:`DEFAULT_GRANTS` when
    ``role_capabilities`` is EMPTY, and both dev and prod have rows. So on any
    real database the twelve codes that replaced ``alumni.full`` exist for a role
    only once the seed migration has run — and if the API deploys before the
    migration lands, ``full_access``/``super_admin`` would 403 on alumni create,
    both importers, every export, headshots, event management, notes, the survey
    console, donation reads, and the reports. That gap has bitten this project
    before, so it is closed in code rather than left to deploy order: a role whose
    rows still say ``alumni.full`` and say NOTHING about the new codes is read as
    holding all twelve.

    The "and says nothing about the new codes" half is what keeps the permission
    editor working. The moment the migration seeds the explicit rows, the role
    holds some replacement code, the expansion stops firing, and a revoke in the
    editor sticks. (Revoking *all twelve* from a role whose stale ``alumni.full``
    row was never cleaned up would resurrect them — an accepted, documented edge
    case that disappears once the legacy rows are dropped in a later cleanup
    migration.)

    ``interactions.create`` is deliberately NOT bridged the same way. Its
    pre-#379 marker would be "holds view and nothing from #379", which is exactly
    what a role looks like after an engineer intentionally revokes it — the shim
    could not tell "not migrated yet" from "deliberately narrowed" and would make
    the toggle un-revokable. The gap it would have covered is the smallest one
    here (a professor briefly cannot log an interaction) and closes the moment
    the seed migration runs, so revocability wins.
    """
    resolved = set(caps)
    if (
        Capability.LEGACY_ALUMNI_FULL in resolved
        and not resolved & ALUMNI_FULL_REPLACEMENTS
    ):
        resolved |= ALUMNI_FULL_REPLACEMENTS
    return resolved


def effective_capabilities(
    config: dict[str, frozenset[str]], roles: list[str] | set[str]
) -> set[str]:
    """The set of capability codes a user holding ``roles`` has under ``config``.

    The engineer is hard-overridden to hold **every** capability — a corrupt or
    incomplete config can never lock the engineer out of their own console or
    the permission editor. For every other role the result is the union of the
    capabilities granted to each role the user holds, after
    :func:`expand_legacy_grants` resolves any not-yet-migrated rows.
    """
    if RoleName.ENGINEER.value in roles:
        return set(ALL_CAPABILITY_CODES)
    caps: set[str] = set()
    for role in roles:
        caps |= expand_legacy_grants(config.get(role, frozenset()))
    return caps

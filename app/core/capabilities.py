"""Capability registry — the editable permission model (#164).

Authorization used to be hardcoded role allow-lists (``require_full_access`` =
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
    ALUMNI_FULL = "alumni.full"
    USER_ADMIN = "user_admin"
    VOCAB_ADMIN = "vocab_admin"
    PROFILE_COMPLETENESS = "profile.completeness"
    ENGINEER = "engineer"


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
            "Edit existing alumni and their related records — interactions, "
            "employment, education, leadership, tags, status labels, and tasks. "
            "Does not include creating or archiving alumni."
        ),
    ),
    CapabilitySpec(
        code=Capability.ALUMNI_FULL,
        label="Manage alumni & data",
        description=(
            "Create and archive alumni, import and export data, and manage "
            "events and the data-quality / tasks tooling."
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
        code=Capability.VOCAB_ADMIN,
        label="Vocabulary & dropdowns",
        description=(
            "Manage the controlled-vocabulary options that populate the app's "
            "dropdowns."
        ),
    ),
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


# --- Default grants (seed + empty-table fallback) -----------------------------
#
# Reproduces the historical hardcoded guards. Keyed by role NAME (the stable
# RoleName value), valued by the set of capability codes that role holds.
DEFAULT_GRANTS: dict[str, frozenset[str]] = {
    RoleName.ENGINEER.value: ALL_CAPABILITY_CODES,
    RoleName.SUPER_ADMIN.value: frozenset(
        {
            Capability.VIEW,
            Capability.ALUMNI_EDIT,
            Capability.ALUMNI_FULL,
            Capability.USER_ADMIN,
            Capability.PROFILE_COMPLETENESS,
        }
    ),
    RoleName.FULL_ACCESS.value: frozenset(
        {Capability.VIEW, Capability.ALUMNI_EDIT, Capability.ALUMNI_FULL}
    ),
    RoleName.STUDENT.value: frozenset({Capability.VIEW, Capability.ALUMNI_EDIT}),
    RoleName.VIEW_ONLY.value: frozenset({Capability.VIEW}),
}


def effective_capabilities(
    config: dict[str, frozenset[str]], roles: list[str] | set[str]
) -> set[str]:
    """The set of capability codes a user holding ``roles`` has under ``config``.

    The engineer is hard-overridden to hold **every** capability — a corrupt or
    incomplete config can never lock the engineer out of their own console or
    the permission editor. For every other role the result is the union of the
    capabilities granted to each role the user holds.
    """
    if RoleName.ENGINEER.value in roles:
        return set(ALL_CAPABILITY_CODES)
    caps: set[str] = set()
    for role in roles:
        caps |= config.get(role, frozenset())
    return caps

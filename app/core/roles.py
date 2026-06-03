"""Canonical role identifiers.

Only two roles exist (see CLAUDE.md). These string values are the stable
contract stored in ``roles.role_name`` and referenced throughout authorization;
the database is seeded with them in
``database/migrations/2026-06-03_seed_roles.sql``. UI labels can differ — these
are the machine identifiers, not display text.
"""

from enum import StrEnum


class RoleName(StrEnum):
    FULL_ACCESS = "full_access"
    VIEW_ONLY = "view_only"

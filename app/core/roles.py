"""Canonical role identifiers.

Three roles, most → least privileged: ``super_admin`` (user/role administration
+ everything full_access can do) ⊇ ``full_access`` (alumni write, import/export)
⊇ ``view_only`` (read). These string values are the stable contract stored in
``roles.role_name`` and referenced throughout authorization; the database is
seeded with them in ``database/migrations/2026-06-03_seed_roles.sql``. UI labels
can differ — these are the machine identifiers, not display text.
"""

from enum import StrEnum


class RoleName(StrEnum):
    SUPER_ADMIN = "super_admin"
    FULL_ACCESS = "full_access"
    VIEW_ONLY = "view_only"

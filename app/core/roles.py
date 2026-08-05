"""Canonical role identifiers.

Five roles, most → least privileged: ``engineer`` (everything, incl. database /
controlled-vocabulary administration) ⊇ ``super_admin`` (user/role administration
+ everything full_access can do) ⊇ ``full_access`` (alumni create/update,
archive, import) ⊇ ``student`` (edit EXISTING alumni records only — no create,
archive, restore, or import) ⊇ ``view_only`` (read). ``student`` is not a strict
subset of ``full_access`` in the "fewer rows" sense — it is a *narrower* writer
that can edit but not create — so it gets its own edit guard rather than being
layered into the create/archive guards (see ``app/api/dependencies/auth.py``).

These string values are the stable contract stored in ``roles.role_name`` and
referenced throughout authorization; the database is seeded with them in
``database/migrations/2026-06-03_seed_roles.sql`` and
``database/migrations/2026-06-16_seed_student_engineer_roles.sql``. UI labels can
differ — these are the machine identifiers, not display text. In particular the
``view_only`` role is surfaced in the UI as "Professor"; the machine id stays
``view_only``.
"""

from enum import StrEnum


class RoleName(StrEnum):
    ENGINEER = "engineer"
    SUPER_ADMIN = "super_admin"
    FULL_ACCESS = "full_access"
    STUDENT = "student"
    VIEW_ONLY = "view_only"


# Privilege ladder, most → least privileged. Drives stable ordering of the
# permission matrix / role-capabilities table.
ROLE_ORDER: tuple[RoleName, ...] = (
    RoleName.ENGINEER,
    RoleName.SUPER_ADMIN,
    RoleName.FULL_ACCESS,
    RoleName.STUDENT,
    RoleName.VIEW_ONLY,
)

# Display labels (the frontend mirrors these in src/constants/roles.ts). Note
# `view_only` is surfaced as "Professor"; the machine id stays `view_only`.
ROLE_LABELS: dict[str, str] = {
    RoleName.ENGINEER.value: "Engineer",
    RoleName.SUPER_ADMIN.value: "Super admin",
    RoleName.FULL_ACCESS.value: "Full access",
    RoleName.STUDENT.value: "Student",
    RoleName.VIEW_ONLY.value: "Professor",
}

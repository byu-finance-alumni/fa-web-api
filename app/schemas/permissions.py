"""Schemas for the engineer permission editor (#164) and capabilities table (#163)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CapabilityInfo(BaseModel):
    """One capability row in the matrix — code plus UI-facing copy.

    ``assignable`` is False for the engineer meta-capability, which the editor
    renders locked to the engineer (it cannot be granted to another role)."""

    code: str
    label: str
    description: str
    assignable: bool


class RoleGrants(BaseModel):
    """A role and the capability codes it currently holds.

    ``editable`` is False for the engineer (its grants are fixed — it always
    holds everything). ``label`` is the display name (``view_only`` → "Professor").
    """

    role: str
    label: str
    editable: bool
    capabilities: list[str]


class PermissionMatrix(BaseModel):
    """The full permission config: every capability and every role's grants.

    Ordered most → least privileged. The capabilities table (#163) renders the
    non-engineer roles; the permission editor (#164) renders the full matrix and
    toggles the editable cells."""

    capabilities: list[CapabilityInfo]
    roles: list[RoleGrants]


class PermissionToggleRequest(BaseModel):
    """Grant or revoke a single capability for a single role."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=100)
    capability: str = Field(min_length=1, max_length=100)
    granted: bool


class PreviewLogRequest(BaseModel):
    """Record that the engineer entered preview-as-role mode for ``role`` (#165)."""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=100)


class PreviewLogResponse(BaseModel):
    """Acknowledgement that a preview-as-role entry was logged."""

    status: str = "ok"

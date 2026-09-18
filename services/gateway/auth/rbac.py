"""Minimal RBAC + ABAC (M1, M13/plan section 30).

Deliberately small. `require_role`/`require_any_role` are pure RBAC --
role-string membership, nothing else. `require_tenant_match_or_role` is
the one ABAC primitive this platform needs: "does this identity's own
tenant_id match the resource being acted on, unless they hold an
override role that bypasses tenant scoping entirely." Not a policy
engine or rule DSL -- plan section 30 found exactly one shape of check
needed in more than one place (admin endpoints comparing caller vs.
resource tenant_id), so it gets one small function, not a framework.

This is what makes `manager` (tenant-scoped: can only touch their own
tenant) distinct from `platform_admin` (global override) -- before this
existed, "admin" was all-or-nothing, see admin_routes.py's retrofit.
"""
from __future__ import annotations

from .identity import AuthorizationError, Identity


def require_role(identity: Identity, role: str) -> None:
    if not identity.has_role(role):
        raise AuthorizationError(f"role '{role}' is required for this operation")


def require_any_role(identity: Identity, *roles: str) -> None:
    if not any(identity.has_role(r) for r in roles):
        raise AuthorizationError(f"one of roles {list(roles)} is required for this operation")


def require_tenant_match_or_role(identity: Identity, resource_tenant_id: str, *, override_role: str) -> None:
    """Raises unless the identity's own tenant owns `resource_tenant_id`,
    or the identity holds `override_role` (bypasses tenant scoping
    entirely -- e.g. platform_admin acting on any tenant)."""
    if identity.tenant_id != resource_tenant_id and not identity.has_role(override_role):
        raise AuthorizationError(
            f"tenant '{resource_tenant_id}' or role '{override_role}' is required for this operation"
        )

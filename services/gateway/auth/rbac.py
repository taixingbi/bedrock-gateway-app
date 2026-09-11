"""Minimal RBAC (M1).

Deliberately small: a role check and nothing else. ABAC (attribute-based
checks like "does this identity's tenant_id match the resource being
acted on") lives next to the resource it protects rather than here --
e.g. the M2 admin-tenant-state endpoint checks
`identity.tenant_id == path tenant_id or identity.has_role("platform_admin")`
inline, since that's a one-line policy specific to that one route, not a
general-purpose rule worth its own abstraction yet.
"""
from __future__ import annotations

from .identity import AuthorizationError, Identity


def require_role(identity: Identity, role: str) -> None:
    if not identity.has_role(role):
        raise AuthorizationError(f"role '{role}' is required for this operation")


def require_any_role(identity: Identity, *roles: str) -> None:
    if not any(identity.has_role(r) for r in roles):
        raise AuthorizationError(f"one of roles {list(roles)} is required for this operation")

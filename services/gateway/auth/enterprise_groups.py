"""Enterprise IdP group -> tenant/role mapping (plan section 34.2).

A real enterprise IdP (Okta, Entra ID) issues a `groups` claim -- an
array of IdP-side group names (`"AI-Gateway-Claims-Manager"`) -- not
this platform's own `tenant_id`/`application_id`/`roles` shape
directly. `auth/identity.py`'s `identity_from_claims()` already accepts
two claim shapes (plain, and Cognito's `custom:*`/`cognito:groups`),
both 1:1 -- a claim value maps to itself. Neither has the indirection a
`groups` claim needs. This module is that indirection, the OIDC-group
equivalent of `auth/aws_iam.py`'s `FileIamTenantResolver` for IAM
principal ARNs.

Deliberately conservative about ambiguity: a caller's matched groups
must all agree on the same `tenant_id` (spanning two tenants from one
token is a config error -- silently picking one would be a tenant
isolation bug, not a convenience) and `application_id` similarly; roles
are the union across every matched group (a caller in two groups gets
both groups' roles, the normal RBAC expectation).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

import yaml

from .identity import AuthError


@dataclass(frozen=True)
class EnterpriseGroupGrant:
    tenant_id: str
    application_id: str
    roles: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ResolvedGroupIdentity:
    tenant_id: str
    application_id: str
    roles: List[str]


class EnterpriseGroupResolver(Protocol):
    def resolve(self, groups: List[str]) -> ResolvedGroupIdentity:
        """Raises AuthError if no group matches, or if matched groups
        disagree on tenant_id/application_id."""
        ...

    def list_mappings(self) -> Dict[str, EnterpriseGroupGrant]:
        """M10-portal-style visibility: every configured group ->
        grant, same "configured, not observed" caveat
        IamTenantResolver.list_grants() already documents."""
        ...


def _resolve_from_mapping(
    groups: List[str], mapping: Dict[str, EnterpriseGroupGrant]
) -> ResolvedGroupIdentity:
    matched = [mapping[g] for g in groups if g in mapping]
    if not matched:
        raise AuthError(
            f"none of the token's groups {groups!r} are mapped to a tenant",
            code="UNKNOWN_ENTERPRISE_GROUP",
        )

    tenant_ids = {g.tenant_id for g in matched}
    if len(tenant_ids) > 1:
        raise AuthError(
            f"token's groups map to conflicting tenants {sorted(tenant_ids)!r} -- "
            "a single caller's groups must all belong to the same tenant",
            code="AMBIGUOUS_ENTERPRISE_GROUP_TENANT",
        )

    application_ids = {g.application_id for g in matched}
    if len(application_ids) > 1:
        raise AuthError(
            f"token's groups map to conflicting applications {sorted(application_ids)!r} -- "
            "a single caller's groups must all belong to the same application",
            code="AMBIGUOUS_ENTERPRISE_GROUP_APPLICATION",
        )

    roles: List[str] = []
    for g in matched:
        for role in g.roles:
            if role not in roles:
                roles.append(role)

    return ResolvedGroupIdentity(
        tenant_id=tenant_ids.pop(), application_id=application_ids.pop(), roles=roles
    )


class InMemoryEnterpriseGroupResolver:
    def __init__(self, mapping: Dict[str, EnterpriseGroupGrant]):
        self._mapping = dict(mapping)

    def resolve(self, groups: List[str]) -> ResolvedGroupIdentity:
        return _resolve_from_mapping(groups, self._mapping)

    def list_mappings(self) -> Dict[str, EnterpriseGroupGrant]:
        return dict(self._mapping)


class FileEnterpriseGroupResolver:
    """Loads a YAML file's `enterprise_groups` map once at startup --
    same loader shape as FileIamTenantResolver/FilePolicyStore.
    Tolerant of a missing file (empty mapping, every group then 403s
    with UNKNOWN_ENTERPRISE_GROUP) since not every environment uses a
    real enterprise IdP.

    Expected shape:
        enterprise_groups:
          "AI-Gateway-Claims-Manager":
            tenant_id: claims
            application_id: claims-portal
            roles: [manager]
    """

    def __init__(self, path: Optional[str]):
        mapping: Dict[str, EnterpriseGroupGrant] = {}
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            for group_name, entry in (data.get("enterprise_groups") or {}).items():
                entry = entry or {}
                mapping[group_name] = EnterpriseGroupGrant(
                    tenant_id=entry["tenant_id"],
                    application_id=entry["application_id"],
                    roles=list(entry.get("roles", [])),
                )
        self._resolver = InMemoryEnterpriseGroupResolver(mapping)

    def resolve(self, groups: List[str]) -> ResolvedGroupIdentity:
        return self._resolver.resolve(groups)

    def list_mappings(self) -> Dict[str, EnterpriseGroupGrant]:
        return self._resolver.list_mappings()

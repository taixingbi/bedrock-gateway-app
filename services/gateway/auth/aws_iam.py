"""AWS IAM (SigV4) caller identity, verified upstream.

This module does **not** verify SigV4 signatures -- that's API Gateway's
`AWS_IAM` authorizer's job (see infra/modules/api_gateway). By the time a
request reaches this app on the IAM route, API Gateway has already
verified the caller's SigV4 signature and overwritten
`x-platform-principal-arn`/`x-platform-account-id` with its own verified
`$context.identity.*` values, discarding whatever the client sent. On
every other route those same headers are stripped entirely. Trusting
them here is therefore only as safe as that infra invariant -- see
auth/identity.py's module docstring.

All this module does is map a verified IAM principal ARN to the
tenant_id/application_id/roles it should have, the IAM-path equivalent of
what a JWT's claims already carry directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

import yaml

from .identity import AuthError

HEADER_PRINCIPAL_ARN = "x-platform-principal-arn"
HEADER_ACCOUNT_ID = "x-platform-account-id"


@dataclass(frozen=True)
class IamPrincipalGrant:
    tenant_id: str
    application_id: str
    roles: List[str] = field(default_factory=list)


class IamTenantResolver(Protocol):
    def resolve(self, principal_arn: str) -> IamPrincipalGrant:
        """Return the grant for a verified IAM principal ARN, or raise AuthError."""
        ...


class FileIamTenantResolver:
    """Loads `policies/iam_tenants.yaml`'s `iam_principals` map once at
    startup, the IAM-path equivalent of `FilePolicyStore`/`tenants.yaml`.

    Matching: an exact ARN match wins; otherwise a pattern ending in "*"
    matches any ARN sharing that prefix (for
    "arn:...:assumed-role/<role>/*" session-name wildcards, since an
    assumed-role ARN's last segment is the caller-chosen session name,
    not something we can enumerate in advance).

    Tolerant of a missing file (empty mapping, so every principal 403s
    with UNKNOWN_IAM_PRINCIPAL) rather than failing app startup -- not
    every environment uses the IAM auth path.
    """

    def __init__(self, path: str):
        self._exact: Dict[str, IamPrincipalGrant] = {}
        self._prefixes: Dict[str, IamPrincipalGrant] = {}

        if not path or not os.path.exists(path):
            return

        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        for arn_pattern, entry in (data.get("iam_principals") or {}).items():
            grant = IamPrincipalGrant(
                tenant_id=entry["tenant_id"],
                application_id=entry["application_id"],
                roles=list(entry.get("roles") or []),
            )
            if arn_pattern.endswith("*"):
                self._prefixes[arn_pattern[:-1]] = grant
            else:
                self._exact[arn_pattern] = grant

    def resolve(self, principal_arn: str) -> IamPrincipalGrant:
        grant = self._exact.get(principal_arn)
        if grant is not None:
            return grant

        for prefix, candidate in self._prefixes.items():
            if principal_arn.startswith(prefix):
                return candidate

        raise AuthError(
            f"no tenant mapping for IAM principal '{principal_arn}'",
            code="UNKNOWN_IAM_PRINCIPAL",
        )

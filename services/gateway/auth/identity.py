"""The authenticated caller (M1).

`Identity` is built exclusively from a verified source -- a JWT's claims
(see jwt_verifier.py) or, for `auth_type="aws_iam"`, the caller identity
API Gateway's AWS_IAM authorizer already verified via SigV4 (see
auth/aws_iam.py) -- never from an arbitrary client-supplied header.
Plan section 5 calls this out for the JWT path explicitly: a request
carrying `X-Tenant-ID: finance` must not be trusted, because that would
let any caller claim any tenant. The same invariant holds for the IAM
path: `services/gateway/pipeline.py`'s `authenticate()` only takes the
`x-platform-principal-arn`/`x-platform-account-id` headers as
trustworthy because the ALB in front of this app is private and only
reachable through API Gateway's VPC Link, which overwrites those
headers with its own verified values on the AWS_IAM route and strips
them entirely on the JWT route (see infra/modules/api_gateway) -- a
client can never set them directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from .enterprise_groups import EnterpriseGroupResolver


class AuthError(Exception):
    """Raised when a request cannot be authenticated.

    `code` maps to the public error body; the route layer turns this into
    a 401 without needing to know anything about JWT/JWKS internals.
    """

    def __init__(self, message: str, *, code: str = "UNAUTHENTICATED"):
        super().__init__(message)
        self.code = code


class AuthorizationError(Exception):
    """Raised when an authenticated caller lacks a required role (403)."""

    def __init__(self, message: str, *, code: str = "FORBIDDEN"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Identity:
    sub: str
    tenant_id: str
    application_id: str
    roles: List[str] = field(default_factory=list)
    auth_type: str = "jwt"  # "jwt" | "aws_iam"
    account_id: Optional[str] = None

    def has_role(self, role: str) -> bool:
        return role in self.roles


def identity_from_claims(
    claims: dict, *, enterprise_group_resolver: Optional["EnterpriseGroupResolver"] = None
) -> Identity:
    """Build an Identity from verified JWT claims.

    Raises AuthError if a required claim is missing -- a token that
    verifies cryptographically but doesn't carry a tenant_id is still
    useless (and dangerous) here, since tenant resolution has nowhere
    else to fall back to (see module docstring).

    Three claim shapes are accepted, checked in this order:
      - Plain `tenant_id`/`application_id`/`roles` -- what
        auth/devkeys.py's mint_dev_token() produces, and the only
        shape before a real IdP existed.
      - Cognito's shape -- `custom:tenant_id`/`custom:application_id`
        (Cognito always prefixes custom schema attributes this way in
        the token) and `cognito:groups` for roles (a real JSON array
        claim from native Cognito group membership, not something we
        had to invent a custom attribute for -- Cognito custom
        attributes can't be arrays at all, only groups can).
      - A real enterprise IdP's (Okta/Entra ID) `groups` claim (plan
        section 34.2) -- only tried when neither of the above supplied
        a tenant_id AND `enterprise_group_resolver` is configured
        (see auth/enterprise_groups.py). Each matched group maps to
        this platform's own tenant_id/application_id/roles; ambiguity
        across groups is a hard error, not a silent pick.
    """
    sub = claims.get("sub")
    tenant_id = claims.get("tenant_id") or claims.get("custom:tenant_id")
    application_id = claims.get("application_id") or claims.get("custom:application_id")
    roles = claims.get("roles")
    if roles is None:
        roles = claims.get("cognito:groups") or []

    if not tenant_id and enterprise_group_resolver is not None:
        idp_groups = claims.get("groups")
        if isinstance(idp_groups, list) and idp_groups:
            resolved = enterprise_group_resolver.resolve(idp_groups)
            tenant_id = resolved.tenant_id
            application_id = resolved.application_id
            roles = resolved.roles

    missing = [
        name
        for name, value in (("sub", sub), ("tenant_id", tenant_id), ("application_id", application_id))
        if not value
    ]
    if missing:
        raise AuthError(f"token is missing required claim(s): {', '.join(missing)}")

    if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
        raise AuthError("token 'roles' claim must be a list of strings")

    return Identity(sub=sub, tenant_id=tenant_id, application_id=application_id, roles=list(roles))

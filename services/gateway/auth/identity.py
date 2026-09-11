"""The authenticated caller (M1).

`Identity` is built exclusively from verified JWT claims -- never from a
client-supplied header. Plan section 5 calls this out explicitly: a
request carrying `X-Tenant-ID: finance` must not be trusted, because that
would let any caller claim any tenant. `tenant_id` here always comes from
the `tenant_id` claim inside a token that already passed signature/issuer/
audience/expiry verification (see jwt_verifier.py).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


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

    def has_role(self, role: str) -> bool:
        return role in self.roles


def identity_from_claims(claims: dict) -> Identity:
    """Build an Identity from verified JWT claims.

    Raises AuthError if a required claim is missing -- a token that
    verifies cryptographically but doesn't carry a tenant_id is still
    useless (and dangerous) here, since tenant resolution has nowhere
    else to fall back to (see module docstring).
    """
    sub = claims.get("sub")
    tenant_id = claims.get("tenant_id")
    application_id = claims.get("application_id")
    roles = claims.get("roles") or []

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

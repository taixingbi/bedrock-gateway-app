"""Assembles the /v1/chat request pipeline (plan section 2):

    Auth -> Tenant/Kill-Switch -> Policy Snapshot -> Rate Limit
    -> Input Guardrail -> Cache Lookup
    -> Certified Router (retry+breaker+fallback) -> Bedrock
    -> Output Guardrail -> Cache Write -> Response
    -> Telemetry/Cost

Grows one stage per milestone. `api/routes.py` stays a thin HTTP adapter;
this module holds the actual orchestration logic so each stage is
unit-testable without going through Starlette's request/response
machinery.
"""
from __future__ import annotations

from typing import Optional

from .auth import rbac
from .auth.identity import AuthError, AuthorizationError, Identity, identity_from_claims
from .auth.jwt_verifier import TokenVerifier


class PipelineError(Exception):
    """A pipeline stage rejected the request. Carries enough to render an
    HTTP error response without the route handler knowing which stage
    (auth, kill switch, guardrail, ...) produced it."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def authenticate(authorization_header: Optional[str], *, token_verifier: TokenVerifier) -> Identity:
    """Stage 1: Auth. Verifies the bearer token and derives an Identity.

    tenant_id always comes from the verified token's claims -- a
    request-supplied header (e.g. X-Tenant-ID) is never consulted, so a
    caller cannot claim a tenant it doesn't hold a token for.
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise PipelineError(401, "UNAUTHENTICATED", "missing bearer token")

    token = authorization_header[len("Bearer "):].strip()
    if not token:
        raise PipelineError(401, "UNAUTHENTICATED", "missing bearer token")

    try:
        claims = token_verifier.verify(token)
        identity = identity_from_claims(claims)
    except AuthError as exc:
        raise PipelineError(401, exc.code, str(exc)) from exc

    return identity


def authorize(identity: Identity, *, required_role: str) -> None:
    """Stage 1b: RBAC. Raises PipelineError(403, ...) if the identity
    lacks the role required for this operation."""
    try:
        rbac.require_role(identity, required_role)
    except AuthorizationError as exc:
        raise PipelineError(403, exc.code, str(exc)) from exc

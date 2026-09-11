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
from .policy.cache import PolicySnapshotCache
from .policy.models import BLOCKING_STATES, TenantPolicy, TenantState, UnknownTenantError
from .policy.rate_limiter import TokenBucketRateLimiter

# THROTTLED tenants get 1/5th their configured rpm_limit rather than being
# blocked outright -- SUSPENDED/EMERGENCY_BLOCK (the kill switch) is what
# blocks entirely.
_THROTTLE_FACTOR = 5


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


def resolve_policy(identity: Identity, *, policy_cache: PolicySnapshotCache) -> TenantPolicy:
    """Stage 2: Policy Snapshot (plan section 8). Reads through the
    bounded-TTL, push-invalidated cache -- never a synchronous
    control-plane call on every request (plan section 9)."""
    try:
        return policy_cache.get(identity.tenant_id)
    except UnknownTenantError as exc:
        raise PipelineError(403, "TENANT_NOT_PROVISIONED", str(exc)) from exc


def enforce_kill_switch(policy: TenantPolicy) -> None:
    """Stage 3: Emergency Gate (plan section 7). Must run before rate
    limiting, cache lookup, and routing -- a SUSPENDED/EMERGENCY_BLOCK
    tenant's request must never reach the model."""
    if policy.state in BLOCKING_STATES:
        raise PipelineError(
            403, "TENANT_BLOCKED", f"tenant '{policy.tenant_id}' is {policy.state.value}"
        )


def enforce_rate_limit(policy: TenantPolicy, *, rate_limiter: TokenBucketRateLimiter) -> None:
    """Stage 4: Rate Limit, scoped per tenant_id (isolation invariant,
    plan section 1) so one tenant's burst never throttles another's."""
    effective_limit = policy.rpm_limit
    if policy.state == TenantState.THROTTLED:
        effective_limit = max(1, policy.rpm_limit // _THROTTLE_FACTOR)

    if not rate_limiter.allow(policy.tenant_id, rpm_limit=effective_limit):
        raise PipelineError(
            429, "QUOTA_EXCEEDED", f"tenant '{policy.tenant_id}' exceeded its rate limit"
        )


def enforce_model_allowlist(
    policy: TenantPolicy, *, requested_model: Optional[str], default_model: str
) -> str:
    """Resolves the model to invoke. An explicit request for a model
    outside the tenant's allowlist is rejected; an empty allowlist means
    "no restriction beyond the gateway default" so existing tenants don't
    need a models: list to keep working."""
    if requested_model is not None:
        if policy.models and requested_model not in policy.models:
            raise PipelineError(
                403,
                "MODEL_NOT_ALLOWED",
                f"model '{requested_model}' is not in tenant '{policy.tenant_id}' allowlist",
            )
        return requested_model

    if policy.models:
        return policy.models[0]
    return default_model

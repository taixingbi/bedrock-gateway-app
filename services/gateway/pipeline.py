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

from typing import Callable, Optional

from .auth import rbac
from .auth.aws_iam import IamTenantResolver
from .auth.identity import AuthError, AuthorizationError, Identity, identity_from_claims
from .auth.jwt_verifier import TokenVerifier
from .guardrails.client import GuardrailClient
from .guardrails.fail_closed import GuardrailUnavailableError, run_guardrail_check
from .guardrails.models import GuardrailAction, GuardrailDecision
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


def authenticate_iam(
    principal_arn: str, account_id: Optional[str], *, iam_tenant_resolver: IamTenantResolver
) -> Identity:
    """Stage 1 (AWS_IAM path): maps an already SigV4-verified IAM
    principal ARN to an Identity via `policies/iam_tenants.yaml`.

    `principal_arn` must only ever come from a request that reached this
    app through API Gateway's AWS_IAM route, which overwrites the
    x-platform-principal-arn/x-platform-account-id headers with its own
    verified $context.identity.* values -- see auth/aws_iam.py's module
    docstring for why that's safe to trust here.
    """
    try:
        grant = iam_tenant_resolver.resolve(principal_arn)
    except AuthError as exc:
        raise PipelineError(403, exc.code, str(exc)) from exc

    return Identity(
        sub=principal_arn,
        tenant_id=grant.tenant_id,
        application_id=grant.application_id,
        roles=grant.roles,
        auth_type="aws_iam",
        account_id=account_id,
    )


def authenticate(
    authorization_header: Optional[str],
    *,
    token_verifier: TokenVerifier,
    iam_principal_arn: Optional[str] = None,
    iam_account_id: Optional[str] = None,
    iam_tenant_resolver: Optional[IamTenantResolver] = None,
) -> Identity:
    """Stage 1: Auth. Derives an Identity from whichever verified source
    the request arrived through.

    If `iam_principal_arn` is set, the caller reached this app through
    API Gateway's AWS_IAM route (see authenticate_iam's docstring) and
    that takes precedence -- no bearer token is expected on that route.
    Otherwise, falls back to the existing JWT path: tenant_id always
    comes from the verified token's claims, never a request-supplied
    header (e.g. X-Tenant-ID), so a caller cannot claim a tenant it
    doesn't hold a token for.
    """
    if iam_principal_arn:
        if iam_tenant_resolver is None:
            raise PipelineError(500, "IAM_AUTH_NOT_CONFIGURED", "aws_iam auth is not configured")
        return authenticate_iam(iam_principal_arn, iam_account_id, iam_tenant_resolver=iam_tenant_resolver)

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


def check_input_guardrail(
    text: str, *, policy: TenantPolicy, guardrail_client: GuardrailClient
) -> GuardrailDecision:
    """Stage 5: Input Guardrail (plan sections 10-11). Raises
    PipelineError on BLOCK (400) or on fail-closed unavailability (503,
    AI_SAFETY_SERVICE_UNAVAILABLE) -- the model is never called in either
    case."""
    decision = _run_guardrail(
        lambda: guardrail_client.check_input(text, guardrail_policy=policy.guardrail_policy),
        policy=policy,
    )
    if decision.action == GuardrailAction.BLOCK:
        raise PipelineError(400, "INPUT_BLOCKED", decision.reason or "input blocked by guardrail")
    return decision


def check_output_guardrail(
    text: str, *, policy: TenantPolicy, guardrail_client: GuardrailClient
) -> GuardrailDecision:
    """Stage 6 (post-Bedrock): Output Guardrail. A blocked completion is
    never returned to the caller (502, OUTPUT_BLOCKED) -- same
    fail-closed contract as the input side."""
    decision = _run_guardrail(
        lambda: guardrail_client.check_output(text, guardrail_policy=policy.guardrail_policy),
        policy=policy,
    )
    if decision.action == GuardrailAction.BLOCK:
        raise PipelineError(502, "OUTPUT_BLOCKED", decision.reason or "output blocked by guardrail")
    return decision


def _run_guardrail(
    check: Callable[[], GuardrailDecision], *, policy: TenantPolicy
) -> GuardrailDecision:
    try:
        return run_guardrail_check(
            check,
            guardrail_policy=policy.guardrail_policy,
            allow_bypass_on_error=policy.allow_guardrail_bypass_on_error,
        )
    except GuardrailUnavailableError as exc:
        raise PipelineError(503, "AI_SAFETY_SERVICE_UNAVAILABLE", str(exc)) from exc

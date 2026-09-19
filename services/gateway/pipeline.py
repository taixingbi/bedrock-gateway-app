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

from typing import Callable, Dict, List, Optional, Set

from .auth.aws_iam import IamTenantResolver
from .auth.enterprise_groups import EnterpriseGroupResolver
from .auth.identity import AuthError, Identity, identity_from_claims
from .auth.jwt_verifier import TokenVerifier
from .authz.decision import Decision, decide
from .concurrency import ConcurrencyLimiter
from .guardrails.client import GuardrailClient
from .guardrails.fail_closed import GuardrailUnavailableError, run_guardrail_check
from .guardrails.models import GuardrailAction, GuardrailDecision
from .policy.cache import PolicySnapshotCache
from .policy.models import BLOCKING_STATES, TenantPolicy, TenantState, UnknownTenantError
from .policy.rate_limiter import TokenBucketRateLimiter
from .routing.model_registry import ModelRegistryEntry, ModelStatus, classification_rank
from .usage.store import UsageStore

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
    principal_arn: str,
    account_id: Optional[str],
    *,
    iam_tenant_resolver: IamTenantResolver,
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
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
        grant = iam_tenant_resolver.resolve(principal_arn, request_id=request_id, session_id=session_id)
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
    request_id: Optional[str] = None,
    session_id: Optional[str] = None,
    enterprise_group_resolver: Optional[EnterpriseGroupResolver] = None,
) -> Identity:
    """Stage 1: Auth. Derives an Identity from whichever verified source
    the request arrived through.

    If `iam_principal_arn` is set, the caller reached this app through
    API Gateway's AWS_IAM route (see authenticate_iam's docstring) and
    that takes precedence -- no bearer token is expected on that route.
    Otherwise, falls back to the existing JWT path: tenant_id always
    comes from the verified token's claims, never a request-supplied
    header (e.g. X-Tenant-ID), so a caller cannot claim a tenant it
    doesn't hold a token for. `enterprise_group_resolver` (plan section
    34.2), when configured, lets a real enterprise IdP's `groups` claim
    resolve to tenant_id/application_id/roles -- see
    auth/identity.py's identity_from_claims for the precedence order.
    """
    if iam_principal_arn:
        if iam_tenant_resolver is None:
            raise PipelineError(500, "IAM_AUTH_NOT_CONFIGURED", "aws_iam auth is not configured")
        return authenticate_iam(
            iam_principal_arn,
            iam_account_id,
            iam_tenant_resolver=iam_tenant_resolver,
            request_id=request_id,
            session_id=session_id,
        )

    if not authorization_header or not authorization_header.startswith("Bearer "):
        raise PipelineError(401, "UNAUTHENTICATED", "missing bearer token")

    token = authorization_header[len("Bearer "):].strip()
    if not token:
        raise PipelineError(401, "UNAUTHENTICATED", "missing bearer token")

    try:
        claims = token_verifier.verify(token)
        identity = identity_from_claims(claims, enterprise_group_resolver=enterprise_group_resolver)
    except AuthError as exc:
        raise PipelineError(401, exc.code, str(exc)) from exc

    return identity


def authorize(identity: Identity, *, required_role: str, action: str = "unspecified") -> Decision:
    """Stage 1b: RBAC. Raises PipelineError(403, ...) if the identity
    lacks the role required for this operation. Returns the PDP
    Decision (plan section 34.3) on success, so a caller that wants to
    thread decision_id/policy_version into the audit event can."""
    decision = decide(identity, action=action, required_role=required_role)
    if not decision.allow:
        raise PipelineError(403, decision.code, decision.reason)
    return decision


def authorize_any(identity: Identity, *, required_roles: List[str], action: str = "unspecified") -> Decision:
    """Stage 1b variant: any one of several roles suffices -- e.g. an
    admin endpoint reachable by either a tenant-scoped manager or a
    global platform_admin (plan section 30)."""
    decision = decide(identity, action=action, required_roles=required_roles)
    if not decision.allow:
        raise PipelineError(403, decision.code, decision.reason)
    return decision


def authorize_tenant_match(
    identity: Identity, resource_tenant_id: str, *, override_role: str, action: str = "unspecified",
    policy_version: Optional[int] = None,
) -> Decision:
    """Stage 1b ABAC variant (plan section 30): the identity's own
    tenant must own `resource_tenant_id`, unless it holds
    `override_role` (bypasses tenant scoping entirely)."""
    decision = decide(
        identity, action=action, resource_tenant_id=resource_tenant_id, override_role=override_role,
        policy_version=policy_version,
    )
    if not decision.allow:
        raise PipelineError(403, decision.code, decision.reason)
    return decision


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


def enforce_concurrency_limit(policy: TenantPolicy, *, concurrency_limiter: ConcurrencyLimiter) -> None:
    """Stage 4c (plan section 16's concurrency fix): fast-reject, not
    queue-and-wait -- raises PipelineError(429) immediately if the
    tenant's or the global slot budget is exhausted. Distinct from
    enforce_rate_limit: rpm_limit bounds request *rate*, this bounds how
    many blocking calls (guardrail checks, Bedrock inference) may be
    in flight for this tenant/globally at once.

    Callers MUST release the acquired slot (concurrency_limiter.release
    (policy.tenant_id)) once the guarded call finishes, success or
    failure, or it leaks permanently -- this function only acquires."""
    if not concurrency_limiter.try_acquire(policy.tenant_id, tenant_max=policy.max_concurrency):
        raise PipelineError(
            429, "CONCURRENCY_LIMIT_EXCEEDED",
            f"tenant '{policy.tenant_id}' exceeded its concurrent-request limit, or the gateway is globally saturated",
        )


def enforce_budget(policy: TenantPolicy, *, usage_store: UsageStore, month: str) -> None:
    """Stage 4b: FinOps hard budget (M8, plan section 20). None means
    unlimited -- most tenants don't opt in. Checked before the model is
    ever called; the actual spend increment happens only after a
    successful response (api/routes.py / jobs/processor.py), not here,
    so a request that itself fails or is blocked never counts against
    the budget it was checked against."""
    if policy.monthly_budget is None:
        return
    current = usage_store.get(policy.tenant_id, month)
    if current >= policy.monthly_budget:
        raise PipelineError(
            429,
            "BUDGET_EXCEEDED",
            f"tenant '{policy.tenant_id}' exceeded its monthly budget (${policy.monthly_budget:.2f})",
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


def enforce_model_certification(
    model_id: str,
    *,
    certified_model_ids: Set[str],
    model_registry: Optional[Dict[str, ModelRegistryEntry]] = None,
    tenant_data_classification: Optional[str] = None,
) -> Optional[str]:
    """Stage 4c: Routing Invariant (M9, plan sections 1 and 13). A model
    that hasn't passed evaluation/certification (evals/run_eval.py,
    policies/certified_models.yaml) must never receive production
    traffic -- checked here for the primary before the router is ever
    called; routing/router.py's CertifiedRouter separately filters
    fallback candidates against the same registry, so a certified
    primary can't fall back to an uncertified model either.

    Plan section 34.5: `model_registry`, when supplied, is a second,
    independent check -- a model can be in `certified_model_ids`
    (passed the eval gate once) yet BLOCKED/DEPRECATED in the registry
    (governance decided to retire it since); the registry wins. A
    CONDITIONAL model is still allowed to route -- this returns a
    warning string instead of raising, for the caller to log, rather
    than silently swallowing it.

    Plan section 34.4b: `tenant_data_classification`, when supplied
    alongside a registry entry with a resolvable
    `max_data_classification`, rejects a model whose data-handling
    ceiling is lower than the tenant's own classification (e.g. a PHI
    tenant routed at a model only cleared for INTERNAL data). Either
    side being unresolvable (not in `_CLASSIFICATION_RANK`) skips this
    check rather than guessing an ordering -- see
    routing/model_registry.py's `classification_rank`.
    """
    if model_id not in certified_model_ids:
        raise PipelineError(
            403,
            "MODEL_NOT_CERTIFIED",
            f"model '{model_id}' has not passed certification (see evals/run_eval.py)",
        )

    if model_registry is None:
        return None

    entry = model_registry.get(model_id)
    status = entry.status if entry is not None else ModelStatus.APPROVED

    if status in (ModelStatus.BLOCKED, ModelStatus.DEPRECATED):
        raise PipelineError(
            403,
            "MODEL_NOT_APPROVED",
            f"model '{model_id}' is {status.value} in the model registry (plan section 34.5)",
        )

    if entry is not None and entry.max_data_classification is not None:
        model_rank = classification_rank(entry.max_data_classification)
        tenant_rank = classification_rank(tenant_data_classification)
        if model_rank is not None and tenant_rank is not None and tenant_rank > model_rank:
            raise PipelineError(
                403,
                "DATA_CLASSIFICATION_EXCEEDS_MODEL_LIMIT",
                f"model '{model_id}' is only approved for data up to "
                f"'{entry.max_data_classification}', tenant requires "
                f"'{tenant_data_classification}' (plan section 34.4b)",
            )

    if status == ModelStatus.CONDITIONAL:
        return f"model '{model_id}' is CONDITIONAL in the model registry: {entry.notes or 'no notes'}"
    return None


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

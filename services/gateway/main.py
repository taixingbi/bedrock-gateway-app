"""App factory + entrypoint for the gateway-api service (M0).

Run locally:
    python -m services.gateway.main

Run under uvicorn directly (what the Dockerfile does):
    uvicorn services.gateway.main:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import uuid
from typing import Dict, Optional, Set

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from opentelemetry import trace
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from .api.admin_routes import build_admin_router
from .api.jobs_routes import build_jobs_router
from .api.onboarding_routes import build_onboarding_router
from .api.routes import build_router
from .auth.aws_iam import (
    DynamoDbIamTenantResolver,
    FileIamTenantResolver,
    HttpIamTenantResolver,
    IamTenantResolver,
    InMemoryIamTenantResolver,
    LayeredIamTenantResolver,
    ProvisionedIamTenantResolver,
)
from .auth.devkeys import load_or_create_dev_keypair
from .auth.jwt_verifier import JwksVerifier, StaticKeyVerifier, TokenVerifier
from .cache.store import InMemoryResponseCache, ResponseCache
from .config import Settings, load_settings
from .guardrails.basic_guardrail import BasicGuardrailClient
from .guardrails.client import GuardrailClient
from .inference.bedrock_client import BedrockClient, ConverseClient
from .jobs.queue import InMemoryJobQueue, JobQueue, SqsJobQueue
from .jobs.store import DynamoDbJobStore, InMemoryJobStore, JobStore
from .onboarding.audit import AuditStore, DynamoDbAuditStore, InMemoryAuditStore
from .onboarding.store import DynamoDbOnboardingStore, InMemoryOnboardingStore, OnboardingStore
from .policy.cache import PolicySnapshotCache
from .policy.rate_limiter import TokenBucketRateLimiter
from .policy.store import (
    DynamoDbPolicyStore,
    FilePolicyStore,
    InMemoryPolicyStore,
    LayeredPolicyStore,
    PolicyStore,
    ProvisionedPolicyStore,
)
from .routing.certification import certified_model_ids as _certified_model_ids_from
from .routing.certification import load_certified_models_from_yaml
from .routing.circuit_breaker import CircuitBreaker
from .routing.router import CertifiedRouter, RouteSet, load_route_sets_from_yaml
from .telemetry.debug_capture import DebugCaptureStore
from .telemetry.logging import configure_logging, get_logger, log_event
from .telemetry.middleware import RequestContextMiddleware
from .telemetry.otel import configure_tracing
from .usage.store import DynamoDbUsageStore, InMemoryUsageStore, UsageStore

_logger = get_logger("gateway.main")


def _build_default_token_verifier(settings: Settings) -> TokenVerifier:
    if settings.oidc_jwks_url:
        return JwksVerifier(
            jwks_url=settings.oidc_jwks_url,
            issuer=settings.oidc_issuer,
            audience=settings.oidc_audience,
            cache_ttl_s=settings.oidc_jwks_cache_ttl_s,
        )
    # No real OIDC provider configured -- local dev keypair (see
    # auth/devkeys.py and scripts/generate_dev_token.py).
    _private_pem, public_pem = load_or_create_dev_keypair(settings.dev_jwt_keypair_path)
    return StaticKeyVerifier(
        public_key_pem=public_pem, issuer=settings.oidc_issuer, audience=settings.oidc_audience
    )


def create_app(
    settings: Optional[Settings] = None,
    converse_client: Optional[ConverseClient] = None,
    token_verifier: Optional[TokenVerifier] = None,
    policy_store: Optional[PolicyStore] = None,
    guardrail_client: Optional[GuardrailClient] = None,
    response_cache: Optional[ResponseCache] = None,
    circuit_breaker: Optional[CircuitBreaker] = None,
    route_sets: Optional[Dict[str, RouteSet]] = None,
    tracer: Optional[trace.Tracer] = None,
    debug_capture_store: Optional[DebugCaptureStore] = None,
    iam_tenant_resolver: Optional[IamTenantResolver] = None,
    job_store: Optional[JobStore] = None,
    job_queue: Optional[JobQueue] = None,
    usage_store: Optional[UsageStore] = None,
    certified_model_ids: Optional[Set[str]] = None,
    onboarding_store: Optional[OnboardingStore] = None,
    onboarding_audit_store: Optional[AuditStore] = None,
    policy_store_primary: Optional[ProvisionedPolicyStore] = None,
    iam_tenant_resolver_primary: Optional[ProvisionedIamTenantResolver] = None,
) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(
        settings.service_name, settings.log_level, service=settings.service, environment=settings.environment
    )

    if converse_client is None:
        converse_client = BedrockClient(
            region=settings.aws_region,
            timeout_s=settings.bedrock_timeout_s,
            max_retries=settings.bedrock_max_retries,
        )
    if token_verifier is None:
        token_verifier = _build_default_token_verifier(settings)
    # M11: primary (provisioned-application) stores exist independently
    # of whether iam_tenant_resolver/policy_store were overridden below
    # -- onboarding_routes.py always needs something to write to.
    if iam_tenant_resolver_primary is None:
        iam_tenant_resolver_primary = (
            DynamoDbIamTenantResolver(
                table_name=settings.provisioned_principal_mappings_table_name, region=settings.aws_region
            )
            if settings.provisioned_principal_mappings_table_name
            else InMemoryIamTenantResolver()
        )
    if policy_store_primary is None:
        policy_store_primary = (
            DynamoDbPolicyStore(
                table_name=settings.provisioned_tenant_policies_table_name, region=settings.aws_region
            )
            if settings.provisioned_tenant_policies_table_name
            else InMemoryPolicyStore({})
        )

    # Only auto-layer the *default* file-based stores this function
    # constructs itself -- a caller-supplied policy_store/
    # iam_tenant_resolver (every existing test) is used exactly as
    # given, unwrapped, so nothing about their behavior changes.
    if iam_tenant_resolver is None:
        # M12: platform-authz-service does its own file+Dynamo layering
        # internally (same tables/policies file, read-only there) --
        # when it's configured, this app defers principal-mapping
        # entirely rather than doing it twice.
        iam_tenant_resolver = (
            HttpIamTenantResolver(base_url=settings.authz_service_url, ca_cert_pem=settings.authz_ca_cert_pem)
            if settings.authz_service_url
            else LayeredIamTenantResolver(
                primary=iam_tenant_resolver_primary,
                fallback=FileIamTenantResolver(settings.iam_tenants_path),
            )
        )
    if policy_store is None:
        policy_store = LayeredPolicyStore(
            primary=policy_store_primary,
            fallback=FilePolicyStore(settings.tenant_policy_path),
        )
    if guardrail_client is None:
        guardrail_client = BasicGuardrailClient()
    if response_cache is None:
        response_cache = InMemoryResponseCache(
            ttl_s=settings.response_cache_ttl_s, max_entries=settings.response_cache_max_entries
        )
    if circuit_breaker is None:
        circuit_breaker = CircuitBreaker(
            failure_threshold=settings.circuit_breaker_failure_threshold,
            reset_timeout_s=settings.circuit_breaker_reset_timeout_s,
        )
    if route_sets is None:
        route_sets = load_route_sets_from_yaml(settings.route_set_config_path)
    if certified_model_ids is None:
        certified_model_ids = _certified_model_ids_from(
            load_certified_models_from_yaml(settings.certified_models_path)
        )

    policy_cache = PolicySnapshotCache(store=policy_store, ttl_s=settings.policy_cache_ttl_s)
    rate_limiter = TokenBucketRateLimiter()
    router = CertifiedRouter(
        converse_client=converse_client,
        circuit_breaker=circuit_breaker,
        route_sets=route_sets,
        certified_model_ids=certified_model_ids,
    )
    if tracer is None:
        tracer = configure_tracing(
            settings.service_name, otlp_endpoint=settings.otel_exporter_otlp_endpoint or None
        )
    if debug_capture_store is None:
        debug_capture_store = DebugCaptureStore(ttl_s=settings.debug_capture_ttl_s)
    if job_store is None:
        job_store = (
            DynamoDbJobStore(table_name=settings.jobs_table_name, region=settings.aws_region)
            if settings.jobs_table_name
            else InMemoryJobStore()
        )
    if job_queue is None:
        job_queue = (
            SqsJobQueue(queue_url=settings.jobs_queue_url, region=settings.aws_region)
            if settings.jobs_queue_url
            else InMemoryJobQueue()
        )
    if usage_store is None:
        usage_store = (
            DynamoDbUsageStore(table_name=settings.usage_table_name, region=settings.aws_region)
            if settings.usage_table_name
            else InMemoryUsageStore()
        )
    if onboarding_store is None:
        onboarding_store = (
            DynamoDbOnboardingStore(table_name=settings.onboarding_requests_table_name, region=settings.aws_region)
            if settings.onboarding_requests_table_name
            else InMemoryOnboardingStore()
        )
    if onboarding_audit_store is None:
        onboarding_audit_store = (
            DynamoDbAuditStore(table_name=settings.onboarding_audit_table_name, region=settings.aws_region)
            if settings.onboarding_audit_table_name
            else InMemoryAuditStore()
        )

    router_ = build_router(
        router=router,
        settings=settings,
        token_verifier=token_verifier,
        iam_tenant_resolver=iam_tenant_resolver,
        policy_cache=policy_cache,
        rate_limiter=rate_limiter,
        guardrail_client=guardrail_client,
        response_cache=response_cache,
        circuit_breaker=circuit_breaker,
        tracer=tracer,
        debug_capture_store=debug_capture_store,
        usage_store=usage_store,
    )
    admin_router = build_admin_router(
        policy_store=policy_store,
        policy_cache=policy_cache,
        settings=settings,
        token_verifier=token_verifier,
        iam_tenant_resolver=iam_tenant_resolver,
        usage_store=usage_store,
        route_sets=route_sets,
        certified_model_ids=certified_model_ids,
    )
    jobs_router = build_jobs_router(
        settings=settings,
        token_verifier=token_verifier,
        iam_tenant_resolver=iam_tenant_resolver,
        policy_cache=policy_cache,
        rate_limiter=rate_limiter,
        guardrail_client=guardrail_client,
        job_store=job_store,
        job_queue=job_queue,
        usage_store=usage_store,
        certified_model_ids=certified_model_ids,
    )
    onboarding_router = build_onboarding_router(
        onboarding_store=onboarding_store,
        audit_store=onboarding_audit_store,
        policy_store=policy_store,
        policy_store_primary=policy_store_primary,
        iam_tenant_resolver=iam_tenant_resolver,
        iam_tenant_resolver_primary=iam_tenant_resolver_primary,
        settings=settings,
        token_verifier=token_verifier,
    )

    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        log_event(
            _logger, "ERROR", "unhandled exception",
            request_id=request_id, path=request.url.path, error=str(exc),
        )
        return JSONResponse(
            {"error": {"code": "INTERNAL_ERROR", "message": "internal error", "request_id": request_id}},
            status_code=500,
        )

    async def invalid_request_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        """Reformats FastAPI's default 422 validation-error shape into
        this gateway's existing ErrorResponse contract (400, a single
        code+message, request_id) -- every route handler used to do
        this by hand around a manual `await request.json()` +
        `Model.model_validate(body)`; this is the one place that logic
        lives now that request bodies are FastAPI-injected parameters.
        Malformed JSON syntax and a schema violation both raise this
        same exception, distinguished only by error type ("json_invalid"
        for the former), which is what INVALID_JSON vs INVALID_REQUEST
        below is keyed on."""
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        first_error = exc.errors()[0]
        code = "INVALID_JSON" if first_error.get("type") == "json_invalid" else "INVALID_REQUEST"
        return JSONResponse(
            {"error": {"code": code, "message": first_error.get("msg", "invalid request"), "request_id": request_id}},
            status_code=400,
        )

    app = FastAPI(
        title="Bedrock Gateway",
        middleware=[Middleware(RequestContextMiddleware)],
        exception_handlers={
            Exception: unhandled_error,
            RequestValidationError: invalid_request_body,
        },
    )
    app.include_router(router_)
    app.include_router(admin_router)
    app.include_router(jobs_router)
    app.include_router(onboarding_router)
    app.state.settings = settings
    return app


# Module-level `app` for `uvicorn services.gateway.main:app`. Constructing a
# real BedrockClient requires boto3 + AWS credentials, so this only works
# where both are present (the container / a properly configured dev box) --
# tests build their own app via create_app(converse_client=<fake>) instead.
try:
    app = create_app()
except Exception:  # pragma: no cover - boto3/creds not available at import time
    import traceback

    traceback.print_exc()  # a silently-None app just 500s with no clue why -- print the real cause
    app = None


if __name__ == "__main__":
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)

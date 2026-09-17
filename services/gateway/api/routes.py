"""HTTP handlers for the gateway API: /healthz, /v1/chat.

FastAPI (APIRouter), not raw Starlette routing -- gets free OpenAPI
docs/schema (/docs, /openapi.json) at the cost of request bodies being
parsed/validated by FastAPI's dependency injection *before* the
handler body runs, rather than by hand after auth. That reordering is
deliberate and judged safe: no pipeline stage's ordering guarantee
(plan section 1's invariants) is about body-shape-vs-auth precedence,
only about safety/policy checks happening before the model is ever
called -- a client sending both a bad token and a malformed body gets
some 4xx either way, and no test exercises that specific combination.
main.py's `invalid_request_body` exception handler reformats FastAPI's
default validation-error shape back into this gateway's existing
ErrorResponse contract (400, INVALID_JSON/INVALID_REQUEST, request_id)
so callers see no difference from before the migration.
"""
from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Request
from opentelemetry import trace
from starlette.responses import JSONResponse, StreamingResponse

from .. import pipeline
from ..auth import aws_iam
from ..auth.aws_iam import IamTenantResolver
from ..auth.jwt_verifier import TokenVerifier
from ..cache.keys import build_cache_key, normalize_messages
from ..cache.store import CachedResponse, ResponseCache
from ..config import Settings
from ..guardrails.client import GuardrailClient
from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError
from ..policy.cache import PolicySnapshotCache
from ..policy.rate_limiter import TokenBucketRateLimiter
from ..routing.circuit_breaker import CircuitBreaker
from ..routing.router import AllRoutesUnavailableError, CertifiedRouter
from ..streaming import stream_chat_response
from ..telemetry.cost import estimate_cost
from ..telemetry.debug_capture import DebugCaptureStore
from ..telemetry.logging import get_logger, log_event
from ..telemetry.otel import set_span_attributes
from ..telemetry.slo import slo_breached
from ..usage.store import UsageStore, current_month
from .errors import error_response as _error
from .schemas import ChatRequest, ChatResponse, Usage

_chat_logger = get_logger("gateway.chat")

# BedrockInvocationError.code -> (http_status, public error code)
_ERROR_STATUS_MAP = {
    "ThrottlingException": (429, "UPSTREAM_THROTTLED"),
    "ServiceUnavailableException": (503, "UPSTREAM_UNAVAILABLE"),
    "ModelTimeoutException": (504, "UPSTREAM_TIMEOUT"),
    "InternalServerException": (502, "UPSTREAM_ERROR"),
}
_DEFAULT_ERROR_STATUS = (502, "UPSTREAM_ERROR")


def build_router(
    *,
    router: CertifiedRouter,
    settings: Settings,
    token_verifier: TokenVerifier,
    iam_tenant_resolver: IamTenantResolver,
    policy_cache: PolicySnapshotCache,
    rate_limiter: TokenBucketRateLimiter,
    guardrail_client: GuardrailClient,
    response_cache: ResponseCache,
    circuit_breaker: CircuitBreaker,
    tracer: trace.Tracer,
    debug_capture_store: DebugCaptureStore,
    usage_store: UsageStore,
) -> APIRouter:
    api_router = APIRouter()

    @api_router.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @api_router.post("/v1/chat", response_model=ChatResponse, response_model_exclude_none=True)
    async def chat(request: Request, chat_request: ChatRequest):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        session_id = getattr(request.state, "session_id", "")

        with tracer.start_as_current_span("chat.request") as span:
            set_span_attributes(span, request_id=request_id, session_id=session_id or None)

            try:
                identity = pipeline.authenticate(
                    request.headers.get("authorization"),
                    token_verifier=token_verifier,
                    iam_principal_arn=request.headers.get(aws_iam.HEADER_PRINCIPAL_ARN),
                    iam_account_id=request.headers.get(aws_iam.HEADER_ACCOUNT_ID),
                    iam_tenant_resolver=iam_tenant_resolver,
                    request_id=request_id,
                    session_id=session_id or None,
                )
                pipeline.authorize(identity, required_role=settings.chat_required_role)
                policy = pipeline.resolve_policy(identity, policy_cache=policy_cache)
                pipeline.enforce_kill_switch(policy)
                pipeline.enforce_rate_limit(policy, rate_limiter=rate_limiter)
                pipeline.enforce_budget(policy, usage_store=usage_store, month=current_month())
            except pipeline.PipelineError as exc:
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                return _error(exc.status_code, exc.code, str(exc), request_id)

            set_span_attributes(
                span, tenant_id=identity.tenant_id, application_id=identity.application_id,
                policy_epoch=policy.policy_epoch, route_set=policy.route_set,
            )

            try:
                model_id = pipeline.enforce_model_allowlist(
                    policy, requested_model=chat_request.model, default_model=settings.bedrock_model_id
                )
                pipeline.enforce_model_certification(model_id, certified_model_ids=router.certified_model_ids)
            except pipeline.PipelineError as exc:
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                return _error(exc.status_code, exc.code, str(exc), request_id)

            set_span_attributes(span, model=model_id)

            combined_input_text = "\n".join(m.content for m in chat_request.messages)
            guardrail_start = time.perf_counter()
            try:
                pipeline.check_input_guardrail(
                    combined_input_text, policy=policy, guardrail_client=guardrail_client
                )
            except pipeline.PipelineError as exc:
                guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)
                set_span_attributes(
                    span, status=exc.status_code, error=str(exc),
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=guardrail_ms, blocked_reason=str(exc),
                )
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=exc.status_code,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=guardrail_ms,
                    blocked_reason=str(exc), error=str(exc),
                )
                return _error(exc.status_code, exc.code, str(exc), request_id)
            input_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)

            messages = [
                BedrockChatMessage(role=m.role, text=m.content) for m in chat_request.messages
            ]

            if chat_request.stream:
                # No cache, no output guardrail, no fallback for streaming
                # -- documented simplification, see streaming.py's module
                # docstring. No debug capture either (nothing to capture
                # up front; the full output isn't known until the stream
                # ends, and by then it's already been sent to the client).
                if not circuit_breaker.allow(model_id):
                    set_span_attributes(span, status=503, error="circuit open")
                    return _error(
                        503, "UPSTREAM_UNAVAILABLE",
                        f"model '{model_id}' is temporarily unavailable (circuit open)",
                        request_id,
                    )
                chunk_iter = router_converse_stream(
                    router, model_id=model_id, messages=messages,
                    max_tokens=chat_request.max_tokens, temperature=chat_request.temperature,
                )
                set_span_attributes(span, status=200, stream=True)
                return StreamingResponse(
                    stream_chat_response(
                        chunk_iter,
                        model_id=model_id,
                        request_id=request_id,
                        tenant_id=identity.tenant_id,
                        circuit_breaker=circuit_breaker,
                        is_disconnected=request.is_disconnected,
                    ),
                    media_type="text/event-stream",
                    headers={"x-request-id": request_id, "cache-control": "no-cache"},
                )

            cache_key = build_cache_key(
                tenant_id=identity.tenant_id,
                application_id=identity.application_id,
                policy=policy,
                model_id=model_id,
                max_tokens=chat_request.max_tokens,
                temperature=chat_request.temperature,
                messages=normalize_messages(chat_request.messages),
            )
            cached = response_cache.get(cache_key)

            if cached is not None:
                estimated_cost = estimate_cost(
                    cached.model_id, input_tokens=cached.input_tokens, output_tokens=cached.output_tokens
                )
                usage_store.add_and_get(identity.tenant_id, current_month(), estimated_cost)
                if policy.debug_capture_enabled:
                    debug_capture_store.capture(
                        request_id=request_id, tenant_id=identity.tenant_id,
                        input_text=combined_input_text, output_text=cached.text,
                    )
                set_span_attributes(
                    span, status=200, model=cached.model_id,
                    guardrail_version=policy.guardrail_policy, guardrail_action="ALLOW",
                    guardrail_latency_ms=input_guardrail_ms,
                    input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
                    latency_ms=0.0, retry_count=0, fallback=False, cache_hit=True,
                    estimated_cost=estimated_cost, slo_breach=False,
                )
                log_event(
                    _chat_logger, "INFO", "chat request completed",
                    request_id=request_id,
                    tenant_id=identity.tenant_id, route_set=policy.route_set, policy_epoch=policy.policy_epoch,
                    model=cached.model_id, guardrail_version=policy.guardrail_policy,
                    guardrail_action="ALLOW", blocked_reason=None,
                    input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
                    guardrail_latency_ms=input_guardrail_ms, ttft_ms=None, latency_ms=0.0,
                    retry_count=0, fallback=False, cache_hit=True,
                    estimated_cost=estimated_cost, slo_breach=False, status=200,
                )
                response = ChatResponse(
                    request_id=request_id,
                    model=cached.model_id,
                    output=cached.text,
                    stop_reason=cached.stop_reason,
                    usage=Usage(input_tokens=cached.input_tokens, output_tokens=cached.output_tokens),
                    latency_ms=0.0,
                    cache_hit=True,
                    fallback=False,
                )
                return JSONResponse(response.model_dump())

            start = time.perf_counter()
            try:
                routed = router.converse(
                    primary_model_id=model_id,
                    route_set_name=policy.route_set,
                    messages=messages,
                    max_tokens=chat_request.max_tokens,
                    temperature=chat_request.temperature,
                )
            except BedrockInvocationError as exc:
                status_code, error_code = _ERROR_STATUS_MAP.get(exc.code, _DEFAULT_ERROR_STATUS)
                set_span_attributes(span, status=status_code, error=str(exc))
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=status_code,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=str(exc),
                )
                return _error(status_code, error_code, str(exc), request_id)
            except AllRoutesUnavailableError as exc:
                set_span_attributes(span, status=503, error=str(exc))
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=model_id, status=503,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    latency_ms=round((time.perf_counter() - start) * 1000, 2),
                    error=str(exc),
                )
                return _error(503, "ALL_ROUTES_UNAVAILABLE", str(exc), request_id)

            result = routed.result

            guardrail_start = time.perf_counter()
            try:
                pipeline.check_output_guardrail(
                    result.text, policy=policy, guardrail_client=guardrail_client
                )
            except pipeline.PipelineError as exc:
                output_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)
                set_span_attributes(
                    span, status=exc.status_code, error=str(exc), model=routed.model_id,
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                    blocked_reason=str(exc),
                )
                log_event(
                    _chat_logger, "ERROR", "chat request failed",
                    request_id=request_id, model=routed.model_id, status=exc.status_code,
                    tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                    guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                    guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                    blocked_reason=str(exc), error=str(exc),
                )
                return _error(exc.status_code, exc.code, str(exc), request_id)
            output_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)

            response_cache.set(
                cache_key,
                CachedResponse(
                    text=result.text,
                    stop_reason=result.stop_reason,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    model_id=routed.model_id,
                ),
            )

            if policy.debug_capture_enabled:
                debug_capture_store.capture(
                    request_id=request_id, tenant_id=identity.tenant_id,
                    input_text=combined_input_text, output_text=result.text,
                )

            estimated_cost = estimate_cost(
                routed.model_id, input_tokens=result.input_tokens, output_tokens=result.output_tokens
            )
            usage_store.add_and_get(identity.tenant_id, current_month(), estimated_cost)
            breached = slo_breached(policy, result.latency_ms)

            set_span_attributes(
                span, status=200, model=routed.model_id,
                guardrail_version=policy.guardrail_policy, guardrail_action="ALLOW",
                guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                input_tokens=result.input_tokens, output_tokens=result.output_tokens,
                latency_ms=result.latency_ms, retry_count=result.retry_count,
                fallback=routed.fallback, cache_hit=False,
                estimated_cost=estimated_cost, slo_breach=breached,
            )
            log_event(
                _chat_logger, "INFO", "chat request completed",
                request_id=request_id,
                tenant_id=identity.tenant_id,
                route_set=policy.route_set,
                policy_epoch=policy.policy_epoch,
                model=routed.model_id,
                guardrail_version=policy.guardrail_policy,
                guardrail_action="ALLOW",
                blocked_reason=None,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                ttft_ms=None,
                latency_ms=result.latency_ms,
                retry_count=result.retry_count,
                fallback=routed.fallback,
                cache_hit=False,
                estimated_cost=estimated_cost,
                slo_breach=breached,
                status=200,
            )

            response = ChatResponse(
                request_id=request_id,
                model=routed.model_id,
                output=result.text,
                stop_reason=result.stop_reason,
                usage=Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
                latency_ms=result.latency_ms,
                cache_hit=False,
                fallback=routed.fallback,
            )
            return JSONResponse(response.model_dump())

    return api_router


def router_converse_stream(router: CertifiedRouter, *, model_id, messages, max_tokens, temperature):
    """Streaming bypasses CertifiedRouter's fallback loop (see module
    docstring) but still goes through the same underlying ConverseClient
    the router wraps, so streaming and non-streaming share one Bedrock
    client configuration."""
    return router.converse_client.converse_stream(
        model_id=model_id, messages=messages, max_tokens=max_tokens, temperature=temperature
    )

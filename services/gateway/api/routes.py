"""HTTP handlers for the gateway API: /healthz, /v1/chat.

Deliberately framework-light (Starlette, not FastAPI) so it runs and is
testable without a package installer reaching the internet -- see
docs/ROADMAP.md for why. Swapping to FastAPI later (for free OpenAPI docs,
section 3 of the plan) is a mechanical port: these handlers already do
their own Pydantic validation and return plain dicts.
"""
from __future__ import annotations

import time
import uuid

from opentelemetry import trace
from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

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
) -> list[Route]:
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def chat(request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        with tracer.start_as_current_span("chat.request") as span:
            set_span_attributes(span, request_id=request_id)

            try:
                identity = pipeline.authenticate(
                    request.headers.get("authorization"),
                    token_verifier=token_verifier,
                    iam_principal_arn=request.headers.get(aws_iam.HEADER_PRINCIPAL_ARN),
                    iam_account_id=request.headers.get(aws_iam.HEADER_ACCOUNT_ID),
                    iam_tenant_resolver=iam_tenant_resolver,
                )
                pipeline.authorize(identity, required_role=settings.chat_required_role)
                policy = pipeline.resolve_policy(identity, policy_cache=policy_cache)
                pipeline.enforce_kill_switch(policy)
                pipeline.enforce_rate_limit(policy, rate_limiter=rate_limiter)
            except pipeline.PipelineError as exc:
                set_span_attributes(span, status=exc.status_code, error=str(exc))
                return _error(exc.status_code, exc.code, str(exc), request_id)

            set_span_attributes(
                span, tenant_id=identity.tenant_id, application_id=identity.application_id,
                policy_epoch=policy.policy_epoch, route_set=policy.route_set,
            )

            try:
                body = await request.json()
            except Exception:
                set_span_attributes(span, status=400, error="invalid JSON")
                return _error(400, "INVALID_JSON", "request body must be valid JSON", request_id)

            try:
                chat_request = ChatRequest.model_validate(body)
            except ValidationError as exc:
                set_span_attributes(span, status=400, error=exc.errors()[0]["msg"])
                return _error(400, "INVALID_REQUEST", exc.errors()[0]["msg"], request_id)

            try:
                model_id = pipeline.enforce_model_allowlist(
                    policy, requested_model=chat_request.model, default_model=settings.bedrock_model_id
                )
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
                    request_id=request_id, model=cached.model_id, tenant_id=identity.tenant_id,
                    policy_epoch=policy.policy_epoch, route_set=policy.route_set,
                    guardrail_version=policy.guardrail_policy, guardrail_action="ALLOW",
                    guardrail_latency_ms=input_guardrail_ms, blocked_reason=None,
                    input_tokens=cached.input_tokens, output_tokens=cached.output_tokens,
                    ttft_ms=None, latency_ms=0.0, retry_count=0, fallback=False,
                    cache_hit=True, status=200, estimated_cost=estimated_cost, slo_breach=False,
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
                model=routed.model_id,
                tenant_id=identity.tenant_id,
                policy_epoch=policy.policy_epoch,
                route_set=policy.route_set,
                guardrail_version=policy.guardrail_policy,
                guardrail_action="ALLOW",
                guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                blocked_reason=None,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                ttft_ms=None,
                latency_ms=result.latency_ms,
                retry_count=result.retry_count,
                fallback=routed.fallback,
                cache_hit=False,
                status=200,
                estimated_cost=estimated_cost,
                slo_breach=breached,
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

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/chat", chat, methods=["POST"]),
    ]


def router_converse_stream(router: CertifiedRouter, *, model_id, messages, max_tokens, temperature):
    """Streaming bypasses CertifiedRouter's fallback loop (see module
    docstring) but still goes through the same underlying ConverseClient
    the router wraps, so streaming and non-streaming share one Bedrock
    client configuration."""
    return router.converse_client.converse_stream(
        model_id=model_id, messages=messages, max_tokens=max_tokens, temperature=temperature
    )

"""HTTP handlers for the gateway API (M0 subset: /healthz, /v1/chat).

Deliberately framework-light (Starlette, not FastAPI) so it runs and is
testable without a package installer reaching the internet -- see
docs/ROADMAP.md for why. Swapping to FastAPI later (for free OpenAPI docs,
section 3 of the plan) is a mechanical port: these handlers already do
their own Pydantic validation and return plain dicts.
"""
from __future__ import annotations

import time
import uuid

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .. import pipeline
from ..auth.jwt_verifier import TokenVerifier
from ..config import Settings
from ..guardrails.client import GuardrailClient
from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError, ConverseClient
from ..policy.cache import PolicySnapshotCache
from ..policy.rate_limiter import TokenBucketRateLimiter
from ..telemetry.logging import get_logger, log_event
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
    converse_client: ConverseClient,
    settings: Settings,
    token_verifier: TokenVerifier,
    policy_cache: PolicySnapshotCache,
    rate_limiter: TokenBucketRateLimiter,
    guardrail_client: GuardrailClient,
) -> list[Route]:
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def chat(request: Request) -> JSONResponse:
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))

        try:
            identity = pipeline.authenticate(
                request.headers.get("authorization"), token_verifier=token_verifier
            )
            pipeline.authorize(identity, required_role=settings.chat_required_role)
            policy = pipeline.resolve_policy(identity, policy_cache=policy_cache)
            pipeline.enforce_kill_switch(policy)
            pipeline.enforce_rate_limit(policy, rate_limiter=rate_limiter)
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        try:
            body = await request.json()
        except Exception:
            return _error(400, "INVALID_JSON", "request body must be valid JSON", request_id)

        try:
            chat_request = ChatRequest.model_validate(body)
        except ValidationError as exc:
            return _error(400, "INVALID_REQUEST", exc.errors()[0]["msg"], request_id)

        try:
            model_id = pipeline.enforce_model_allowlist(
                policy, requested_model=chat_request.model, default_model=settings.bedrock_model_id
            )
        except pipeline.PipelineError as exc:
            return _error(exc.status_code, exc.code, str(exc), request_id)

        combined_input_text = "\n".join(m.content for m in chat_request.messages)
        guardrail_start = time.perf_counter()
        try:
            pipeline.check_input_guardrail(
                combined_input_text, policy=policy, guardrail_client=guardrail_client
            )
        except pipeline.PipelineError as exc:
            log_event(
                _chat_logger, "ERROR", "chat request failed",
                request_id=request_id, model=model_id, status=exc.status_code,
                tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                guardrail_latency_ms=round((time.perf_counter() - guardrail_start) * 1000, 2),
                blocked_reason=str(exc), error=str(exc),
            )
            return _error(exc.status_code, exc.code, str(exc), request_id)
        input_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)

        messages = [
            BedrockChatMessage(role=m.role, text=m.content) for m in chat_request.messages
        ]

        start = time.perf_counter()
        try:
            result = converse_client.converse(
                model_id=model_id,
                messages=messages,
                max_tokens=chat_request.max_tokens,
                temperature=chat_request.temperature,
            )
        except BedrockInvocationError as exc:
            status_code, error_code = _ERROR_STATUS_MAP.get(exc.code, _DEFAULT_ERROR_STATUS)
            log_event(
                _chat_logger, "ERROR", "chat request failed",
                request_id=request_id, model=model_id, status=status_code,
                tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                latency_ms=round((time.perf_counter() - start) * 1000, 2),
                error=str(exc),
            )
            return _error(status_code, error_code, str(exc), request_id)

        guardrail_start = time.perf_counter()
        try:
            pipeline.check_output_guardrail(
                result.text, policy=policy, guardrail_client=guardrail_client
            )
        except pipeline.PipelineError as exc:
            output_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)
            log_event(
                _chat_logger, "ERROR", "chat request failed",
                request_id=request_id, model=model_id, status=exc.status_code,
                tenant_id=identity.tenant_id, policy_epoch=policy.policy_epoch,
                guardrail_version=policy.guardrail_policy, guardrail_action="BLOCK",
                guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
                blocked_reason=str(exc), error=str(exc),
            )
            return _error(exc.status_code, exc.code, str(exc), request_id)
        output_guardrail_ms = round((time.perf_counter() - guardrail_start) * 1000, 2)

        log_event(
            _chat_logger, "INFO", "chat request completed",
            request_id=request_id,
            model=model_id,
            tenant_id=identity.tenant_id,
            policy_epoch=policy.policy_epoch,
            route_set=policy.route_set,
            guardrail_version=policy.guardrail_policy,
            guardrail_action="ALLOW",
            guardrail_latency_ms=round(input_guardrail_ms + output_guardrail_ms, 2),
            blocked_reason=None,
            # Fields below are placeholders until the corresponding milestone
            # lands (M4 cache/fallback, M14 streaming). Kept here so the
            # schema doesn't change shape later -- see section 17 of the plan.
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            ttft_ms=None,
            latency_ms=result.latency_ms,
            retry_count=result.retry_count,
            fallback=False,
            cache_hit=False,
            status=200,
        )

        response = ChatResponse(
            request_id=request_id,
            model=model_id,
            output=result.text,
            stop_reason=result.stop_reason,
            usage=Usage(input_tokens=result.input_tokens, output_tokens=result.output_tokens),
            latency_ms=result.latency_ms,
        )
        return JSONResponse(response.model_dump())

    return [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/v1/chat", chat, methods=["POST"]),
    ]

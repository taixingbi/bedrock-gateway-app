"""Request-scoped context: request_id + session_id + duration_ms tracking.

Every request gets a request_id (reused from the `X-Request-Id` inbound
header when the caller supplied one, so it survives retries/proxies;
otherwise generated here). It is stashed on `request.state.request_id`,
exposed via a ContextVar for code that doesn't have the Request object
handy, echoed back on the response, and used to correlate every log line
for that request.

session_id works the same way except it's never invented when absent
(read from `X-Session-Id`, empty if the caller didn't send one) -- a
random session_id wouldn't actually group anything, unlike request_id
where any unique value is useful. api_gateway_request_id is the same
idea again, but read from `X-Apigw-Request-Id` -- platform-api-gateway
maps its own `$context.requestId` onto this header explicitly (not
something AWS adds by itself, and not a header any client sets); the
join key back to that repo's own access log (see telemetry/logging.py's
module docstring for the full picture).

This middleware also opens the OUTER span for the whole request
(everything downstream, including route handlers' own child spans,
nests under it) -- the *reason* being that this is also where the
`gateway.access` "request completed" line gets logged, in the
`finally` block, and JsonFormatter (telemetry/logging.py) only picks
up trace_id/span_id from whatever span is current *at log time*. A
route handler's own span has already ended by the time this runs, so
without a span opened here, gateway.access lines would never carry
trace_id/span_id at all. Skipped for /healthz -- polled every 10-15s
forever, and X-Ray bills per trace recorded, so a span (and CloudWatch
cost) for those pings would be pure waste.
"""
from __future__ import annotations

import contextlib
import contextvars
import time
import uuid

from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging import api_gateway_request_id_ctx, get_logger, log_event, session_id_ctx

_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gateway_request_id", default="-"
)

_access_logger = get_logger("gateway.access")
_tracer = trace.get_tracer(__name__)


def current_request_id() -> str:
    return _request_id_ctx.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        session_id = request.headers.get("x-session-id") or ""
        # platform-api-gateway's own $context.requestId, mapped onto
        # this header explicitly (not a header any client sets, and not
        # something AWS adds by itself -- confirmed live: the bare name
        # "apigw-requestid" is AWS-reserved and 400s any mapping
        # operation at all, x- prefixed like every other custom header
        # here). Same value platform-api-gateway's own access log calls
        # api_gateway_request_id, so the two logs can be joined on it.
        api_gateway_request_id = request.headers.get("x-apigw-request-id") or ""
        request_token = _request_id_ctx.set(request_id)
        session_token = session_id_ctx.set(session_id)
        api_gateway_request_id_token = api_gateway_request_id_ctx.set(api_gateway_request_id)
        request.state.request_id = request_id
        request.state.session_id = session_id

        span_cm = (
            contextlib.nullcontext(None)
            if request.url.path == "/healthz"
            else _tracer.start_as_current_span("http.request")
        )

        start = time.perf_counter()
        status_code = 500
        try:
            with span_cm as request_span:
                if request_span is not None:
                    request_span.set_attribute("request_id", request_id)
                    request_span.set_attribute("http.method", request.method)
                    request_span.set_attribute("http.target", request.url.path)
                    if session_id:
                        request_span.set_attribute("session_id", session_id)
                    if api_gateway_request_id:
                        request_span.set_attribute("api_gateway_request_id", api_gateway_request_id)

                try:
                    response = await call_next(request)
                    status_code = response.status_code
                    return response
                finally:
                    duration_ms = round((time.perf_counter() - start) * 1000, 2)
                    # /healthz is polled every 10-15s by the ALB target
                    # group and the container's own Docker HEALTHCHECK,
                    # forever -- logging every successful ping drowns
                    # out real request logs for no benefit. A *failing*
                    # health check is still logged; that's the one case
                    # worth knowing about.
                    if request.url.path != "/healthz" or status_code != 200:
                        log_event(
                            _access_logger,
                            "INFO",
                            "request completed",
                            request_id=request_id,
                            method=request.method,
                            path=request.url.path,
                            status=status_code,
                            duration_ms=duration_ms,
                        )
                    if request_span is not None:
                        request_span.set_attribute("status", status_code)
        finally:
            _request_id_ctx.reset(request_token)
            session_id_ctx.reset(session_token)
            api_gateway_request_id_ctx.reset(api_gateway_request_id_token)
            try:
                response.headers["x-request-id"] = request_id
                if session_id:
                    response.headers["x-session-id"] = session_id
            except UnboundLocalError:
                pass

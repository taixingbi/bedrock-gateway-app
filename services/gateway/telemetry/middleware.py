"""Request-scoped context: request_id + duration_ms tracking.

Every request gets a request_id (reused from the `X-Request-Id` inbound
header when the caller supplied one, so it survives retries/proxies;
otherwise generated here). It is stashed on `request.state.request_id`,
exposed via a ContextVar for code that doesn't have the Request object
handy, echoed back on the response, and used to correlate every log line
for that request.
"""
from __future__ import annotations

import contextvars
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from .logging import get_logger, log_event

_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gateway_request_id", default="-"
)

_access_logger = get_logger("gateway.access")


def current_request_id() -> str:
    return _request_id_ctx.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = _request_id_ctx.set(request_id)
        request.state.request_id = request_id

        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
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
            _request_id_ctx.reset(token)
            try:
                response.headers["x-request-id"] = request_id
            except UnboundLocalError:
                pass

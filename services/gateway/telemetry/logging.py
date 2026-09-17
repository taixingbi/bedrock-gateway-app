"""Structured JSON logging for the gateway.

Field order is fixed and intentional (mirrors the convention already used
for the vLLM gateway's `layer-gateway-llm-inference-v1` structured logs):

    ts -> level -> service -> environment -> logger -> message
    -> request_id -> api_gateway_request_id -> trace_id -> span_id
    -> session_id -> <event-specific fields> -> error

`service`/`environment` are fixed per-process (e.g. "gateway-api"/"dev"),
never per-request -- they exist so a log aggregated across every
service's CloudWatch log group (or a future centralized Loki/ELK
instance) can be filtered/grouped without needing to know which log
group it came from. Same two fields, same position, in
platform-authz-service's own copy of this module and in
platform-api-gateway's access log format.

`trace_id`/`span_id` are pulled automatically from whatever OTel span is
current when the log call happens (telemetry/otel.py) -- omitted
entirely when there is none (an invalid/no-op span context), never
fabricated. `session_id` comes from `session_id_ctx`, set by
telemetry/middleware.py from the inbound `x-session-id` header (empty
when the caller didn't send one) -- unlike request_id, no session_id
is invented when absent, since a made-up one wouldn't actually group
anything. `api_gateway_request_id` comes from `api_gateway_request_id_ctx`,
set from the inbound `X-Apigw-Request-Id` header -- platform-api-gateway
maps its own `$context.requestId` onto this header explicitly (the bare
name "apigw-requestid" is AWS-reserved, confirmed live: 400s any
mapping operation at all, read or write), carrying the SAME value that
repo's own access log calls `api_gateway_request_id` (renamed from the
AWS default `requestId` for exactly this reason), so the two logs can
be joined on it even though that access log can carry none of the other
IDs here (see platform-api-gateway's own module comment -- API Gateway
access logs can't read arbitrary request headers at all). Absent
entirely for calls that never went through API Gateway (e.g. local
dev). All three are ContextVars rather than explicit log_event()
arguments so every call site gets them for free, the same way every
call site already gets `service`/`environment` for free.

Every log line is one JSON object on one line (easy to ship to
CloudWatch/Loki and to grep in dev). `error` is only present when a value
was actually passed, so success lines don't carry a stray `"error": null`.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
from typing import Any, Optional

from opentelemetry import trace

_RESERVED_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}

# Set by telemetry/middleware.py's RequestContextMiddleware from the
# inbound x-session-id header; read here so every log line picks it up
# without each call site having to pass it explicitly.
session_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("gateway_session_id", default="")

# Same idea, from the inbound X-Apigw-Request-Id header -- see module
# docstring.
api_gateway_request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gateway_api_gateway_request_id", default=""
)


class JsonFormatter(logging.Formatter):
    """Renders a LogRecord as one ordered JSON object per line."""

    def __init__(self, *, service: str = "", environment: str = "") -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        extra = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED_LOGRECORD_KEYS and not k.startswith("_")
        }
        request_id = extra.pop("request_id", None)
        error = extra.pop("error", None)

        ordered: dict[str, Any] = {
            "ts": _iso_ts(record.created),
            "level": record.levelname,
            "service": self._service,
            "environment": self._environment,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id,
        }

        api_gateway_request_id = api_gateway_request_id_ctx.get()
        if api_gateway_request_id:
            ordered["api_gateway_request_id"] = api_gateway_request_id

        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            ordered["trace_id"] = format(span_context.trace_id, "032x")
            ordered["span_id"] = format(span_context.span_id, "016x")

        session_id = session_id_ctx.get()
        if session_id:
            ordered["session_id"] = session_id

        ordered.update(extra)
        if error is not None:
            ordered["error"] = error
        elif record.exc_info:
            ordered["error"] = self.formatException(record.exc_info)

        return json.dumps(ordered, default=str)


def _iso_ts(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_seconds)) + (
        f".{int((epoch_seconds % 1) * 1000):03d}Z"
    )


def configure_logging(
    service_name: str, level: str = "INFO", *, service: str = "", environment: str = ""
) -> None:
    """Configure the root logger to emit structured JSON on stdout.

    `service_name` names the logger whose level this sets (e.g.
    "gateway-dev", historically also used as this process's OTel
    resource name) -- unrelated to `service`/`environment`, the fixed
    identity fields stamped onto every emitted line (see module
    docstring).

    Idempotent -- safe to call more than once (e.g. once from app startup,
    once from a test fixture).
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Avoid duplicate handlers if configure_logging() is called twice.
    root.handlers = [h for h in root.handlers if not isinstance(h, _GatewayStreamHandler)]

    handler = _GatewayStreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service=service, environment=environment))
    root.addHandler(handler)

    logging.getLogger(service_name).setLevel(level.upper())


class _GatewayStreamHandler(logging.StreamHandler):
    """Marker subclass so configure_logging() can find/replace its own handler."""


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(
    logger: logging.Logger,
    level: str,
    message: str,
    *,
    request_id: Optional[str] = None,
    error: Optional[str] = None,
    **fields: Any,
) -> None:
    """Emit one structured log line.

    Example:
        log_event(
            logger, "INFO", "chat request completed",
            request_id=req_id, model=model_id, latency_ms=123,
            input_tokens=42, output_tokens=17, status=200,
        )
    """
    log_fn = getattr(logger, level.lower(), logger.info)
    log_fn(message, extra={"request_id": request_id, "error": error, **fields})

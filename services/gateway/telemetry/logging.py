"""Structured JSON logging for the gateway.

Field order is fixed and intentional (mirrors the convention already used
for the vLLM gateway's `layer-gateway-llm-inference-v1` structured logs):

    ts -> level -> logger -> request_id -> <event-specific fields> -> message -> error

Every log line is one JSON object on one line (easy to ship to
CloudWatch/Loki and to grep in dev). `error` is only present when a value
was actually passed, so success lines don't carry a stray `"error": null`.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Optional


_RESERVED_LOGRECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Renders a LogRecord as one ordered JSON object per line."""

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
            "logger": record.name,
            "request_id": request_id,
        }
        ordered.update(extra)
        ordered["message"] = record.getMessage()
        if error is not None:
            ordered["error"] = error
        elif record.exc_info:
            ordered["error"] = self.formatException(record.exc_info)

        return json.dumps(ordered, default=str)


def _iso_ts(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch_seconds)) + (
        f".{int((epoch_seconds % 1) * 1000):03d}Z"
    )


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Configure the root logger to emit structured JSON on stdout.

    Idempotent -- safe to call more than once (e.g. once from app startup,
    once from a test fixture).
    """
    root = logging.getLogger()
    root.setLevel(level.upper())

    # Avoid duplicate handlers if configure_logging() is called twice.
    root.handlers = [h for h in root.handlers if not isinstance(h, _GatewayStreamHandler)]

    handler = _GatewayStreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
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

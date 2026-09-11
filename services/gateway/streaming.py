"""SSE streaming + client-disconnect cancellation (M4, plan section 14).

Kept as a small, pure async generator independent of Starlette's Request
so the cancellation behavior -- the actual thing this milestone needs to
prove -- is unit-testable without simulating a real ASGI disconnect:
`is_disconnected` is just an async predicate; tests pass a fake one that
flips True after N calls, and a fake chunk iterator that records whether
`.close()` reached it (via GeneratorExit).

Deliberately scoped down from a full production streaming story -- both
limitations are called out in docs/ROADMAP.md:
  - no output guardrail on the streamed text: blocking after the fact
    can't un-send tokens already streamed to the client. A real system
    needs incremental/partial-buffer guardrail checks, which is its own
    milestone-sized feature.
  - no certified-router fallback mid-stream: switching models after
    tokens have already reached the client isn't a clean retry.
The circuit breaker still gates and records the single attempt (see
api/routes.py), so a model already known to be failing doesn't get a
streaming request either.
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator, Awaitable, Callable, Iterator, Optional

from .inference.bedrock_client import BedrockInvocationError, StreamChunk
from .routing.circuit_breaker import CircuitBreaker
from .telemetry.logging import get_logger, log_event

_logger = get_logger("gateway.chat.stream")


async def stream_chat_response(
    chunks: Iterator[StreamChunk],
    *,
    model_id: str,
    request_id: str,
    tenant_id: str,
    circuit_breaker: CircuitBreaker,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[bytes]:
    generated_chunks = 0
    aborted = False
    final: Optional[StreamChunk] = None
    start = time.perf_counter()

    try:
        for chunk in chunks:
            if await is_disconnected():
                aborted = True
                break
            if chunk.text_delta:
                generated_chunks += 1
                yield _sse(data={"delta": chunk.text_delta})
            if chunk.is_final:
                final = chunk
    except BedrockInvocationError as exc:
        circuit_breaker.record_failure(model_id)
        yield _sse(data={"error": {"code": "UPSTREAM_ERROR", "message": str(exc), "request_id": request_id}})
        yield _sse(event="done", data={})
        log_event(
            _logger, "ERROR", "chat stream failed",
            request_id=request_id, model=model_id, tenant_id=tenant_id, error=str(exc),
        )
        return
    finally:
        close = getattr(chunks, "close", None)
        if callable(close):
            close()

    if not aborted:
        circuit_breaker.record_success(model_id)

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    yield _sse(
        data={
            "done": True,
            "aborted": aborted,
            "stop_reason": final.stop_reason if final else None,
            "usage": {
                "input_tokens": final.input_tokens if final else None,
                "output_tokens": final.output_tokens if final else None,
            },
        }
    )

    log_event(
        _logger, "INFO", "chat stream completed",
        request_id=request_id, model=model_id, tenant_id=tenant_id,
        client_disconnected=aborted, generated_chunks=generated_chunks,
        stream_duration_ms=duration_ms, status=200,
    )


def _sse(*, data: dict, event: Optional[str] = None) -> bytes:
    lines = []
    if event:
        lines.append(f"event: {event}")
    lines.append(f"data: {json.dumps(data)}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")

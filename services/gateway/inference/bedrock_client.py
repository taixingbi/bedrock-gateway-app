"""Thin wrapper around the Bedrock Converse API.

Deliberately narrow: one method (`converse`), one request shape (a list of
role/content messages), one result shape (`ConverseResult`). This is the
seam M4 (retry/circuit-breaker/certified-fallback) and M12 (certified model
router) will wrap -- keep it boring so those can be added without touching
callers.

boto3 is imported lazily (only when a real BedrockClient is constructed) so
this module stays importable -- and therefore the whole gateway package
stays importable and unit-testable -- in environments where boto3 isn't
installed yet (e.g. this sandbox). Tests inject a fake object that
implements the same `converse(...)` signature instead of a real
BedrockClient; see tests/fakes.py.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Iterator, List, Optional, Protocol


@dataclass(frozen=True)
class BedrockChatMessage:
    role: str  # "user" | "assistant"
    text: str


@dataclass(frozen=True)
class ConverseResult:
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: Optional[str]
    latency_ms: float
    retry_count: int = 0


@dataclass(frozen=True)
class StreamChunk:
    """One event from converse_stream(). A chunk either carries a text
    delta or (on the final chunk) the completed message's stop_reason/
    usage -- never both, so callers can check `is_final` rather than
    guessing from which fields are populated."""

    text_delta: str = ""
    is_final: bool = False
    stop_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class BedrockInvocationError(Exception):
    """Raised when a Bedrock invocation ultimately fails (after retries).

    `retryable` and `code` let the API layer map this to the right HTTP
    status/error body without knowing anything about botocore.
    """

    def __init__(self, message: str, *, code: str, retryable: bool):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ConverseClient(Protocol):
    """The interface routes.py actually depends on. Both BedrockClient and
    the fakes used in tests satisfy this without needing a shared base
    class."""

    def converse(
        self,
        *,
        model_id: str,
        messages: List[BedrockChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> ConverseResult: ...

    def converse_stream(
        self,
        *,
        model_id: str,
        messages: List[BedrockChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        """No retry/backoff here (M4, plan section 14): retrying a
        partially-streamed response would mean re-sending already-emitted
        tokens to the client, which isn't a clean retry. A failure
        mid-stream surfaces as BedrockInvocationError raised from the
        generator; the caller (streaming.py) turns that into an SSE error
        event rather than an HTTP status code, since headers are already
        sent by the time streaming starts."""
        ...


_RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
}


class BedrockClient:
    """Real Bedrock-backed implementation of ConverseClient."""

    def __init__(
        self,
        *,
        region: str,
        timeout_s: float = 30.0,
        max_retries: int = 2,
        base_backoff_s: float = 0.25,
    ):
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:  # pragma: no cover - exercised only when boto3 truly missing
            raise RuntimeError(
                "boto3 is required to construct a real BedrockClient. "
                "Install it (see requirements.txt) or inject a fake ConverseClient for tests."
            ) from exc

        self._max_retries = max_retries
        self._base_backoff_s = base_backoff_s
        self._client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=BotoConfig(
                connect_timeout=timeout_s,
                read_timeout=timeout_s,
                retries={"max_attempts": 0},  # we own retry/backoff/jitter ourselves
            ),
        )

    def converse(
        self,
        *,
        model_id: str,
        messages: List[BedrockChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> ConverseResult:
        bedrock_messages = [
            {"role": m.role, "content": [{"text": m.text}]} for m in messages
        ]

        attempt = 0
        start = time.perf_counter()
        while True:
            try:
                response = self._client.converse(
                    modelId=model_id,
                    messages=bedrock_messages,
                    inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
                )
                latency_ms = round((time.perf_counter() - start) * 1000, 2)
                output_message = response["output"]["message"]
                text = "".join(
                    block.get("text", "") for block in output_message.get("content", [])
                )
                usage = response.get("usage", {})
                return ConverseResult(
                    text=text,
                    input_tokens=usage.get("inputTokens", 0),
                    output_tokens=usage.get("outputTokens", 0),
                    stop_reason=response.get("stopReason"),
                    latency_ms=latency_ms,
                    retry_count=attempt,
                )
            except Exception as exc:  # narrowed below via botocore inspection
                error_code = _extract_error_code(exc)
                retryable = error_code in _RETRYABLE_ERROR_CODES
                attempt += 1
                if not retryable or attempt > self._max_retries:
                    raise BedrockInvocationError(
                        f"Bedrock invocation failed after {attempt} attempt(s): {exc}",
                        code=error_code or "UPSTREAM_ERROR",
                        retryable=retryable,
                    ) from exc
                # bounded exponential backoff + full jitter (section 15/16 of the plan)
                sleep_s = self._base_backoff_s * (2 ** (attempt - 1))
                time.sleep(random.uniform(0, sleep_s))

    def converse_stream(
        self,
        *,
        model_id: str,
        messages: List[BedrockChatMessage],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> Iterator[StreamChunk]:
        bedrock_messages = [
            {"role": m.role, "content": [{"text": m.text}]} for m in messages
        ]

        try:
            response = self._client.converse_stream(
                modelId=model_id,
                messages=bedrock_messages,
                inferenceConfig={"maxTokens": max_tokens, "temperature": temperature},
            )
        except Exception as exc:
            error_code = _extract_error_code(exc)
            raise BedrockInvocationError(
                f"Bedrock stream invocation failed: {exc}",
                code=error_code or "UPSTREAM_ERROR",
                retryable=False,
            ) from exc

        stop_reason: Optional[str] = None
        input_tokens: Optional[int] = None
        output_tokens: Optional[int] = None
        try:
            for event in response["stream"]:
                if "contentBlockDelta" in event:
                    text = event["contentBlockDelta"].get("delta", {}).get("text")
                    if text:
                        yield StreamChunk(text_delta=text)
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
                    input_tokens = usage.get("inputTokens")
                    output_tokens = usage.get("outputTokens")
        except Exception as exc:
            error_code = _extract_error_code(exc)
            raise BedrockInvocationError(
                f"Bedrock stream failed mid-stream: {exc}",
                code=error_code or "UPSTREAM_ERROR",
                retryable=False,
            ) from exc

        yield StreamChunk(
            is_final=True, stop_reason=stop_reason, input_tokens=input_tokens, output_tokens=output_tokens
        )


def _extract_error_code(exc: Exception) -> Optional[str]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None

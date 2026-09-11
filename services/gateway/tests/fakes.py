"""Fake ConverseClient for tests -- no boto3, no network, no AWS creds.

Satisfies the same interface BedrockClient does (see
inference/bedrock_client.py: ConverseClient Protocol), so it can be handed
to create_app(converse_client=...) as a drop-in replacement.
"""
from __future__ import annotations

from typing import Iterator, List, Optional

from ..inference.bedrock_client import BedrockChatMessage, BedrockInvocationError, ConverseResult, StreamChunk


class FakeConverseClient:
    def __init__(
        self,
        *,
        response_text: str = "Hello from a fake Bedrock model.",
        input_tokens: int = 10,
        output_tokens: int = 8,
        stop_reason: str = "end_turn",
        latency_ms: float = 42.0,
        retry_count: int = 0,
        error: Optional[BedrockInvocationError] = None,
        stream_chunks: Optional[List[str]] = None,
        stream_error: Optional[BedrockInvocationError] = None,
    ):
        self.response_text = response_text
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.stop_reason = stop_reason
        self.latency_ms = latency_ms
        self.retry_count = retry_count
        self.error = error
        self.stream_chunks = stream_chunks if stream_chunks is not None else ["Hello", ", ", "world", "!"]
        self.stream_error = stream_error
        self.calls: List[dict] = []
        self.stream_calls: List[dict] = []
        self.stream_was_cancelled = False

    def converse(
        self,
        *,
        model_id: str,
        messages: List[BedrockChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> ConverseResult:
        self.calls.append(
            {
                "model_id": model_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        if self.error is not None:
            raise self.error
        return ConverseResult(
            text=self.response_text,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            stop_reason=self.stop_reason,
            latency_ms=self.latency_ms,
            retry_count=self.retry_count,
        )

    def converse_stream(
        self,
        *,
        model_id: str,
        messages: List[BedrockChatMessage],
        max_tokens: int,
        temperature: float,
    ) -> Iterator[StreamChunk]:
        self.stream_calls.append(
            {
                "model_id": model_id,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        try:
            if self.stream_error is not None:
                raise self.stream_error
            for piece in self.stream_chunks:
                yield StreamChunk(text_delta=piece)
            yield StreamChunk(
                is_final=True,
                stop_reason=self.stop_reason,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
            )
        except GeneratorExit:
            # Raised into this generator by generator.close() -- the
            # cancellation signal on client disconnect (see streaming.py).
            self.stream_was_cancelled = True
            raise

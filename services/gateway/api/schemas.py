"""Request/response models for the gateway API (M0 subset).

POST /v1/chat only, for now -- matches the shape section 3 of the plan
describes, minus everything that needs a tenant/policy plane (model
allowlist, guardrail_policy, route_set, ...). Those fields get added in
M1/M2/M3 without breaking this contract.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    model: Optional[str] = Field(
        default=None,
        description="Bedrock model id. Falls back to BEDROCK_MODEL_ID if omitted.",
    )
    messages: List[ChatMessage] = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)
    stream: bool = Field(
        default=False,
        description=(
            "SSE streaming (M4, plan section 14). Streaming responses skip "
            "the output guardrail and certified-router fallback -- see "
            "docs/ROADMAP.md."
        ),
    )

    @field_validator("messages")
    @classmethod
    def _last_message_is_user(cls, messages: List[ChatMessage]) -> List[ChatMessage]:
        if messages[-1].role != "user":
            raise ValueError("the last message must have role='user'")
        return messages


class Usage(BaseModel):
    input_tokens: int
    output_tokens: int


class ChatResponse(BaseModel):
    request_id: str
    model: str
    output: str
    stop_reason: Optional[str]
    usage: Usage
    latency_ms: float
    cache_hit: bool = False
    fallback: bool = False


class ErrorBody(BaseModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(BaseModel):
    error: ErrorBody

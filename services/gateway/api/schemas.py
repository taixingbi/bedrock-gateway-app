"""Request/response models for the gateway API (M0 subset).

POST /v1/chat only, for now -- matches the shape section 3 of the plan
describes, minus everything that needs a tenant/policy plane (model
allowlist, guardrail_policy, route_set, ...). Those fields get added in
M1/M2/M3 without breaking this contract.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

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


class JobRequest(BaseModel):
    """POST /v1/jobs (M7, plan section 15). Same shape as ChatRequest
    minus `stream` -- a job is inherently non-interactive."""

    model: Optional[str] = Field(default=None)
    messages: List[ChatMessage] = Field(min_length=1)
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.7, ge=0.0, le=1.0)

    @field_validator("messages")
    @classmethod
    def _last_message_is_user(cls, messages: List[ChatMessage]) -> List[ChatMessage]:
        if messages[-1].role != "user":
            raise ValueError("the last message must have role='user'")
        return messages


class JobResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    model: str
    output: Optional[str] = None
    usage: Optional[Usage] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None


class SetTenantStateBody(BaseModel):
    # Left as a permissive str (not the TenantState enum) deliberately:
    # admin_routes.py validates it manually against TenantState itself,
    # to keep its existing custom "'state' must be one of [...]" error
    # message and code rather than FastAPI's own enum-validation shape.
    state: str


class OnboardingRequestBody(BaseModel):
    """POST /v1/admin/onboarding-requests (M11, plan section 22.1).
    Same fields the portal's "New Application" form collects."""

    tenant_id: str = Field(min_length=1)
    application_id: str = Field(min_length=1)
    environment: str = Field(default="dev")
    # Left as a permissive str, same reasoning as SetTenantStateBody.state
    # -- onboarding_routes.py validates it against AuthType manually.
    auth_type: str
    principal_arn: Optional[str] = Field(
        default=None, description="Required when auth_type='iam'."
    )
    requested_models: List[str] = Field(default_factory=list)
    rpm_limit: int = Field(default=60, ge=1)
    monthly_budget: Optional[float] = Field(default=None, gt=0)
    guardrail_policy: str = Field(default="standard-v1")
    data_classification: str = Field(default="internal")


class RejectOnboardingRequestBody(BaseModel):
    reason: str = Field(min_length=1)


class ProposePolicyChangeBody(BaseModel):
    """POST /v1/admin/tenants/{tenant_id}/policy-changes (plan section
    33). `changes` is a plain field->value map, not a full TenantPolicy
    -- see change_requests.py's PolicyChangeRequest docstring. Field-level
    validation happens in policy/validation.py, not here, since the set
    of legal fields/ranges belongs with TenantPolicy, not the transport
    layer."""

    changes: Dict[str, Any] = Field(min_length=1)
    base_policy_epoch: int = Field(ge=1)


class RejectPolicyChangeBody(BaseModel):
    reason: str = Field(min_length=1)


class RollbackPolicyBody(BaseModel):
    target_epoch: int = Field(ge=1)

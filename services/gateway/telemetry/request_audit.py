"""Unified, durable, metadata-only audit event (plan section 34.4).

Checked precisely against what already exists before building this:
`api/routes.py` already logs an enormous amount per request via
`log_event` (request_id, tenant_id, policy_epoch, model,
guardrail_policy, tokens, estimated_cost, cache_hit, fallback,
retry_count, slo_breach) -- to CloudWatch, which is encrypted at rest
and IAM-access-controlled, but not immutable (a log-group admin can
delete it) and not the "dedicated archive, long retention, restricted
access" the critique wants. `application_id`/`principal` were missing
from the structured fields despite being on `Identity` already; there
was no single `authz_decision`/`policy_version`/`decision_id` field
(plan section 34.3's `Decision` now supplies those).

`S3AuditStore` (telemetry/debug_capture.py) is the nearest existing
thing -- one encrypted S3 object per request -- but it's opt-in
(`debug_capture_enabled`) and carries raw redacted prompt/response
text, not a general-purpose audit trail. `RequestAuditEvent` here
carries METADATA ONLY (no prompt/response text at all), which is
exactly what makes it safe to write unconditionally, for every
request, not gated behind an opt-in flag.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Protocol


@dataclass(frozen=True)
class RequestAuditEvent:
    request_id: str
    tenant_id: str
    application_id: str
    principal: str  # Identity.sub -- who
    action: str  # e.g. "chat.completion", "job.submit"
    status: int  # HTTP status this request resolved to
    trace_id: Optional[str] = None
    model: Optional[str] = None
    policy_version: Optional[int] = None
    authz_decision: Optional[str] = None  # "ALLOW" | "DENY"
    decision_id: Optional[str] = None
    guardrail_version: Optional[str] = None
    guardrail_action: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    estimated_cost: Optional[float] = None
    timestamp: float = 0.0


class RequestAuditStore(Protocol):
    def write(self, event: RequestAuditEvent) -> None: ...


class InMemoryRequestAuditStore:
    """Test/dev fallback -- same "in-memory, not durable" role
    InMemoryPolicyStore/InMemoryUsageStore already play for their own
    real stores. Unlike those (bounded by tenant/job count),
    write() here is called once per HTTP request with no natural
    bound, so a long-running ECS task left without
    REQUEST_AUDIT_BUCKET_NAME configured would otherwise leak memory
    forever -- `maxlen` caps it to a bounded ring buffer (oldest
    dropped first), same tradeoff DebugCaptureStore's TTL makes for a
    different kind of unboundedness."""

    def __init__(self, *, maxlen: int = 10_000) -> None:
        self.events: "deque[RequestAuditEvent]" = deque(maxlen=maxlen)

    def write(self, event: RequestAuditEvent) -> None:
        self.events.append(event)


class S3RequestAuditStore:
    """Durable, metadata-only, always-on (not opt-in like
    S3AuditStore) -- one JSON object per request. boto3 imported
    lazily, same reasoning as every other real AWS-backed client here.

    Deliberately writes to a SEPARATE bucket from S3AuditStore's --
    the two have genuinely different retention/access requirements
    (this one has no raw content to protect, so can retain far longer
    and be queried more broadly than the opt-in payload store)."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "us-east-1",
        client: Optional[Any] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._bucket = bucket
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region)
        self._client = client
        self._clock = clock

    def write(self, event: RequestAuditEvent) -> None:
        now = self._clock()
        key = f"{event.tenant_id}/{now:%Y}/{now:%m}/{now:%d}/{event.request_id}.json"
        body: Dict[str, Any] = asdict(event)
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(body).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )


def current_trace_id() -> Optional[str]:
    """Same span-context lookup telemetry/logging.py's log_event
    already uses -- pulls trace_id from whatever OTel span is current,
    None if there isn't one (e.g. called outside a traced request)."""
    from opentelemetry import trace

    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        return format(span_context.trace_id, "032x")
    return None

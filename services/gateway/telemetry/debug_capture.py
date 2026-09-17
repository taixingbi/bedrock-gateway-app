"""Opt-in debug capture (M5, plan section 19).

Separate from operational telemetry: raw prompt/response content is
never written to the structured JSON logs (see telemetry/logging.py) --
by default nothing captures it at all. Only when a tenant explicitly sets
`debug_capture_enabled` on their policy does a redacted copy of the
input/output get written here, to a store kept deliberately separate from
operational telemetry so it can carry different (stricter) access and
retention rules.

Two stores exist, both gated by the same `debug_capture_enabled` flag,
serving different purposes:

- `DebugCaptureStore` -- in-memory, short TTL (default 15 min), gone on
  restart. Fast, zero-config, good for "what did this request just do"
  during active development; never durable, never a real audit trail.
- `S3AuditStore` -- durable, encrypted S3 object per request, the actual
  productionization of plan section 19's target pipeline (Raw Payload ->
  Redaction -> Encryption -> Restricted Debug Store). Only active when
  `AUDIT_BUCKET_NAME` is configured (see config.py) -- unset in
  tests/CI, so this stays fully optional infra, not a hard dependency.
  Retention is currently ONE uniform bucket-wide lifecycle rule set in
  Terraform, not per-tenant -- see TenantPolicy.debug_capture_retention_days'
  docstring for why per-tenant enforcement isn't implemented yet.
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

# Same patterns as guardrails/basic_guardrail.py -- kept independent
# rather than imported from there, since guardrails classifies (allow/
# block) while this only needs to redact for storage.
_REDACT_PATTERNS = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED_CARD]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


@dataclass(frozen=True)
class DebugRecord:
    request_id: str
    tenant_id: str
    redacted_input: str
    redacted_output: str
    captured_at: float


class DebugCaptureStore:
    def __init__(self, *, ttl_s: float = 900.0, clock: Callable[[], float] = time.monotonic):
        self._ttl_s = ttl_s
        self._clock = clock
        self._records: Dict[str, DebugRecord] = {}
        self._lock = threading.Lock()

    def capture(self, *, request_id: str, tenant_id: str, input_text: str, output_text: str) -> None:
        record = DebugRecord(
            request_id=request_id,
            tenant_id=tenant_id,
            redacted_input=redact(input_text),
            redacted_output=redact(output_text),
            captured_at=self._clock(),
        )
        with self._lock:
            self._records[request_id] = record

    def get(self, request_id: str) -> Optional[DebugRecord]:
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                return None
            if (self._clock() - record.captured_at) >= self._ttl_s:
                del self._records[request_id]
                return None
            return record


class S3AuditStore:
    """Durable counterpart to `DebugCaptureStore` -- see module
    docstring. boto3 imported lazily (only when actually constructed),
    same reasoning as jobs/store.py's `DynamoDbJobStore` -- this module
    stays importable without boto3 installed. `client`, when passed, is
    used instead of constructing a real one -- the seam tests use to
    inject a fake.

    Object key is `<tenant_id>/<YYYY>/<MM>/<DD>/<request_id>.json` --
    per-tenant prefixing is what would let a future per-tenant lifecycle
    rule (see TenantPolicy.debug_capture_retention_days) actually scope
    itself to one tenant's objects without touching anyone else's."""

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

    def write(
        self,
        *,
        request_id: str,
        tenant_id: str,
        application_id: str,
        model: str,
        input_text: str,
        output_text: str,
        input_tokens: int,
        output_tokens: int,
    ) -> str:
        """Writes one redacted request/response object to S3, returns
        its `s3://` URI (the `payload_ref` logged alongside the
        operational -- payload-free -- chat log line)."""
        now = self._clock()
        key = f"{tenant_id}/{now:%Y}/{now:%m}/{now:%d}/{request_id}.json"
        body: Dict[str, Any] = {
            "request_id": request_id,
            "tenant_id": tenant_id,
            "application_id": application_id,
            "timestamp": now.isoformat(),
            "request": {"model": model, "input_text": redact(input_text)},
            "response": {
                "output_text": redact(output_text),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
        }
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=json.dumps(body).encode("utf-8"),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )
        return f"s3://{self._bucket}/{key}"

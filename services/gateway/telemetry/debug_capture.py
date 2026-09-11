"""Opt-in debug capture (M5, plan section 19).

Separate from operational telemetry: raw prompt/response content is
never written to the structured JSON logs (see telemetry/logging.py) --
by default nothing captures it at all. Only when a tenant explicitly sets
`debug_capture_enabled` on their policy does a redacted copy of the
input/output get written here, to a store kept deliberately separate from
operational telemetry so it can carry different (stricter) access and
retention rules.

A real deployment would back this with its own IAM permissions,
encryption, and short retention TTL, per plan section 19 -- there's no
real AWS infra in this MVP to attach those to, so `DebugCaptureStore`
just demonstrates the operational/debug *separation* (a different class,
a different set of callers) rather than being production-grade storage
itself.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

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

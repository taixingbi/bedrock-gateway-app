"""Fault-injecting fakes for M6 load/chaos scenarios (plan section 17).

Distinct from the small, single-behavior fakes in
services/gateway/tests/fakes.py: load scenarios need to inject faults
across many *concurrent* requests (a percentage that throttle, guardrail
timeouts at a configurable rate), with thread-safe counters so scenario
assertions can check exactly how many real calls were attempted -- that
count is the actual evidence for "no retry storm" and "fail-closed under
load", not just individual response codes.

Never talks to real Bedrock. See docs/LOAD_TESTING.md for how to re-point
the locustfiles in this directory tree at a real BedrockClient when you
want to load-test for real (that run is on you -- it costs money and
needs your AWS credentials, so it isn't executed as part of this repo's
automation).
"""
from __future__ import annotations

import random
import threading
from typing import List

from services.gateway.guardrails.client import GuardrailCheckError
from services.gateway.guardrails.models import GuardrailAction, GuardrailDecision
from services.gateway.inference.bedrock_client import BedrockInvocationError, ConverseResult


class ThrottlingFaultConverseClient:
    """Simulates Bedrock throttling a configurable fraction of calls.
    One call in, one outcome out -- no internal retry (that's
    BedrockClient's job, already covered in M0/M4's own tests); this
    fake exists to prove what the *gateway* does when the underlying
    model keeps failing (circuit breaker, fallback), not to re-test
    per-call retry."""

    def __init__(self, *, throttle_rate: float = 0.0, response_text: str = "ok"):
        self.throttle_rate = throttle_rate
        self.response_text = response_text
        self._lock = threading.Lock()
        self.total_calls = 0
        self.throttled_calls = 0
        self.calls_by_model: dict = {}

    def converse(self, *, model_id, messages, max_tokens, temperature) -> ConverseResult:
        with self._lock:
            self.total_calls += 1
            self.calls_by_model[model_id] = self.calls_by_model.get(model_id, 0) + 1
            throttle = random.random() < self.throttle_rate
            if throttle:
                self.throttled_calls += 1
        if throttle:
            raise BedrockInvocationError("throttled", code="ThrottlingException", retryable=True)
        return ConverseResult(
            text=self.response_text, input_tokens=5, output_tokens=5,
            stop_reason="end_turn", latency_ms=1.0, retry_count=0,
        )


class AlwaysAllowGuardrailClient:
    """A guardrail that never blocks and never fails -- the "control"
    guardrail for scenarios that aren't about safety, so guardrail checks
    don't confound the thing actually being measured."""

    def check_input(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        return GuardrailDecision(action=GuardrailAction.ALLOW)

    def check_output(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        return GuardrailDecision(action=GuardrailAction.ALLOW)


class FlakyGuardrailClient:
    """Raises GuardrailCheckError (simulating an unavailable guardrail
    backend) for a configurable fraction of input checks. Output checks
    always allow, since these scenarios are about the input side failing
    closed under load."""

    def __init__(self, *, failure_rate: float = 1.0):
        self.failure_rate = failure_rate
        self._lock = threading.Lock()
        self.input_checks: int = 0
        self.failures: int = 0

    def check_input(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        with self._lock:
            self.input_checks += 1
            fail = random.random() < self.failure_rate
            if fail:
                self.failures += 1
        if fail:
            raise GuardrailCheckError("simulated guardrail backend unavailable")
        return GuardrailDecision(action=GuardrailAction.ALLOW)

    def check_output(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        return GuardrailDecision(action=GuardrailAction.ALLOW)

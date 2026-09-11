"""Fake GuardrailClient for tests -- no regex, no real check, fully
controllable, including simulating an unavailable guardrail backend."""
from __future__ import annotations

from typing import List, Optional

from ..guardrails.client import GuardrailCheckError
from ..guardrails.models import GuardrailAction, GuardrailDecision


class FakeGuardrailClient:
    def __init__(
        self,
        *,
        input_decision: Optional[GuardrailDecision] = None,
        output_decision: Optional[GuardrailDecision] = None,
        raise_on_input: bool = False,
        raise_on_output: bool = False,
    ):
        self.input_decision = input_decision or GuardrailDecision(action=GuardrailAction.ALLOW)
        self.output_decision = output_decision or GuardrailDecision(action=GuardrailAction.ALLOW)
        self.raise_on_input = raise_on_input
        self.raise_on_output = raise_on_output
        self.input_calls: List[str] = []
        self.output_calls: List[str] = []

    def check_input(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        self.input_calls.append(text)
        if self.raise_on_input:
            raise GuardrailCheckError("simulated guardrail input-check timeout")
        return self.input_decision

    def check_output(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        self.output_calls.append(text)
        if self.raise_on_output:
            raise GuardrailCheckError("simulated guardrail output-check timeout")
        return self.output_decision

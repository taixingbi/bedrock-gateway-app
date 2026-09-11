"""GuardrailClient seam (M3) -- same pattern as ConverseClient (M0),
TokenVerifier (M1), and PolicyStore (M2): a Protocol real callers and
fakes both implement, so a real backend (Bedrock Guardrails, Comprehend,
a third-party moderation API) can replace `BasicGuardrailClient` later
without touching the pipeline.

Implementations raise `GuardrailCheckError` when the check itself could
not be completed (timeout, upstream 5xx, service unavailable) -- this is
distinct from a BLOCK decision, which is a *completed* check that says
no. `guardrails/fail_closed.py` is what turns a `GuardrailCheckError`
into the fail-closed HTTP response; this module only defines the
contract.
"""
from __future__ import annotations

from typing import Protocol

from .models import GuardrailDecision


class GuardrailCheckError(Exception):
    """The guardrail check could not be completed (timeout/unavailable)."""


class GuardrailClient(Protocol):
    def check_input(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        """Raises GuardrailCheckError if the check could not complete."""
        ...

    def check_output(self, text: str, *, guardrail_policy: str) -> GuardrailDecision:
        """Raises GuardrailCheckError if the check could not complete."""
        ...

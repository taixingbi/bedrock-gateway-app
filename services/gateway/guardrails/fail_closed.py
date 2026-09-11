"""Fail-closed guardrail enforcement (M3, plan section 11).

Core invariant: no request requiring a strong guardrail may reach the LLM
unless the required guardrail decision has completed successfully.

    Guardrail
       |
       +-- ALLOW -> model
       +-- BLOCK -> reject
       +-- timeout/429/500/unavailable (GuardrailCheckError)
              |
              STRICT / STANDARD -> fail closed (GuardrailUnavailableError)
              LOW_RISK + tenant opt-in -> degrade to ALLOW
              LOW_RISK, no opt-in      -> fail closed (same as STRICT/STANDARD)

There is no silent downgrade from a strong guardrail to a weak one: the
LOW_RISK bypass only ever applies when the tenant's policy explicitly set
`allow_guardrail_bypass_on_error`.
"""
from __future__ import annotations

from typing import Callable

from .client import GuardrailCheckError
from .models import GuardrailAction, GuardrailDecision, SafetyClass


class GuardrailUnavailableError(Exception):
    pass


def classify_safety(guardrail_policy: str) -> SafetyClass:
    """Maps a tenant's guardrail_policy id to a safety class via a naming
    convention (…-strict / …-lowrisk / default standard). A real control
    plane would carry safety_class as an explicit field on the guardrail
    policy record (plan section 10) rather than inferring it from the id;
    this is the simplest thing that lets the fail-closed contract above
    actually branch on something today."""
    lowered = guardrail_policy.lower()
    if "strict" in lowered:
        return SafetyClass.STRICT
    if "low" in lowered:
        return SafetyClass.LOW_RISK
    return SafetyClass.STANDARD


def run_guardrail_check(
    check: Callable[[], GuardrailDecision],
    *,
    guardrail_policy: str,
    allow_bypass_on_error: bool,
) -> GuardrailDecision:
    """Runs `check` (already bound to input or output text) and applies
    the fail-closed contract if it raises GuardrailCheckError."""
    safety_class = classify_safety(guardrail_policy)

    try:
        return check()
    except GuardrailCheckError as exc:
        if safety_class == SafetyClass.LOW_RISK and allow_bypass_on_error:
            return GuardrailDecision(
                action=GuardrailAction.ALLOW, reason=f"guardrail bypassed on error: {exc}"
            )
        raise GuardrailUnavailableError(
            f"guardrail check unavailable ({safety_class.value}): {exc}"
        ) from exc

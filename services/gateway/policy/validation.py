"""Validates a policy change request's proposed field values (plan
section 33) -- runs before a change is accepted as PENDING_APPROVAL,
so a proposer finds out immediately, not at approval time.

Honesty about scope (plan section 33.2's point 4): this hand-keeps the
same per-field constraints bedrock-gateway-policies/schemas/
tenants.schema.json already defines for the YAML path (rpm_limit >= 1,
monthly_budget > 0, ...), rather than loading and validating against
that schema file directly -- this repo has no dependency on it and no
`jsonschema` library today. That means the two *can* drift apart if
one is edited without the other; a real shared-schema mechanism
(vendoring the schema JSON itself, or a shared package) is a genuine
follow-up, not silently pretended to be solved here.
"""
from __future__ import annotations

from typing import Any, Dict

# tenant_id/policy_epoch/state-transition-via-approval aren't editable
# through a plain field change -- tenant_id is identity, policy_epoch is
# system-managed, and `state` already has its own dedicated, audited
# path (set_state / the kill switch, plan section 7) that a generic
# field edit shouldn't duplicate or bypass.
_IMMUTABLE_FIELDS = frozenset({"tenant_id", "policy_epoch", "state"})

_KNOWN_FIELDS = frozenset({
    "models", "rpm_limit", "guardrail_policy", "route_set", "slo",
    "allow_guardrail_bypass_on_error", "debug_capture_enabled",
    "debug_capture_retention_days", "max_concurrency", "monthly_budget",
})


class PolicyValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)


def validate_policy_changes(changes: Dict[str, Any]) -> None:
    """Raises PolicyValidationError on the first problem found."""
    if not changes:
        raise PolicyValidationError("a policy change must include at least one field")

    for field_name in changes:
        if field_name in _IMMUTABLE_FIELDS:
            raise PolicyValidationError(f"'{field_name}' cannot be changed via a policy change request")
        if field_name not in _KNOWN_FIELDS:
            raise PolicyValidationError(f"unknown policy field '{field_name}'")

    if "models" in changes:
        models = changes["models"]
        if not isinstance(models, list) or not all(isinstance(m, str) and m for m in models):
            raise PolicyValidationError("'models' must be a list of non-empty strings")

    if "rpm_limit" in changes:
        value = changes["rpm_limit"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise PolicyValidationError("'rpm_limit' must be an integer >= 1")

    if "guardrail_policy" in changes:
        value = changes["guardrail_policy"]
        if not isinstance(value, str) or not value:
            raise PolicyValidationError("'guardrail_policy' must be a non-empty string")

    if "route_set" in changes:
        value = changes["route_set"]
        if not isinstance(value, str) or not value:
            raise PolicyValidationError("'route_set' must be a non-empty string")

    if "allow_guardrail_bypass_on_error" in changes:
        if not isinstance(changes["allow_guardrail_bypass_on_error"], bool):
            raise PolicyValidationError("'allow_guardrail_bypass_on_error' must be a boolean")

    if "debug_capture_enabled" in changes:
        if not isinstance(changes["debug_capture_enabled"], bool):
            raise PolicyValidationError("'debug_capture_enabled' must be a boolean")

    if "debug_capture_retention_days" in changes:
        value = changes["debug_capture_retention_days"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise PolicyValidationError("'debug_capture_retention_days' must be an integer >= 1")

    if "max_concurrency" in changes:
        value = changes["max_concurrency"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise PolicyValidationError("'max_concurrency' must be an integer >= 1")

    if "monthly_budget" in changes:
        value = changes["monthly_budget"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise PolicyValidationError("'monthly_budget' must be a positive number")

    if "slo" in changes:
        slo = changes["slo"]
        if not isinstance(slo, dict) or not set(slo.keys()) <= {"p95_latency_ms"}:
            raise PolicyValidationError("'slo' must be an object with only a 'p95_latency_ms' key")
        if "p95_latency_ms" in slo:
            p95 = slo["p95_latency_ms"]
            if isinstance(p95, bool) or not isinstance(p95, (int, float)) or p95 <= 0:
                raise PolicyValidationError("'slo.p95_latency_ms' must be a positive number")

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
    "data_classification", "daily_budget", "application_budgets",
    "monthly_budget_soft_threshold_pct", "priority_class",
})

_KNOWN_PRIORITY_CLASSES = frozenset({"critical", "standard", "best_effort"})

# Plan section 34.4b -- mirrors routing/model_registry.py's
# _CLASSIFICATION_RANK; kept as a separate literal here rather than
# imported, since validation.py's own docstring already documents this
# file as deliberately hand-kept in sync rather than importing across
# the store/routing boundary for one small constant.
_KNOWN_DATA_CLASSIFICATIONS = frozenset({"public", "internal", "confidential", "phi", "pii"})


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

    if "data_classification" in changes:
        value = changes["data_classification"]
        if not isinstance(value, str) or value.strip().lower() not in _KNOWN_DATA_CLASSIFICATIONS:
            raise PolicyValidationError(
                f"'data_classification' must be one of {sorted(_KNOWN_DATA_CLASSIFICATIONS)}"
            )

    if "daily_budget" in changes:
        value = changes["daily_budget"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise PolicyValidationError("'daily_budget' must be a positive number")

    if "application_budgets" in changes:
        value = changes["application_budgets"]
        if not isinstance(value, dict) or not value:
            raise PolicyValidationError("'application_budgets' must be a non-empty object")
        for app_id, budget in value.items():
            if not isinstance(app_id, str) or not app_id:
                raise PolicyValidationError("'application_budgets' keys must be non-empty strings")
            if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0:
                raise PolicyValidationError(
                    f"'application_budgets[{app_id!r}]' must be a positive number"
                )

    if "monthly_budget_soft_threshold_pct" in changes:
        value = changes["monthly_budget_soft_threshold_pct"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0 < value <= 1):
            raise PolicyValidationError(
                "'monthly_budget_soft_threshold_pct' must be a number in (0, 1]"
            )

    if "priority_class" in changes:
        value = changes["priority_class"]
        if not isinstance(value, str) or value not in _KNOWN_PRIORITY_CLASSES:
            raise PolicyValidationError(f"'priority_class' must be one of {sorted(_KNOWN_PRIORITY_CLASSES)}")

    if "slo" in changes:
        slo = changes["slo"]
        if not isinstance(slo, dict) or not set(slo.keys()) <= {"p95_latency_ms"}:
            raise PolicyValidationError("'slo' must be an object with only a 'p95_latency_ms' key")
        if "p95_latency_ms" in slo:
            p95 = slo["p95_latency_ms"]
            if isinstance(p95, bool) or not isinstance(p95, (int, float)) or p95 <= 0:
                raise PolicyValidationError("'slo.p95_latency_ms' must be a positive number")

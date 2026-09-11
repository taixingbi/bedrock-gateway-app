"""Per-model cost estimation (M5, plan sections 18/20).

A static $/1K-token table, not live AWS billing data -- Bedrock pricing
varies by model/region and changes over time. This is enough to populate
`estimated_cost` in telemetry and prove the FinOps seam exists; syncing
real pricing and aggregating spend per tenant/application is M8 (FinOps)
scope, not this one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class ModelPricing:
    input_per_1k: float
    output_per_1k: float


DEFAULT_PRICING: Dict[str, ModelPricing] = {
    "anthropic.claude-3-5-sonnet-20241022-v2:0": ModelPricing(input_per_1k=0.003, output_per_1k=0.015),
    "anthropic.claude-3-haiku-20240307-v1:0": ModelPricing(input_per_1k=0.00025, output_per_1k=0.00125),
}

# Used for any model_id not in the table above, so estimated_cost is
# never silently zero for an unrecognized/new model id.
_FALLBACK_PRICING = ModelPricing(input_per_1k=0.003, output_per_1k=0.015)


def estimate_cost(
    model_id: str,
    *,
    input_tokens: int,
    output_tokens: int,
    pricing: Optional[Dict[str, ModelPricing]] = None,
) -> float:
    table = pricing if pricing is not None else DEFAULT_PRICING
    rates = table.get(model_id, _FALLBACK_PRICING)
    cost = (input_tokens / 1000.0) * rates.input_per_1k + (output_tokens / 1000.0) * rates.output_per_1k
    return round(cost, 6)

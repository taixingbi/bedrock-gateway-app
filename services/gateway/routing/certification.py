"""Model certification registry (M9, plan section 21).

Routing Invariant (plan section 1): production traffic may only reach
models that have passed the required evaluation/certification gates --
"including fallback traffic" (plan section 13). Before this module
existed, route_sets.yaml's own docstring admitted that wasn't actually
enforced anywhere: "'Certified' here just means 'listed in this
file'". This closes that gap -- CertifiedRouter (routing/router.py)
filters every candidate, primary and fallback alike, against this
registry, and pipeline.enforce_model_certification rejects an
uncertified primary before the router is ever reached.

evals/run_eval.py is what actually produces
policies/certified_models.yaml's entries, by running a golden dataset
against a model and checking it clears plan section 21's release gate
(quality/safety/latency/cost thresholds) -- this module only loads and
exposes the result of that process, it doesn't run evaluations itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set


@dataclass(frozen=True)
class ModelCertification:
    model_id: str
    quality_score: float
    safety_score: float
    p95_latency_ms: float
    cost_per_request: float
    certified_at: str  # ISO date; informational only, not re-checked at runtime


def load_certified_models_from_yaml(path: str) -> Dict[str, ModelCertification]:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    certified: Dict[str, ModelCertification] = {}
    for model_id, cfg in (raw.get("certified_models") or {}).items():
        cfg = cfg or {}
        certified[model_id] = ModelCertification(
            model_id=model_id,
            quality_score=float(cfg["quality_score"]),
            safety_score=float(cfg["safety_score"]),
            p95_latency_ms=float(cfg["p95_latency_ms"]),
            cost_per_request=float(cfg["cost_per_request"]),
            certified_at=str(cfg.get("certified_at", "")),
        )
    return certified


def certified_model_ids(certified: Dict[str, ModelCertification]) -> Set[str]:
    return set(certified.keys())

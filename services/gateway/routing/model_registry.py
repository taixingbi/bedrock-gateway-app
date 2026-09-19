"""Model governance registry (plan section 34.5).

`routing/certification.py` (M9) already implements the critique's
"platform governance controls whether a model enters production, not
gateway-side ML evaluation" -- evals/run_eval.py runs the release gate,
`certified_models.yaml` is its output, the gateway only reads it. What
that file *doesn't* carry is governance richness: owner, risk
classification, approved use cases, region, max data classification,
retirement date, or a lifecycle richer than "present or absent."

This is a deliberately SEPARATE overlay, keyed by the same model_id as
`certified_models.yaml` -- eval results and governance curation are
different concerns maintained by different people/processes; merging
them into one file would mean a governance-only edit (retiring a
model) has to touch a file that's otherwise machine-written by
evals/run_eval.py.

A model absent from the registry is treated as APPROVED (permissive
default) -- so introducing this file doesn't silently break every
pre-existing certified model that hasn't been curated into it yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class ModelStatus(str, Enum):
    APPROVED = "APPROVED"
    CONDITIONAL = "CONDITIONAL"
    DEPRECATED = "DEPRECATED"
    BLOCKED = "BLOCKED"


# Ordinal ranking for TenantPolicy.data_classification vs. a model's
# max_data_classification (plan section 34.4b) -- deliberately a small,
# fixed, case-insensitive table rather than accepting arbitrary
# strings: an unrecognized value on either side means the comparison
# can't be made, and enforce_model_certification treats that as "don't
# block" (documented there) rather than guessing an ordering.
_CLASSIFICATION_RANK = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "phi": 3,
    "pii": 3,
}


def classification_rank(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    return _CLASSIFICATION_RANK.get(value.strip().lower())


@dataclass(frozen=True)
class ModelRegistryEntry:
    model_id: str
    status: ModelStatus
    owner: str
    risk_classification: str
    approved_use_cases: List[str]
    region: Optional[str] = None
    max_data_classification: Optional[str] = None
    model_version: Optional[str] = None
    retirement_date: Optional[str] = None
    notes: Optional[str] = None


def load_model_registry_from_yaml(path: str) -> Dict[str, ModelRegistryEntry]:
    """Tolerant of a missing/empty path -- same "not every environment
    has this configured" reasoning as FileIamTenantResolver/
    FileEnterpriseGroupResolver -- returns an empty registry (every
    model treated as APPROVED) rather than failing app startup."""
    import os

    import yaml

    if not path or not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    registry: Dict[str, ModelRegistryEntry] = {}
    for model_id, cfg in (raw.get("model_registry") or {}).items():
        cfg = cfg or {}
        registry[model_id] = ModelRegistryEntry(
            model_id=model_id,
            status=ModelStatus(cfg["status"]),
            owner=cfg["owner"],
            risk_classification=cfg["risk_classification"],
            approved_use_cases=list(cfg.get("approved_use_cases", [])),
            region=cfg.get("region"),
            max_data_classification=cfg.get("max_data_classification"),
            model_version=cfg.get("model_version"),
            retirement_date=cfg.get("retirement_date"),
            notes=cfg.get("notes"),
        )
    return registry


def get_status(model_id: str, *, registry: Dict[str, ModelRegistryEntry]) -> ModelStatus:
    """A model absent from the registry is APPROVED -- see module
    docstring for why that's the permissive, not restrictive, default."""
    entry = registry.get(model_id)
    return entry.status if entry is not None else ModelStatus.APPROVED

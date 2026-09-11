"""Tenant policy storage (M2, plan section 8).

`PolicyStore` is the seam -- same pattern as `ConverseClient` (M0) and
`TokenVerifier` (M1). `FilePolicyStore` reads a local YAML file once and
keeps policies in memory, standing in for the DynamoDB table plan section
8 describes; swapping in a real DynamoDB-backed store later is a new
class behind this same Protocol, not a rewrite of callers.

`set_state()` (used by the admin endpoint) mutates the in-memory policy
and bumps `policy_epoch` -- a state change is itself a policy change, so
any response cached under the old epoch must not survive it (M4, plan
section 12). A real control plane would instead write to DynamoDB and let
the change propagate via the event in section 8; here, the in-process
mutation plus `PolicySnapshotCache.invalidate()` (cache.py) demonstrates
the same "push + bounded TTL" contract without needing SNS/SQS.
"""
from __future__ import annotations

import dataclasses
from typing import Dict, Protocol

from .models import TenantPolicy, TenantSlo, TenantState, UnknownTenantError


class PolicyStore(Protocol):
    def get(self, tenant_id: str) -> TenantPolicy:
        """Raises UnknownTenantError if tenant_id has no policy."""
        ...


class MutablePolicyStore(PolicyStore, Protocol):
    """A PolicyStore that also supports the admin state-change action.
    A read replica or a future DynamoDB-backed store that only mirrors
    control-plane writes need not implement this half."""

    def set_state(self, tenant_id: str, state: TenantState) -> TenantPolicy: ...


class InMemoryPolicyStore:
    """Backing store keyed by tenant_id. `FilePolicyStore` loads into one
    of these; tests can construct one directly with explicit policies."""

    def __init__(self, policies: Dict[str, TenantPolicy]):
        self._policies = dict(policies)

    def get(self, tenant_id: str) -> TenantPolicy:
        try:
            return self._policies[tenant_id]
        except KeyError:
            raise UnknownTenantError(tenant_id) from None

    def set_state(self, tenant_id: str, state: TenantState) -> TenantPolicy:
        current = self.get(tenant_id)
        updated = dataclasses.replace(current, state=state, policy_epoch=current.policy_epoch + 1)
        self._policies[tenant_id] = updated
        return updated


def load_policies_from_yaml(path: str) -> InMemoryPolicyStore:
    import yaml

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    policies: Dict[str, TenantPolicy] = {}
    for tenant_id, cfg in (raw.get("tenants") or {}).items():
        cfg = cfg or {}
        slo_cfg = cfg.get("slo") or {}
        policies[tenant_id] = TenantPolicy(
            tenant_id=tenant_id,
            state=TenantState(cfg.get("state", "ACTIVE")),
            models=list(cfg.get("models", [])),
            rpm_limit=int(cfg.get("rpm_limit", 60)),
            guardrail_policy=cfg.get("guardrail_policy", "standard-v1"),
            route_set=cfg.get("route_set"),
            slo=TenantSlo(p95_latency_ms=slo_cfg.get("p95_latency_ms")),
            policy_epoch=int(cfg.get("policy_epoch", 1)),
            allow_guardrail_bypass_on_error=bool(cfg.get("allow_guardrail_bypass_on_error", False)),
            debug_capture_enabled=bool(cfg.get("debug_capture_enabled", False)),
        )
    return InMemoryPolicyStore(policies)


class FilePolicyStore:
    """Callers depend on 'a PolicyStore'; this one happens to be backed by
    a YAML file read once at startup."""

    def __init__(self, path: str):
        self._backing = load_policies_from_yaml(path)

    def get(self, tenant_id: str) -> TenantPolicy:
        return self._backing.get(tenant_id)

    def set_state(self, tenant_id: str, state: TenantState) -> TenantPolicy:
        return self._backing.set_state(tenant_id, state)

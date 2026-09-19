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
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol

from .models import TenantAlreadyExistsError, TenantPolicy, TenantSlo, TenantState, UnknownTenantError


class PolicyStore(Protocol):
    def get(self, tenant_id: str) -> TenantPolicy:
        """Raises UnknownTenantError if tenant_id has no policy."""
        ...

    def list_tenant_ids(self) -> List[str]:
        """M8: every known tenant_id, for the admin usage/showback report
        (api/admin_routes.py) to enumerate. Order is not guaranteed."""
        ...


class MutablePolicyStore(PolicyStore, Protocol):
    """A PolicyStore that also supports the admin state-change action.
    A read replica or a future DynamoDB-backed store that only mirrors
    control-plane writes need not implement this half."""

    def set_state(self, tenant_id: str, state: TenantState) -> TenantPolicy: ...


class ProvisionedPolicyStore(MutablePolicyStore, Protocol):
    """M11: a MutablePolicyStore that also supports onboarding's
    create-only write -- `InMemoryPolicyStore` and `DynamoDbPolicyStore`
    both satisfy this; `FilePolicyStore` deliberately doesn't (existing
    hand-managed tenants are never created through this path)."""

    def create(self, policy: TenantPolicy) -> None:
        """Raises TenantAlreadyExistsError if tenant_id already has a
        row here. The authoritative conflict check (a conditional
        write), not a caller's own exists()-then-create()."""
        ...


class InMemoryPolicyStore:
    """Backing store keyed by tenant_id. `FilePolicyStore` loads into one
    of these; tests can construct one directly with explicit policies.
    Also usable directly as a `ProvisionedPolicyStore` (M11) when no
    DynamoDB table is configured -- same "in-memory fallback" pattern
    jobs/store.py and usage/store.py already use."""

    def __init__(self, policies: Dict[str, TenantPolicy]):
        self._policies = dict(policies)

    def create(self, policy: TenantPolicy) -> None:
        if policy.tenant_id in self._policies:
            raise TenantAlreadyExistsError(policy.tenant_id)
        self._policies[policy.tenant_id] = policy

    def get(self, tenant_id: str) -> TenantPolicy:
        try:
            return self._policies[tenant_id]
        except KeyError:
            raise UnknownTenantError(tenant_id) from None

    def list_tenant_ids(self) -> List[str]:
        return list(self._policies.keys())

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
            debug_capture_retention_days=(
                int(cfg["debug_capture_retention_days"])
                if cfg.get("debug_capture_retention_days") is not None
                else None
            ),
            max_concurrency=(
                int(cfg["max_concurrency"]) if cfg.get("max_concurrency") is not None else None
            ),
            monthly_budget=(
                float(cfg["monthly_budget"]) if cfg.get("monthly_budget") is not None else None
            ),
        )
    return InMemoryPolicyStore(policies)


class FilePolicyStore:
    """Callers depend on 'a PolicyStore'; this one happens to be backed by
    a YAML file read once at startup."""

    def __init__(self, path: str):
        self._backing = load_policies_from_yaml(path)

    def get(self, tenant_id: str) -> TenantPolicy:
        return self._backing.get(tenant_id)

    def list_tenant_ids(self) -> List[str]:
        return self._backing.list_tenant_ids()

    def set_state(self, tenant_id: str, state: TenantState) -> TenantPolicy:
        return self._backing.set_state(tenant_id, state)


def _policy_to_item(policy: TenantPolicy) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "tenant_id": policy.tenant_id,
        "state": policy.state.value,
        "models": list(policy.models),
        "rpm_limit": policy.rpm_limit,
        "guardrail_policy": policy.guardrail_policy,
        "policy_epoch": policy.policy_epoch,
        "allow_guardrail_bypass_on_error": policy.allow_guardrail_bypass_on_error,
        "debug_capture_enabled": policy.debug_capture_enabled,
    }
    if policy.route_set is not None:
        item["route_set"] = policy.route_set
    if policy.slo.p95_latency_ms is not None:
        item["slo_p95_latency_ms"] = Decimal(str(policy.slo.p95_latency_ms))
    if policy.monthly_budget is not None:
        item["monthly_budget"] = Decimal(str(policy.monthly_budget))
    if policy.debug_capture_retention_days is not None:
        item["debug_capture_retention_days"] = policy.debug_capture_retention_days
    if policy.max_concurrency is not None:
        item["max_concurrency"] = policy.max_concurrency
    return item


def _item_to_policy(item: Dict[str, Any]) -> TenantPolicy:
    return TenantPolicy(
        tenant_id=item["tenant_id"],
        state=TenantState(item["state"]),
        models=list(item.get("models", [])),
        rpm_limit=int(item["rpm_limit"]),
        guardrail_policy=item["guardrail_policy"],
        route_set=item.get("route_set"),
        slo=TenantSlo(
            p95_latency_ms=float(item["slo_p95_latency_ms"]) if "slo_p95_latency_ms" in item else None
        ),
        policy_epoch=int(item["policy_epoch"]),
        allow_guardrail_bypass_on_error=bool(item.get("allow_guardrail_bypass_on_error", False)),
        debug_capture_enabled=bool(item.get("debug_capture_enabled", False)),
        debug_capture_retention_days=(
            int(item["debug_capture_retention_days"]) if "debug_capture_retention_days" in item else None
        ),
        max_concurrency=int(item["max_concurrency"]) if "max_concurrency" in item else None,
        monthly_budget=float(item["monthly_budget"]) if "monthly_budget" in item else None,
    )


class DynamoDbPolicyStore:
    """Real, durable tenant-policy storage (M11, plan section 22.3) --
    what `FilePolicyStore`'s own docstring calls "a real control plane
    would instead write to DynamoDB". Used as a *layer*
    (`LayeredPolicyStore` below), not a replacement for
    `FilePolicyStore`: existing hand-managed tenants stay file/git/PR
    -reviewed exactly as before; only tenants *provisioned* through
    onboarding (services/gateway/onboarding/provisioning.py) live here.

    Unlike `InMemoryPolicyStore.set_state`, this survives an ECS task
    restart and is visible to every task, not just the one that
    handled the admin call -- the real "push + durable" contract plan
    section 8 describes, not the single-process approximation
    `FilePolicyStore` has always been standing in for.
    """

    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def create(self, policy: TenantPolicy) -> None:
        """Provisioning-only: fails if tenant_id already has a row here,
        so onboarding can never silently clobber an existing provisioned
        tenant's policy. Does not (and cannot) protect against colliding
        with a *file*-configured tenant_id -- callers must check
        `LayeredPolicyStore.exists()` first."""
        from botocore.exceptions import ClientError

        try:
            self._table.put_item(
                Item=_policy_to_item(policy),
                ConditionExpression="attribute_not_exists(tenant_id)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                raise TenantAlreadyExistsError(policy.tenant_id) from exc
            raise

    def get(self, tenant_id: str) -> TenantPolicy:
        response = self._table.get_item(Key={"tenant_id": tenant_id})
        item = response.get("Item")
        if item is None:
            raise UnknownTenantError(tenant_id)
        return _item_to_policy(item)

    def list_tenant_ids(self) -> List[str]:
        ids: List[str] = []
        kwargs: Dict[str, Any] = {"ProjectionExpression": "tenant_id"}
        while True:
            response = self._table.scan(**kwargs)
            ids.extend(item["tenant_id"] for item in response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        return ids

    def set_state(self, tenant_id: str, state: TenantState) -> TenantPolicy:
        current = self.get(tenant_id)
        updated = dataclasses.replace(current, state=state, policy_epoch=current.policy_epoch + 1)
        self._table.put_item(Item=_policy_to_item(updated))
        return updated


class LayeredPolicyStore:
    """`primary` (provisioned/DynamoDB) is checked first, `fallback`
    (hand-configured/file) second -- additive, so nothing about an
    existing file-managed tenant changes. A tenant_id can only ever
    live in one layer at a time (provisioning refuses to create one
    that already exists in either -- see onboarding/provisioning.py),
    so there's no merge-conflict case to resolve here."""

    def __init__(self, *, primary: ProvisionedPolicyStore, fallback: PolicyStore):
        self._primary = primary
        self._fallback = fallback

    def exists(self, tenant_id: str) -> bool:
        try:
            self.get(tenant_id)
            return True
        except UnknownTenantError:
            return False

    def get(self, tenant_id: str) -> TenantPolicy:
        try:
            return self._primary.get(tenant_id)
        except UnknownTenantError:
            return self._fallback.get(tenant_id)

    def list_tenant_ids(self) -> List[str]:
        return list({*self._primary.list_tenant_ids(), *self._fallback.list_tenant_ids()})

    def set_state(self, tenant_id: str, state: TenantState) -> TenantPolicy:
        try:
            self._primary.get(tenant_id)
            return self._primary.set_state(tenant_id, state)
        except UnknownTenantError:
            pass
        # MutablePolicyStore is the narrower protocol `fallback` (a
        # plain FilePolicyStore) already satisfies -- see policy_store.py.
        return self._fallback.set_state(tenant_id, state)  # type: ignore[union-attr]

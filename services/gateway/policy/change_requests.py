"""Policy change request domain model + storage (plan section 33).

Closes the gap `onboarding/provisioning.py` names in its own docstring:
"Widening an existing application's quota/models is a change request
against that resource, not a new onboarding request; this module
doesn't implement that yet." This is that change request -- same
submit-then-approve shape as `OnboardingRequest`, reused rather than
reinvented, but scoped to *editing* an already-provisioned tenant's
policy fields (rpm_limit, models, monthly_budget, ...) instead of
creating a new tenant.

Deliberately simpler status machine than onboarding's (no PROVISIONING
step -- approving *is* applying, one atomic DynamoDB write, not a
multi-step process that can fail partway through):

    PENDING_APPROVAL -> APPLIED
                      \\-> REJECTED

Only ever targets a *provisioned* (DynamoDB-backed) tenant -- a
file-managed tenant's policy changes still go through a YAML PR, same
invariant `onboarding/provisioning.py` already enforces for tenant
creation. See api/admin_routes.py for where that check happens.
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


class PolicyChangeStatus(str, Enum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"


class PolicyChangeError(Exception):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class PolicyChangeNotFoundError(PolicyChangeError):
    def __init__(self, change_id: str):
        super().__init__(f"policy change '{change_id}' not found", code="POLICY_CHANGE_NOT_FOUND")
        self.change_id = change_id


class InvalidPolicyChangeTransitionError(PolicyChangeError):
    def __init__(self, change_id: str, *, from_status: PolicyChangeStatus, action: str):
        super().__init__(
            f"policy change '{change_id}' cannot be {action} from status {from_status.value}",
            code="INVALID_POLICY_CHANGE_TRANSITION",
        )


@dataclass(frozen=True)
class PolicyChangeRequest:
    change_id: str
    tenant_id: str
    # Field name -> new value, e.g. {"rpm_limit": 500, "monthly_budget": 20000.0}.
    # Only fields actually being changed -- not a full TenantPolicy, so
    # a change request can never accidentally reset an unrelated field.
    changes: Dict[str, Any]
    # The tenant's policy_epoch at proposal time -- checked again at
    # approval time (store.py's PolicyEpochConflictError) so an approval
    # can never silently apply on top of an intervening, unrelated write.
    base_policy_epoch: int
    requested_by: str
    status: PolicyChangeStatus = PolicyChangeStatus.PENDING_APPROVAL
    reason: Optional[str] = None  # rejection reason
    approved_by: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


def new_change_id() -> str:
    return f"pchg-{uuid.uuid4().hex[:12]}"


class PolicyChangeStore(Protocol):
    def put(self, change: PolicyChangeRequest) -> None:
        """Create or fully overwrite a change record."""
        ...

    def get(self, change_id: str) -> PolicyChangeRequest:
        """Raises PolicyChangeNotFoundError if change_id doesn't exist."""
        ...

    def list_for_tenant(self, tenant_id: str) -> List[PolicyChangeRequest]:
        """Newest first -- a tenant's change history for the admin/
        portal view. Unbounded scan is fine at this scale, same
        tradeoff OnboardingStore.list_all() already accepts."""
        ...


class InMemoryPolicyChangeStore:
    def __init__(self) -> None:
        self._changes: Dict[str, PolicyChangeRequest] = {}
        self._lock = threading.Lock()

    def put(self, change: PolicyChangeRequest) -> None:
        with self._lock:
            self._changes[change.change_id] = change

    def get(self, change_id: str) -> PolicyChangeRequest:
        with self._lock:
            try:
                return self._changes[change_id]
            except KeyError:
                raise PolicyChangeNotFoundError(change_id) from None

    def list_for_tenant(self, tenant_id: str) -> List[PolicyChangeRequest]:
        with self._lock:
            matches = [c for c in self._changes.values() if c.tenant_id == tenant_id]
            return sorted(matches, key=lambda c: c.created_at, reverse=True)


def _change_to_item(change: PolicyChangeRequest) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "change_id": change.change_id,
        "tenant_id": change.tenant_id,
        "changes": _decimalize(change.changes),
        "base_policy_epoch": change.base_policy_epoch,
        "requested_by": change.requested_by,
        "status": change.status.value,
        "created_at": Decimal(str(change.created_at)),
        "updated_at": Decimal(str(change.updated_at)),
    }
    if change.reason is not None:
        item["reason"] = change.reason
    if change.approved_by is not None:
        item["approved_by"] = change.approved_by
    return item


def _item_to_change(item: Dict[str, Any]) -> PolicyChangeRequest:
    return PolicyChangeRequest(
        change_id=item["change_id"],
        tenant_id=item["tenant_id"],
        changes=_undecimalize(item["changes"]),
        base_policy_epoch=int(item["base_policy_epoch"]),
        requested_by=item["requested_by"],
        status=PolicyChangeStatus(item["status"]),
        reason=item.get("reason"),
        approved_by=item.get("approved_by"),
        created_at=float(item["created_at"]),
        updated_at=float(item["updated_at"]),
    )


def _decimalize(value: Any) -> Any:
    """DynamoDB has no native float type -- `changes` is an arbitrary
    field->value map (int, float, str, bool, list), so this recurses
    rather than assuming a shape the way _policy_to_item's fixed fields
    can."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _decimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decimalize(v) for v in value]
    return value


def _undecimalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value) if value % 1 != 0 else int(value)
    if isinstance(value, dict):
        return {k: _undecimalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_undecimalize(v) for v in value]
    return value


class DynamoDbPolicyChangeStore:
    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, change: PolicyChangeRequest) -> None:
        self._table.put_item(Item=_change_to_item(change))

    def get(self, change_id: str) -> PolicyChangeRequest:
        response = self._table.get_item(Key={"change_id": change_id})
        item = response.get("Item")
        if item is None:
            raise PolicyChangeNotFoundError(change_id)
        return _item_to_change(item)

    def list_for_tenant(self, tenant_id: str) -> List[PolicyChangeRequest]:
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {}
        while True:
            response = self._table.scan(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        changes = [_item_to_change(item) for item in items if item.get("tenant_id") == tenant_id]
        return sorted(changes, key=lambda c: c.created_at, reverse=True)

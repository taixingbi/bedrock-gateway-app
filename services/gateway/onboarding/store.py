"""OnboardingRequest storage (M11). Same seam pattern as JobStore/
UsageStore: `InMemoryOnboardingStore` for tests and any environment
without ONBOARDING_REQUESTS_TABLE_NAME configured, `DynamoDbOnboardingStore`
the real backend.
"""
from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any, Dict, List, Optional, Protocol

from .models import AuthType, OnboardingRequest, OnboardingRequestNotFoundError, OnboardingStatus


class OnboardingStore(Protocol):
    def put(self, request: OnboardingRequest) -> None:
        """Create or fully overwrite a request record."""
        ...

    def get(self, request_id: str) -> OnboardingRequest:
        """Raises OnboardingRequestNotFoundError if request_id doesn't exist."""
        ...

    def list_all(self) -> List[OnboardingRequest]:
        """Every request, newest first -- for the admin review queue /
        portal's onboarding-requests list. Unbounded scan is fine at
        this scale (same tradeoff InMemoryJobStore/DynamoDbJobStore's
        callers already accept)."""
        ...


class InMemoryOnboardingStore:
    def __init__(self) -> None:
        self._requests: Dict[str, OnboardingRequest] = {}
        self._lock = threading.Lock()

    def put(self, request: OnboardingRequest) -> None:
        with self._lock:
            self._requests[request.request_id] = request

    def get(self, request_id: str) -> OnboardingRequest:
        with self._lock:
            try:
                return self._requests[request_id]
            except KeyError:
                raise OnboardingRequestNotFoundError(request_id) from None

    def list_all(self) -> List[OnboardingRequest]:
        with self._lock:
            return sorted(self._requests.values(), key=lambda r: r.created_at, reverse=True)


def _request_to_item(request: OnboardingRequest) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "request_id": request.request_id,
        "tenant_id": request.tenant_id,
        "application_id": request.application_id,
        "environment": request.environment,
        "auth_type": request.auth_type.value,
        "requested_models": list(request.requested_models),
        "rpm_limit": request.rpm_limit,
        "guardrail_policy": request.guardrail_policy,
        "data_classification": request.data_classification,
        "requested_by": request.requested_by,
        "status": request.status.value,
        "created_at": Decimal(str(request.created_at)),
        "updated_at": Decimal(str(request.updated_at)),
    }
    if request.principal_arn is not None:
        item["principal_arn"] = request.principal_arn
    if request.monthly_budget is not None:
        item["monthly_budget"] = Decimal(str(request.monthly_budget))
    if request.reason is not None:
        item["reason"] = request.reason
    return item


def _item_to_request(item: Dict[str, Any]) -> OnboardingRequest:
    return OnboardingRequest(
        request_id=item["request_id"],
        tenant_id=item["tenant_id"],
        application_id=item["application_id"],
        environment=item["environment"],
        auth_type=AuthType(item["auth_type"]),
        principal_arn=item.get("principal_arn"),
        requested_models=list(item.get("requested_models", [])),
        rpm_limit=int(item["rpm_limit"]),
        monthly_budget=float(item["monthly_budget"]) if "monthly_budget" in item else None,
        guardrail_policy=item["guardrail_policy"],
        data_classification=item["data_classification"],
        requested_by=item["requested_by"],
        status=OnboardingStatus(item["status"]),
        reason=item.get("reason"),
        created_at=float(item["created_at"]),
        updated_at=float(item["updated_at"]),
    )


class DynamoDbOnboardingStore:
    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def put(self, request: OnboardingRequest) -> None:
        self._table.put_item(Item=_request_to_item(request))

    def get(self, request_id: str) -> OnboardingRequest:
        response = self._table.get_item(Key={"request_id": request_id})
        item = response.get("Item")
        if item is None:
            raise OnboardingRequestNotFoundError(request_id)
        return _item_to_request(item)

    def list_all(self) -> List[OnboardingRequest]:
        items: List[Dict[str, Any]] = []
        kwargs: Dict[str, Any] = {}
        while True:
            response = self._table.scan(**kwargs)
            items.extend(response.get("Items", []))
            if "LastEvaluatedKey" not in response:
                break
            kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
        requests = [_item_to_request(item) for item in items]
        return sorted(requests, key=lambda r: r.created_at, reverse=True)

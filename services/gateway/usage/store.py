"""Per-tenant monthly spend tracking (M8, plan section 20).

`UsageStore` is the seam -- same pattern as `JobStore`/`PolicyStore`.
`add_and_get()` is an atomic increment (DynamoDB's UpdateItem ADD, or a
lock-protected total in memory) so concurrent requests from the same
tenant never lose an update to a race. Keyed by (tenant_id, month) --
"YYYY-MM" in UTC -- so a new month is just a new row; there's no
explicit reset job anywhere.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, Protocol, Tuple


def current_month(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m")


class UsageStore(Protocol):
    def add_and_get(self, tenant_id: str, month: str, amount: float) -> float:
        """Atomically add `amount` to (tenant_id, month)'s running total
        and return the new total."""
        ...

    def get(self, tenant_id: str, month: str) -> float:
        """Current total for (tenant_id, month); 0.0 if nothing recorded
        yet -- a tenant with no spend this month has no row, not a row
        with spend=0."""
        ...


class InMemoryUsageStore:
    def __init__(self) -> None:
        self._totals: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def add_and_get(self, tenant_id: str, month: str, amount: float) -> float:
        with self._lock:
            key = (tenant_id, month)
            new_total = self._totals.get(key, 0.0) + amount
            self._totals[key] = new_total
            return new_total

    def get(self, tenant_id: str, month: str) -> float:
        with self._lock:
            return self._totals.get((tenant_id, month), 0.0)


class DynamoDbUsageStore:
    """Real DynamoDB-backed UsageStore. boto3 imported lazily, same
    reasoning as jobs/store.py's DynamoDbJobStore/BedrockClient."""

    def __init__(self, *, table_name: str, region: str):
        import boto3

        self._table = boto3.resource("dynamodb", region_name=region).Table(table_name)

    def add_and_get(self, tenant_id: str, month: str, amount: float) -> float:
        from decimal import Decimal

        response = self._table.update_item(
            Key={"tenant_id": tenant_id, "month": month},
            UpdateExpression="ADD spend :amt",
            ExpressionAttributeValues={":amt": Decimal(str(amount))},
            ReturnValues="UPDATED_NEW",
        )
        return float(response["Attributes"]["spend"])

    def get(self, tenant_id: str, month: str) -> float:
        response = self._table.get_item(Key={"tenant_id": tenant_id, "month": month})
        item = response.get("Item")
        if item is None:
            return 0.0
        return float(item["spend"])

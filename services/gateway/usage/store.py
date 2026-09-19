"""Per-tenant monthly spend tracking (M8, plan section 20).

`UsageStore` is the seam -- same pattern as `JobStore`/`PolicyStore`.
`add_and_get()` is an atomic increment (DynamoDB's UpdateItem ADD, or a
lock-protected total in memory) so concurrent requests from the same
tenant never lose an update to a race. Keyed by (tenant_id, month) --
"YYYY-MM" in UTC -- so a new month is just a new row; there's no
explicit reset job anywhere.

Plan section 34.7 (cost governance) needs daily and per-application
tracking too, but deliberately doesn't touch the Protocol or either
implementation to get them -- `period` is an opaque string key already
("YYYY-MM"); `current_day()` below just produces a differently-shaped
one ("YYYY-MM-DD"), and `add_and_get_application()`/`get_application()`
fold `application_id` into the `tenant_id` argument instead of adding
a real third dimension. Same one table/store, same schema, no new
Terraform, no new IAM grant -- composition over the existing key
space, not a new one.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Protocol, Tuple


def current_month(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m")


def current_day(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), tz=timezone.utc).strftime("%Y-%m-%d")


def trailing_days(count: int, *, clock: Callable[[], float] = time.time) -> List[str]:
    """The `count` days strictly before today (today excluded), oldest
    first -- plan section 34.7's anomaly heuristic compares today's
    spend against the average of this window."""
    today = datetime.fromtimestamp(clock(), tz=timezone.utc)
    return [(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(count, 0, -1)]


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


def _application_key(tenant_id: str, application_id: str) -> str:
    return f"{tenant_id}#app:{application_id}"


def add_and_get_application(
    store: "UsageStore", tenant_id: str, application_id: str, month: str, amount: float
) -> float:
    """Plan section 34.7: per-application attribution within a tenant
    (the critique's own example -- "Claims team $10k -> claims-agent
    $7k, summarizer $3k"). See module docstring for why this composes
    over the existing store rather than adding a real third key
    dimension."""
    return store.add_and_get(_application_key(tenant_id, application_id), month, amount)


def get_application(store: "UsageStore", tenant_id: str, application_id: str, month: str) -> float:
    return store.get(_application_key(tenant_id, application_id), month)
